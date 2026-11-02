"""Modbus client side of the register map. Knows nothing about the simulator, only about registers."""

from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from shared.registers import DEVICE_ID, Point, RegisterMap


class CommError(Exception):
    """The plant could not be reached, or it answered with a Modbus exception."""


@dataclass(frozen=True)
class PlantStatus:
    readings: dict[str, float]
    setpoint: float
    coils: dict[str, bool]


class PlantClient:
    def __init__(self, host: str, port: int, registers: RegisterMap, timeout: float = 1.0) -> None:
        self._registers = registers
        # The poll loop retries every cycle anyway, so pymodbus' own retries would only delay
        # noticing a lost plant, and its background reconnect would compete with connect() below.
        self._client = AsyncModbusTcpClient(host, port=port, timeout=timeout, retries=0, reconnect_delay=0)

    async def read_status(self) -> PlantStatus:
        inputs = await self._read_block(self._client.read_input_registers, self._registers.input_registers, "registers")
        holding = await self._read_block(self._client.read_holding_registers, self._registers.holding_registers, "registers")
        coils = await self._read_block(self._client.read_coils, self._registers.coils, "bits")
        return PlantStatus(
            readings={p.name: p.decode(raw) for p, raw in inputs},
            setpoint=next(p.decode(raw) for p, raw in holding if p.name == "speed_setpoint"),
            coils={p.name: value for p, value in coils},
        )

    async def write_coil(self, name: str, value: bool) -> None:
        await self._call(self._client.write_coil, self._registers[name].address, value)

    async def write_setpoint(self, speed: float) -> None:
        point = self._registers["speed_setpoint"]
        await self._call(self._client.write_register, point.address, point.encode(speed))

    def close(self) -> None:
        self._client.close()

    async def _read_block(self, read, points: tuple[Point, ...], field: str) -> list[tuple[Point, int]]:
        # One request per table: the plant answers it whole, so all the readings are from the same scan.
        start = min(p.address for p in points)
        count = max(p.address for p in points) - start + 1
        values = getattr(await self._call(read, start, count=count), field)
        return [(p, values[p.address - start]) for p in points]

    async def _call(self, request, *args, **kwargs):
        if not self._client.connected and not await self._client.connect():
            raise CommError("cannot connect to the plant")
        try:
            response = await request(*args, device_id=DEVICE_ID, **kwargs)
        except ModbusException as e:
            raise CommError(str(e)) from e
        if response.isError():
            raise CommError(f"Modbus exception code {response.exception_code}")
        return response
