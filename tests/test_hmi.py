"""Full-stack tests: real plant server, real Modbus client, real HTTP and WebSocket, real SQLite file."""

import asyncio
import json
import pathlib
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, replace

import pytest
import uvicorn
import websockets

from hmi.alarms import AlarmEngine, AlarmEvent, EventKind, Severity, derive_signals, load_alarm_config
from hmi.app import create_app
from hmi.logbook import Logbook
from hmi.plant_client import CommError, PlantClient
from hmi.service import HmiService
from shared.registers import load_register_map
from simulator.plant import ConveyorPlant, Fault, load_plant_config
from simulator.server import PlantServer

REGISTERS = load_register_map()
HOST = "127.0.0.1"
FAST_PLANT = replace(load_plant_config(), acceleration=40.0, estop_deceleration=40.0, belt_response=40.0,
                     thermal_time_constant=0.5, noise_speed=0.0, noise_current=0.0, noise_temp=0.0, noise_vibration=0.0)
SCAN = 0.005
POLL = 0.01


def no_delays(config):
    return replace(config, alarms=tuple(replace(a, delay=0) for a in config.alarms))


ALARMS = no_delays(load_alarm_config(REGISTERS))


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self, tmp_path, poll_period: float = POLL) -> None:
        self.poll_period = poll_period
        self.modbus_port = free_port()
        self.http_port = free_port()
        self.db_path = tmp_path / "logs" / "hmi.sqlite3"
        self.plant_server = self.make_plant_server()

    def make_plant_server(self) -> PlantServer:
        return PlantServer(ConveyorPlant(FAST_PLANT, seed=1), REGISTERS, HOST, self.modbus_port, scan_period=SCAN)

    async def start(self) -> None:
        await self.plant_server.start()
        self.logbook = Logbook(self.db_path)
        client = PlantClient(HOST, self.modbus_port, REGISTERS, timeout=0.5)
        self.service = HmiService(client, REGISTERS, ALARMS, self.logbook, poll_period=self.poll_period)
        await self.service.start()
        app = create_app(self.service, REGISTERS, self.logbook, ALARMS)
        self.http = uvicorn.Server(uvicorn.Config(app, host=HOST, port=self.http_port, log_level="warning",
                                                  timeout_graceful_shutdown=1))
        self.serving = asyncio.create_task(self.http.serve())
        while not self.http.started:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self.http.should_exit = True
        await self.serving
        await self.service.stop()
        await self.plant_server.stop()
        self.logbook.close()

    def _request(self, method: str, path: str, body: dict | None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://{HOST}:{self.http_port}{path}", data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    async def get(self, path: str) -> object:
        status, body = await asyncio.to_thread(self._request, "GET", path, None)
        assert status == 200, body
        return body

    async def post(self, path: str, body: dict | None = None, expect: int = 200) -> object:
        status, reply = await asyncio.to_thread(self._request, "POST", path, body)
        assert status == expect, reply
        return reply

    async def until(self, predicate, timeout: float = 5.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            state = await self.get("/api/state")
            if predicate(state):
                return state
            assert asyncio.get_running_loop().time() < deadline, f"never true, last state: {state}"
            await asyncio.sleep(POLL)

    async def until_running(self) -> dict:
        await self.post("/api/commands/start")
        return await self.until(lambda s: s["run_state"] == "running")

    async def until_alarm(self, name: str) -> dict:
        state = await self.until(lambda s: any(a["name"] == name for a in s["alarms"]))
        return next(a for a in state["alarms"] if a["name"] == name)


@pytest.fixture
async def stack(tmp_path):
    s = Stack(tmp_path)
    await s.start()
    yield s
    await s.stop()


# --- Reading state ---

async def test_state_shows_the_stopped_plant(stack):
    state = await stack.get("/api/state")
    assert state["connected"] is True
    assert state["run_state"] == "stopped"
    assert state["active_faults"] == []
    assert state["setpoint"] == FAST_PLANT.nominal_speed
    assert state["alarms"] == []
    assert state["injections"] == {"overload": False, "bearing_overheat": False, "belt_slip": False}
    assert set(state["readings"]) == {p.name for p in REGISTERS.input_registers}


async def test_config_describes_the_page_from_the_yaml_files(stack):
    config = await stack.get("/api/config")
    names = {r["name"] for r in config["readings"]}
    assert names == {p.name for p in REGISTERS.input_registers if p.unit}
    assert "run_state" not in names, "state and fault bits have their own display, not a gauge"
    assert config["speed"] == {"min": 0, "max": FAST_PLANT.max_speed, "unit": "m/s"}
    assert config["faults"] == ["overload", "bearing_overheat", "belt_slip"]
    assert {a["name"] for a in config["alarms"]} == {a.name for a in ALARMS.alarms}


async def test_page_and_its_files_are_served_without_hiding_the_api(stack):
    def fetch(path):
        with urllib.request.urlopen(f"http://{HOST}:{stack.http_port}{path}", timeout=5) as response:
            return response.read().decode("utf-8")

    page = await asyncio.to_thread(fetch, "/")
    for asset in re.findall(r'(?:src|href)="(?!https?:)([^"]+)"', page):
        assert await asyncio.to_thread(fetch, f"/{asset}")
    assert "app.js" in page
    assert (await stack.get("/api/state"))["connected"] is True


async def test_websocket_pushes_updates(stack):
    async with websockets.connect(f"ws://{HOST}:{stack.http_port}/ws") as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), 2))
        assert first["run_state"] == "stopped"
        await stack.post("/api/commands/start")
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if message["run_state"] != "stopped":
                break
        assert message["timestamp"] > first["timestamp"]


