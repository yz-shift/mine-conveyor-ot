"""The HMI's core: polls the plant, runs the alarm engine, logs, and carries out operator actions."""

import asyncio
import logging
import time

from hmi.alarms import AlarmConfig, AlarmEngine, derive_signals
from hmi.logbook import Logbook
from hmi.plant_client import CommError, PlantClient, PlantStatus
from shared.registers import RegisterMap

logger = logging.getLogger(__name__)

POLL_PERIOD = 1.0  # seconds
COMMAND_PREFIX = "cmd_"
FAULT_PREFIX = "inject_"


class NothingToAcknowledge(Exception):
    pass


class HmiService:
    def __init__(self, client: PlantClient, registers: RegisterMap, alarm_config: AlarmConfig, logbook: Logbook,
                 poll_period: float = POLL_PERIOD) -> None:
        self._client = client
        self._registers = registers
        self._slip_min_speed = alarm_config.slip_min_speed
        self._engine = AlarmEngine(alarm_config.alarms)
        self._logbook = logbook
        self._poll_period = poll_period
        self._poll_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._snapshot: dict = {}
        self.commands = self._names_with_prefix(COMMAND_PREFIX)
        self.faults = self._names_with_prefix(FAULT_PREFIX)

    def _names_with_prefix(self, prefix: str) -> tuple[str, ...]:
        return tuple(p.name.removeprefix(prefix) for p in self._registers.coils if p.name.startswith(prefix))

    async def start(self) -> None:
        await self._poll_once()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def wait(self) -> None:
        """Block until the poll loop ends, which only happens by crashing; the crash is raised here."""
        await self._poll_task

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        self._client.close()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_period)
            await self._poll_once()

    async def _poll_once(self) -> None:
        try:
            status = await self._client.read_status()
        except CommError as e:
            logger.warning("Poll failed: %s", e)
            status = None
            # Only comm_lost has a value, so the other alarms hold their state instead of clearing on no data.
            signals = {"comm_lost": 1.0}
        else:
            signals = derive_signals(status.readings, self._registers, self._slip_min_speed)
        timestamp = time.time()
        for event in self._engine.update(signals, time.monotonic(), timestamp):
            self._logbook.record_alarm(event)
        self._publish(self._build_snapshot(status, timestamp))

    def _build_snapshot(self, status: PlantStatus | None, timestamp: float) -> dict:
        snapshot = {
            "timestamp": timestamp,
            "connected": status is not None,
            # Null rather than the last good values: a frozen number on a gauge looks like a healthy plant.
            "readings": None,
            "setpoint": None,
            "run_state": None,
            "active_faults": None,
            "injections": None,
            "alarms": self._alarms(),
        }
        if status is not None:
            r = status.readings
            faults = self._registers["fault_code"].labels
            snapshot.update(
                readings=r,
                setpoint=status.setpoint,
                run_state=self._registers["run_state"].labels[int(r["run_state"])],
                active_faults=[label for bit, label in faults.items() if int(r["fault_code"]) & bit],
                injections={name: status.coils[FAULT_PREFIX + name] for name in self.faults},
            )
        return snapshot

    def _alarms(self) -> list[dict]:
        return [
            {"name": s.name, "severity": s.severity, "state": s.state, "value": s.value,
             "raised_at": s.raised_at, "message": s.message}
            for s in self._engine.visible()
        ]

    def snapshot(self) -> dict:
        return self._snapshot

    def _publish(self, snapshot: dict) -> None:
        self._snapshot = snapshot
        for queue in self._subscribers:
            # Latest value wins: a slow browser skips updates and must never hold up the poll loop.
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(snapshot)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def send_command(self, name: str) -> None:
        await self._operate(f"command:{name}", "", self._client.write_coil(COMMAND_PREFIX + name, True))

    async def set_speed(self, speed: float) -> None:
        await self._operate("set_speed", f"{speed} m/s", self._client.write_setpoint(speed))

    async def set_fault(self, name: str, active: bool) -> None:
        await self._operate(f"inject:{name}", "on" if active else "off", self._client.write_coil(FAULT_PREFIX + name, active))

    async def _operate(self, action: str, detail: str, write) -> None:
        """Failed attempts are logged too: during an incident, what the operator tried matters."""
        try:
            await write
        except CommError as e:
            self._logbook.record_operation(time.time(), action, detail, error=str(e))
            raise
        self._logbook.record_operation(time.time(), action, detail)

    def acknowledge(self, alarm: str) -> None:
        """Raises KeyError for an unknown alarm and NothingToAcknowledge if it needs no acknowledgement.

        The log is written first; if that fails the alarm stays unacknowledged and the operator can retry.
        """
        event = self._engine.acknowledge(alarm, time.time(), record=self._logbook.record_acknowledgement)
        if event is None:
            raise NothingToAcknowledge(alarm)
        # Show it at once rather than up to one poll later. The timestamp stays that of the last poll,
        # because the readings are from that poll; the page uses it to tell new data from a repeat.
        self._publish({**self._snapshot, "alarms": self._alarms()})
