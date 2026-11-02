"""Modbus TCP server around the plant, run like a PLC scan: read controls, step the physics, publish readings."""

import asyncio
import logging
import socket
import sys
import time

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from shared.registers import DEVICE_ID, Point, RegisterMap, RegisterMapError, load_register_map
from shared.settings import load_settings
from simulator.plant import ConveyorPlant, Fault, load_plant_config

logger = logging.getLogger(__name__)

SCAN_PERIOD = 0.1  # seconds

# pymodbus picks the data table from the function code, so one code per table is enough here.
FC_COILS = 1
FC_HOLDING = 3
FC_INPUT = 4

INJECTIONS = {
    "inject_overload": Fault.OVERLOAD,
    "inject_bearing_overheat": Fault.BEARING_OVERHEAT,
    "inject_belt_slip": Fault.BELT_SLIP,
}


def _build_device(registers: RegisterMap) -> SimDevice:
    def words(points: tuple[Point, ...]) -> list[SimData]:
        return [SimData(p.address, values=0, datatype=DataType.REGISTERS) for p in points]

    coil_count = max(p.address for p in registers.coils) + 1
    coils = [SimData(0, count=coil_count, values=False, datatype=DataType.BITS)]
    # The map has no discrete inputs, but pymodbus requires a non-empty table of bits. Nothing reads this one.
    unused_discrete_inputs = [SimData(0, values=False, datatype=DataType.BITS)]
    # Separate tables, as the register map defines them: coil 0 and input register 0 are different things.
    return SimDevice(id=DEVICE_ID, simdata=(coils, unused_discrete_inputs, words(registers.holding_registers),
                                            words(registers.input_registers)))


def _ensure_port_free(host: str, port: int) -> None:
    """pymodbus listens with SO_REUSEADDR, which on Windows lets a second server share a busy port
    without any error, and clients then reach either one. A plain bind is still refused there."""
    if sys.platform != "win32":
        return
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
        except OSError as e:
            raise RuntimeError(f"{host}:{port} is already in use") from e


class PlantServer:
    """Serves one plant over Modbus. Clients only ever touch registers; the scan turns them into plant calls."""

    def __init__(self, plant: ConveyorPlant, registers: RegisterMap, host: str, port: int,
                 scan_period: float = SCAN_PERIOD) -> None:
        self._plant = plant
        self._registers = registers
        self._host = host
        self._port = port
        self._scan_period = scan_period
        # Run in this order, so when several arrive in one scan the safer command has the last word:
        # stop beats start, and an emergency stop beats everything.
        self._commands = {
            "cmd_reset": plant.reset,
            "cmd_start": plant.start,
            "cmd_stop": plant.stop,
            "cmd_estop": plant.emergency_stop,
        }
        unknown = {p.name for p in registers.coils} - self._commands.keys() - INJECTIONS.keys()
        if unknown:
            raise RegisterMapError(f"coils with no handler: {sorted(unknown)}")
        self._server: ModbusTcpServer | None = None
        self._scan_task: asyncio.Task | None = None

    async def start(self) -> None:
        _ensure_port_free(self._host, self._port)
        self._server = ModbusTcpServer(_build_device(self._registers), address=(self._host, self._port))
        # Fill the registers before listening: the first client must not see zeros, and the
        # first scan must not read a zero setpoint and take it for a request to stand still.
        await self._write_setpoint()
        await self._publish()
        await self._server.serve_forever(background=True)
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("Modbus server listening on %s:%d", self._host, self._port)

    async def wait(self) -> None:
        """Block until the scan loop ends. It only ends by crashing, and the crash is raised here,
        so the process dies instead of serving frozen readings that look healthy."""
        await self._scan_task

    async def stop(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()
            await asyncio.gather(self._scan_task, return_exceptions=True)
        if self._server:
            await self._server.shutdown()

    async def _scan_loop(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(self._scan_period)
            now = time.perf_counter()
            # Measured, not assumed: a late wake-up must not slow the simulated plant down.
            await self._scan(now - last)
            last = now

    async def _scan(self, dt: float) -> None:
        # Nothing awaited here actually suspends, so client requests are served between scans
        # and can never read half-updated registers or slip a write in between read and clear.
        await self._apply_setpoint()
        await self._apply_injections()
        await self._apply_commands()
        self._plant.step(dt)
        await self._publish()

    async def _apply_setpoint(self) -> None:
        point = self._registers["speed_setpoint"]
        [raw] = await self._server.async_getValues(DEVICE_ID, FC_HOLDING, point.address, 1)
        self._plant.set_speed(point.decode(raw))
        # The plant may have clamped the request; show what it actually accepted.
        await self._write_setpoint()

    async def _write_setpoint(self) -> None:
        point = self._registers["speed_setpoint"]
        raw = point.encode(self._plant.setpoint)
        await self._server.async_setValues(DEVICE_ID, FC_HOLDING, point.address, [raw])

    async def _apply_injections(self) -> None:
        # Level signals: the fault stays active for as long as the coil is on.
        for name, fault in INJECTIONS.items():
            self._plant.inject(fault, await self._coil(name))

    async def _apply_commands(self) -> None:
        # Pulse signals: acted on once, then cleared, so a client only ever writes True.
        for name, command in self._commands.items():
            if await self._coil(name):
                if command() is False:
                    logger.info("%s refused in state %s", name, self._plant.state.name)
                address = self._registers[name].address
                await self._server.async_setValues(DEVICE_ID, FC_COILS, address, [False])

    async def _coil(self, name: str) -> bool:
        [value] = await self._server.async_getValues(DEVICE_ID, FC_COILS, self._registers[name].address, 1)
        return value

    async def _publish(self) -> None:
        readings = self._plant.readings()
        for point in self._registers.input_registers:
            raw = point.encode(getattr(readings, point.name))
            await self._server.async_setValues(DEVICE_ID, FC_INPUT, point.address, [raw])


async def main() -> None:
    settings = load_settings()
    server = PlantServer(ConveyorPlant(load_plant_config()), load_register_map(),
                         settings.modbus_host, settings.modbus_port)
    await server.start()
    try:
        await server.wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