async def test_websocket_sends_the_current_state_without_waiting_for_a_poll(tmp_path):
    stack = Stack(tmp_path, poll_period=60)
    await stack.start()
    try:
        async with websockets.connect(f"ws://{HOST}:{stack.http_port}/ws") as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 2))["run_state"] == "stopped"
    finally:
        await stack.stop()


async def test_closed_websocket_is_forgotten(stack):
    async with websockets.connect(f"ws://{HOST}:{stack.http_port}/ws") as ws:
        await ws.recv()
        assert len(stack.service._subscribers) == 1
    for _ in range(200):
        if not stack.service._subscribers:
            return
        await asyncio.sleep(POLL)
    pytest.fail("subscriber was never removed")


def test_slow_subscriber_only_keeps_the_latest_snapshot(tmp_path):
    async def scenario():
        logbook = Logbook(tmp_path / "x.sqlite3")
        service = HmiService(PlantClient(HOST, 1, REGISTERS), REGISTERS, ALARMS, logbook)
        queue = service.subscribe()
        service._publish({"n": 1})
        service._publish({"n": 2})
        assert queue.get_nowait() == {"n": 2}
        assert queue.empty()
        logbook.close()
    asyncio.run(scenario())


# --- Control ---

async def test_start_stop_and_speed_reach_the_plant(stack):
    await stack.until_running()
    await stack.post("/api/speed", {"speed": 2.5})
    await stack.until(lambda s: s["setpoint"] == 2.5 and s["readings"]["belt_speed"] == pytest.approx(2.5, abs=0.01))
    await stack.post("/api/commands/stop")
    await stack.until(lambda s: s["run_state"] == "stopped")


async def test_fault_injection_can_be_turned_off_again(stack):
    await stack.post("/api/faults/belt_slip", {"active": True})
    await stack.until(lambda s: s["injections"]["belt_slip"] and "belt_slip" in s["active_faults"])
    await stack.post("/api/faults/belt_slip", {"active": False})
    await stack.until(lambda s: not s["injections"]["belt_slip"] and s["active_faults"] == [])


async def test_commands_during_polling_do_not_collide(stack):
    await asyncio.gather(*(stack.post("/api/speed", {"speed": 1 + i / 10}) for i in range(20)))
    state = await stack.until(lambda s: s["connected"])
    assert state["connected"]


async def test_bad_requests_are_rejected(stack):
    await stack.post("/api/commands/explode", expect=404)
    await stack.post("/api/faults/explode", {"active": True}, expect=404)
    await stack.post("/api/speed", {"speed": 6.01}, expect=422)
    await stack.post("/api/speed", {"speed": -1}, expect=422)
    await stack.post("/api/speed", {"speed": "fast"}, expect=422)
    assert await stack.get("/api/operations") == []


