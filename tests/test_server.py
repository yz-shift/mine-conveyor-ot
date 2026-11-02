"""Integration tests: a real server on a real TCP socket, driven only through a Modbus client."""

import asyncio
import socket
from dataclasses import replace

import pytest
from pymodbus.client import AsyncModbusTcpClient

from shared.registers import DEVICE_ID, RegisterMapError, load_register_map
from simulator.plant import ConveyorPlant, Fault, RunState, load_plant_config
from simulator.server import PlantServer

REGISTERS = load_register_map()
HOST = "127.0.0.1"
# Same plant, faster physics, so the tests watch whole transitions in milliseconds of wall time.
FAST = replace(load_plant_config(), acceleration=40.0, estop_deceleration=40.0, belt_response=40.0,
               thermal_time_constant=0.5, noise_speed=0.0, noise_current=0.0, noise_temp=0.0, noise_vibration=0.0)
SCAN = 0.005


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Hmi:
    """The client side of the register map, as a stand-in for the real HMI."""

    def __init__(self, client: AsyncModbusTcpClient) -> None:
        self.client = client

    async def read(self, name: str) -> float:
        point = REGISTERS[name]
        response = await self.client.read_input_registers(point.address, count=1, device_id=DEVICE_ID)
        assert not response.isError(), response
        return point.decode(response.registers[0])

    async def setpoint(self) -> float:
        point = REGISTERS["speed_setpoint"]
        response = await self.client.read_holding_registers(point.address, count=1, device_id=DEVICE_ID)
        assert not response.isError(), response
        return point.decode(response.registers[0])

    async def write_setpoint(self, speed: float) -> None:
        point = REGISTERS["speed_setpoint"]
        response = await self.client.write_register(point.address, round(speed * point.scale), device_id=DEVICE_ID)
        assert not response.isError(), response

    async def coil(self, name: str) -> bool:
        response = await self.client.read_coils(REGISTERS[name].address, count=1, device_id=DEVICE_ID)
        assert not response.isError(), response
        return response.bits[0]

    async def write(self, name: str, value: bool = True) -> None:
        response = await self.client.write_coil(REGISTERS[name].address, value, device_id=DEVICE_ID)
        assert not response.isError(), response

    async def write_together(self, *names: str) -> None:
        """One FC15 request, so all the coils are set before the next scan sees any of them."""
        values = [p.name in names for p in REGISTERS.coils]
        response = await self.client.write_coils(0, values, device_id=DEVICE_ID)
        assert not response.isError(), response

    async def until(self, name: str, predicate, timeout: float = 3.0) -> float:
        """Poll until the predicate holds, as an HMI would. Fails with the last value seen."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            value = await self.read(name)
            if predicate(value):
                return value
            assert asyncio.get_running_loop().time() < deadline, f"{name} stuck at {value}"
            await asyncio.sleep(SCAN)

    async def until_state(self, state: RunState) -> None:
        await self.until("run_state", lambda v: v == state)

    async def settle(self) -> None:
        """Long enough for several scans to have run."""
        await asyncio.sleep(SCAN * 10)


@pytest.fixture
def port():
    return free_port()


@pytest.fixture
async def server(port):
    plant_server = PlantServer(ConveyorPlant(FAST, seed=1), REGISTERS, HOST, port, scan_period=SCAN)
    await plant_server.start()
    yield plant_server
    await plant_server.stop()


@pytest.fixture
async def hmi(server, port):
    client = AsyncModbusTcpClient(HOST, port=port)
    assert await client.connect()
    yield Hmi(client)
    client.close()


async def test_first_read_sees_a_healthy_stopped_plant(hmi):
    assert await hmi.read("run_state") == RunState.STOPPED
    assert await hmi.read("belt_speed") == 0
    assert await hmi.read("fault_code") == 0
    assert await hmi.read("bearing_temp") == pytest.approx(FAST.ambient_temp, abs=0.1)
    assert await hmi.setpoint() == FAST.nominal_speed


async def test_readings_are_there_before_the_first_scan(port):
    plant_server = PlantServer(ConveyorPlant(FAST), REGISTERS, HOST, port, scan_period=60)
    await plant_server.start()
    client = AsyncModbusTcpClient(HOST, port=port)
    try:
        assert await client.connect()
        assert await Hmi(client).read("bearing_temp") == pytest.approx(FAST.ambient_temp, abs=0.1)
    finally:
        client.close()
        await plant_server.stop()


async def test_a_scan_runs_without_giving_way_to_client_requests(server):
    # The coroutine finishing on its first send proves it never suspended, so no request can run mid-scan.
    with pytest.raises(StopIteration):
        server._scan(0.01).send(None)


async def test_all_input_registers_can_be_read_in_one_request(hmi):
    response = await hmi.client.read_input_registers(0, count=len(REGISTERS.input_registers), device_id=DEVICE_ID)
    assert not response.isError()
    assert len(response.registers) == len(REGISTERS.input_registers)


async def test_start_brings_the_belt_up_to_the_setpoint(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    await hmi.until("belt_speed", lambda v: v == pytest.approx(FAST.nominal_speed, abs=0.01))
    assert await hmi.read("motor_current") > FAST.no_load_current
    assert await hmi.read("material_flow") > 0


async def test_command_coils_clear_themselves(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    assert not await hmi.coil("cmd_start")


async def test_stop_brings_the_belt_to_rest(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    await hmi.write("cmd_stop")
    await hmi.until_state(RunState.STOPPED)
    assert await hmi.read("belt_speed") == 0


async def test_writing_the_setpoint_changes_the_belt_speed(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    await hmi.write_setpoint(2.5)
    await hmi.until("belt_speed", lambda v: v == pytest.approx(2.5, abs=0.01))
    assert await hmi.setpoint() == 2.5


async def test_over_range_setpoint_reads_back_what_the_plant_accepted(hmi):
    await hmi.write_setpoint(50.0)
    await hmi.settle()
    assert await hmi.setpoint() == FAST.max_speed


def test_register_range_of_the_setpoint_matches_the_plant():
    assert REGISTERS["speed_setpoint"].max == FAST.max_speed


async def test_emergency_stop_latches_until_reset(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    await hmi.write("cmd_estop")
    await hmi.until_state(RunState.EMERGENCY_STOP)
    assert int(await hmi.read("fault_code")) & Fault.EMERGENCY_STOP
    await hmi.until("belt_speed", lambda v: v == 0)

    await hmi.write("cmd_start")
    await hmi.settle()
    assert await hmi.read("run_state") == RunState.EMERGENCY_STOP

    await hmi.write("cmd_reset")
    await hmi.until_state(RunState.STOPPED)
    await hmi.settle()
    assert await hmi.read("run_state") == RunState.STOPPED, "neither the reset nor the refused start may restart it"
    assert await hmi.read("fault_code") == 0


async def test_emergency_stop_wins_over_everything_in_the_same_scan(hmi):
    await hmi.write_together("cmd_start", "cmd_stop", "cmd_reset", "cmd_estop")
    await hmi.until_state(RunState.EMERGENCY_STOP)
    await hmi.settle()
    assert await hmi.read("run_state") == RunState.EMERGENCY_STOP


async def test_stop_wins_over_start_in_the_same_scan(hmi):
    await hmi.write_together("cmd_start", "cmd_stop")
    await hmi.until_state(RunState.STOPPED)
    await hmi.settle()
    assert await hmi.read("run_state") == RunState.STOPPED
    assert await hmi.read("belt_speed") == 0


async def test_overload_coil_raises_flow_until_cleared(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    await hmi.until("belt_speed", lambda v: v == pytest.approx(FAST.nominal_speed, abs=0.01))
    await hmi.write("inject_overload")
    await hmi.until("fault_code", lambda v: int(v) & Fault.OVERLOAD)
    assert await hmi.read("material_flow") > FAST.nominal_flow * 1.2
    await hmi.settle()
    assert await hmi.coil("inject_overload"), "injection coils hold their value"

    await hmi.write("inject_overload", False)
    await hmi.until("fault_code", lambda v: v == 0)
    assert await hmi.read("material_flow") < FAST.nominal_flow * 1.4


async def test_bearing_overheat_coil_heats_the_bearing(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    healthy = await hmi.until("bearing_temp", lambda v: v > FAST.ambient_temp + 30)
    await hmi.write("inject_bearing_overheat")
    await hmi.until("bearing_temp", lambda v: v > healthy + 30)
    assert int(await hmi.read("fault_code")) & Fault.BEARING_OVERHEAT


async def test_belt_slip_coil_separates_belt_and_motor_speed(hmi):
    await hmi.write("cmd_start")
    await hmi.until_state(RunState.RUNNING)
    await hmi.write("inject_belt_slip")
    expected = FAST.nominal_speed * (1 - FAST.slip_ratio)
    await hmi.until("belt_speed", lambda v: v == pytest.approx(expected, abs=0.01))
    assert await hmi.read("motor_speed") == pytest.approx(FAST.nominal_speed, abs=0.01)


async def test_second_client_sees_the_same_plant(hmi, port):
    other = AsyncModbusTcpClient(HOST, port=port)
    assert await other.connect()
    try:
        await hmi.write("cmd_start")
        await Hmi(other).until_state(RunState.RUNNING)
    finally:
        other.close()


async def test_unmapped_address_is_a_modbus_exception(hmi):
    response = await hmi.client.read_input_registers(100, count=1, device_id=DEVICE_ID)
    assert response.isError()
    response = await hmi.client.read_holding_registers(100, count=1, device_id=DEVICE_ID)
    assert response.isError()
    response = await hmi.client.read_coils(100, count=1, device_id=DEVICE_ID)
    assert response.isError()


async def test_a_crashed_scan_is_raised_not_hidden(port):
    class BrokenPlant(ConveyorPlant):
        def step(self, dt):
            raise RuntimeError("model blew up")

    plant_server = PlantServer(BrokenPlant(FAST), REGISTERS, HOST, port, scan_period=SCAN)
    await plant_server.start()
    try:
        with pytest.raises(RuntimeError, match="model blew up"):
            await asyncio.wait_for(plant_server.wait(), timeout=2)
    finally:
        await plant_server.stop()


async def test_restart_on_the_same_port_works_after_a_client_was_connected(port):
    for _ in range(2):
        plant_server = PlantServer(ConveyorPlant(FAST), REGISTERS, HOST, port, scan_period=SCAN)
        await plant_server.start()
        client = AsyncModbusTcpClient(HOST, port=port)
        assert await client.connect()
        assert await Hmi(client).read("run_state") == RunState.STOPPED
        await plant_server.stop()
        client.close()


async def test_second_server_on_the_same_port_fails_loudly(server, port):
    second = PlantServer(ConveyorPlant(FAST), REGISTERS, HOST, port, scan_period=SCAN)
    with pytest.raises(RuntimeError):
        await second.start()
    await second.stop()
    # The first server must be unharmed.
    client = AsyncModbusTcpClient(HOST, port=port)
    assert await client.connect()
    assert await Hmi(client).read("run_state") == RunState.STOPPED
    client.close()


def test_coil_without_a_handler_is_rejected():
    extra = replace(REGISTERS.coils[0], name="cmd_explode", address=99)
    registers = replace(REGISTERS, coils=REGISTERS.coils + (extra,))
    with pytest.raises(RegisterMapError, match="cmd_explode"):
        PlantServer(ConveyorPlant(FAST), registers, HOST, 5020)