# --- Each injected fault produces its alarm, from measurements alone ---

async def test_overload_raises_flow_and_current_alarms(stack):
    await stack.until_running()
    await stack.post("/api/faults/overload", {"active": True})
    await stack.until_alarm("material_flow_high")
    await stack.until_alarm("motor_current_high")


async def test_bearing_overheat_raises_temperature_and_vibration_alarms(stack):
    await stack.until_running()
    await stack.post("/api/faults/bearing_overheat", {"active": True})
    for name in ("bearing_temp_high", "bearing_temp_critical", "vibration_high", "vibration_critical"):
        await stack.until_alarm(name)


async def test_belt_slip_raises_slip_alarms(stack):
    await stack.until_running()
    await stack.post("/api/faults/belt_slip", {"active": True})
    alarm = await stack.until_alarm("belt_slip_critical")
    assert alarm["severity"] == "critical"
    await stack.until_alarm("belt_slip_warning")


async def test_emergency_stop_raises_its_alarm(stack):
    await stack.until_running()
    await stack.post("/api/commands/estop")
    await stack.until_alarm("emergency_stop")
    state = await stack.get("/api/state")
    assert "emergency_stop" in state["active_faults"]


async def test_healthy_running_raises_no_alarms(stack):
    await stack.until_running()
    await asyncio.sleep(POLL * 30)
    assert (await stack.get("/api/state"))["alarms"] == []


def simulate_alarms(fault: Fault | None, speed: float, seconds: int) -> set[str]:
    """Real plant parameters and real alarm config, no network: which alarms are ever raised?"""
    plant_config = load_plant_config()
    plant = ConveyorPlant(plant_config, seed=3)
    plant.set_speed(speed)
    plant.start()
    if fault:
        plant.inject(fault, True)
    config = load_alarm_config(REGISTERS)
    engine = AlarmEngine(config.alarms)
    raised = set()
    # Poll once a second like the HMI, with the plant stepping at its real scan period in between.
    for second in range(seconds):
        for _ in range(10):
            plant.step(0.1)
        signals = derive_signals(asdict(plant.readings()), REGISTERS, config.slip_min_speed)
        raised |= {e.alarm for e in engine.update(signals, second, second) if e.kind is EventKind.RAISED}
    return raised


@pytest.mark.parametrize("speed", [4.0, 6.0])
def test_real_thresholds_give_no_false_alarms_in_six_healthy_hours(speed):
    assert simulate_alarms(None, speed, 6 * 3600) == set()


@pytest.mark.parametrize("fault, expected", [
    (Fault.OVERLOAD, {"material_flow_high", "motor_current_high"}),
    (Fault.BEARING_OVERHEAT, {"bearing_temp_high", "bearing_temp_critical", "vibration_high", "vibration_critical"}),
    (Fault.BELT_SLIP, {"belt_slip_warning", "belt_slip_critical"}),
])
def test_real_thresholds_catch_each_fault_within_ten_minutes_at_nominal_speed(fault, expected):
    assert expected <= simulate_alarms(fault, 4.0, 600)


# --- Alarm lifecycle through the API ---

async def test_alarm_lifecycle_is_logged(stack):
    await stack.until_running()
    await stack.post("/api/commands/estop")
    alarm = await stack.until_alarm("emergency_stop")
    assert alarm["state"] == "active_unacked"

    await stack.post("/api/alarms/emergency_stop/ack")
    state = await stack.get("/api/state")
    assert next(a for a in state["alarms"] if a["name"] == "emergency_stop")["state"] == "active_acked"
    await stack.post("/api/alarms/emergency_stop/ack", expect=409)

    await stack.until(lambda s: s["readings"]["belt_speed"] == 0)
    await stack.post("/api/commands/reset")
    await stack.until(lambda s: s["alarms"] == [])

    events = await stack.get("/api/alarm-events")
    mine = [e["kind"] for e in events if e["alarm"] == "emergency_stop"]
    assert mine == ["cleared", "acknowledged", "raised"], "newest first"


async def test_unknown_alarm_acknowledge_is_404(stack):
    await stack.post("/api/alarms/nope/ack", expect=404)


# --- Operations log ---

async def test_every_operation_is_logged_with_details(stack):
    await stack.post("/api/commands/start")
    await stack.post("/api/speed", {"speed": 3.0})
    await stack.post("/api/faults/belt_slip", {"active": True})
    await stack.post("/api/faults/belt_slip", {"active": False})
    await stack.post("/api/commands/estop")
    await stack.until_alarm("emergency_stop")
    await stack.post("/api/alarms/emergency_stop/ack")

    operations = await stack.get("/api/operations")
    assert [(o["action"], o["detail"], o["ok"]) for o in reversed(operations)] == [
        ("command:start", "", 1),
        ("set_speed", "3.0 m/s", 1),
        ("inject:belt_slip", "on", 1),
        ("inject:belt_slip", "off", 1),
        ("command:estop", "", 1),
        ("acknowledge", "emergency_stop", 1),
    ]


async def test_history_limit_is_enforced(stack):
    await stack.post("/api/commands/start")
    await stack.post("/api/commands/stop")
    assert len(await stack.get("/api/operations?limit=1")) == 1
    status, _ = await asyncio.to_thread(stack._request, "GET", "/api/operations?limit=99999", None)
    assert status == 422


def test_logbook_survives_a_restart(tmp_path):
    path = tmp_path / "hmi.sqlite3"
    first = Logbook(path)
    first.record_operation(1.0, "command:start", "")
    first.record_alarm(AlarmEvent("a", Severity.WARNING, EventKind.RAISED, 5.0, 2.0, "m"))
    first.close()
    second = Logbook(path)
    assert second.recent_operations()[0]["action"] == "command:start"
    event = second.recent_alarm_events()[0]
    assert (event["alarm"], event["severity"], event["kind"], event["value"]) == ("a", "warning", "raised", 5.0)
    second.close()


def test_logbook_stores_hostile_text_as_plain_data(tmp_path):
    logbook = Logbook(tmp_path / "hmi.sqlite3")
    text = "x'); DROP TABLE operations; --"
    logbook.record_operation(1.0, text, text)
    assert logbook.recent_operations()[0]["action"] == text
    logbook.close()


# --- Losing the plant ---

async def test_losing_the_plant_raises_an_alarm_blanks_readings_and_recovers(stack):
    await stack.until_running()
    await stack.post("/api/faults/belt_slip", {"active": True})
    await stack.until_alarm("belt_slip_critical")

    await stack.plant_server.stop()
    state = await stack.until(lambda s: not s["connected"])
    assert state["readings"] is None, "no frozen numbers"
    assert state["run_state"] is None
    states = {a["name"]: a["state"] for a in state["alarms"]}
    assert states["comm_lost"] == "active_unacked"
    assert states["belt_slip_critical"] == "active_unacked", "alarms hold their state with no data, they do not clear"

    await stack.post("/api/commands/stop", expect=503)
    failed = (await stack.get("/api/operations"))[0]
    assert failed["action"] == "command:stop"
    assert failed["ok"] == 0
    assert failed["error"]

    stack.plant_server = stack.make_plant_server()
    await stack.plant_server.start()
    state = await stack.until(lambda s: s["connected"])
    await stack.until(lambda s: not any(a["name"] == "belt_slip_critical" and a["state"].startswith("active")
                                        for a in s["alarms"]))
    assert state["run_state"] == "stopped"
    comm = next(a for a in (await stack.get("/api/state"))["alarms"] if a["name"] == "comm_lost")
    assert comm["state"] == "cleared_unacked", "the outage stays on screen until someone acknowledges it"


async def test_hmi_can_start_before_the_plant_exists(tmp_path):
    logbook = Logbook(tmp_path / "hmi.sqlite3")
    service = HmiService(PlantClient(HOST, free_port(), REGISTERS, timeout=0.2), REGISTERS, ALARMS, logbook, poll_period=POLL)
    await service.start()
    assert service.snapshot()["connected"] is False
    assert service.snapshot()["alarms"][0]["name"] == "comm_lost"
    await service.stop()
    logbook.close()


async def test_client_reports_failure_as_comm_error():
    client = PlantClient(HOST, free_port(), REGISTERS, timeout=0.2)
    with pytest.raises(CommError):
        await client.read_status()
    with pytest.raises(CommError):
        await client.write_coil("cmd_start", True)
    client.close()


async def test_plant_that_connects_but_never_answers_is_a_comm_error():
    # The nasty failure: the socket looks fine and the device is hung.
    async def ignore(reader, writer):
        await reader.read()
        writer.close()

    port = free_port()
    hung = await asyncio.start_server(ignore, HOST, port)
    client = PlantClient(HOST, port, REGISTERS, timeout=0.2)
    try:
        with pytest.raises(CommError):
            await client.read_status()
    finally:
        client.close()
        hung.close()


async def test_modbus_exception_reply_is_a_comm_error(stack):
    moved = tuple(replace(p, address=p.address + 100) for p in REGISTERS.input_registers)
    client = PlantClient(HOST, stack.modbus_port, replace(REGISTERS, input_registers=moved), timeout=0.5)
    with pytest.raises(CommError, match="exception code"):
        await client.read_status()
    client.close()


async def test_crashed_poll_loop_is_raised_not_hidden(tmp_path):
    class BrokenLogbook(Logbook):
        def record_alarm(self, event):
            raise RuntimeError("disk full")

    # The delay lets start() finish; the plant is absent, so comm_lost then tries to log and fails.
    comm_lost = next(a for a in ALARMS.alarms if a.name == "comm_lost")
    config = replace(ALARMS, alarms=(replace(comm_lost, delay=0.05),))
    logbook = BrokenLogbook(tmp_path / "hmi.sqlite3")
    service = HmiService(PlantClient(HOST, free_port(), REGISTERS, timeout=0.2), REGISTERS, config, logbook, poll_period=POLL)
    await service.start()
    with pytest.raises(RuntimeError, match="disk full"):
        await asyncio.wait_for(service.wait(), timeout=5)
    await service.stop()
    logbook.close()


# --- Separation of the two sides ---

def test_hmi_never_imports_the_simulator():
    # The whole point of the architecture: swap the simulator for a real PLC and the HMI cannot tell.
    hmi_dir = pathlib.Path(__file__).resolve().parent.parent / "hmi"
    for source in hmi_dir.glob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+simulator", source.read_text(encoding="utf-8"), re.M), source.name


# --- Fixes from the 2026-11-05 review ---

async def test_requests_issued_together_open_only_one_connection():
    port = free_port()
    server = PlantServer(ConveyorPlant(FAST_PLANT, seed=1), REGISTERS, HOST, port, scan_period=SCAN)
    await server.start()
    try:
        for _ in range(10):
            client = PlantClient(HOST, port, REGISTERS, timeout=0.5)
            await asyncio.gather(client.read_status(), client.write_setpoint(1.0), client.read_status(),
                                 client.write_setpoint(1.5))
            await asyncio.sleep(0.05)
            assert len(server._server.active_connections) == 1
            client.close()
            await asyncio.sleep(0.05)
    finally:
        await server.stop()


class FailingLogbook(Logbook):
    fail = False

    def record_acknowledgement(self, event):
        if self.fail:
            raise RuntimeError("disk full")
        super().record_acknowledgement(event)


async def test_acknowledgement_that_cannot_be_logged_does_not_take_effect(tmp_path):
    book = FailingLogbook(tmp_path / "x.sqlite3")
    service = HmiService(PlantClient(HOST, free_port(), REGISTERS, timeout=0.2), REGISTERS, ALARMS, book)
    try:
        await service._poll_once()
        book.fail = True
        with pytest.raises(RuntimeError):
            service.acknowledge("comm_lost")
        assert next(a for a in service.snapshot()["alarms"] if a["name"] == "comm_lost")["state"] == "active_unacked"
        assert book.recent_operations() == []

        book.fail = False
        service.acknowledge("comm_lost")
        assert [e["kind"] for e in book.recent_alarm_events()] == ["acknowledged", "raised"]
        assert [o["action"] for o in book.recent_operations()] == ["acknowledge"]
    finally:
        await service.stop()
        book.close()


def test_chart_library_cannot_hold_up_the_page():
    page = (pathlib.Path(__file__).parent.parent / "hmi" / "static" / "index.html").read_text(encoding="utf-8")
    external = re.findall(r"<script[^>]*src=\"https?:[^>]*>", page)
    assert external, "the test is looking at the wrong tag"
    assert all(" async" in tag for tag in external)
