"""Load and validate the Modbus register map shared by the simulator and the HMI."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "registers.yaml"
MAX_ADDRESS = 0xFFFF
MAX_RAW = 0xFFFF
TABLES = ("input_registers", "holding_registers", "coils")


class RegisterMapError(ValueError):
    """The register map file is not usable."""


@dataclass(frozen=True)
class Point:
    """One Modbus data point. Coils are points with scale 1 and a 0..1 range."""

    name: str
    address: int
    table: str
    scale: int = 1
    unit: str = ""
    min: float = 0
    max: float = 1
    description: str = ""
    labels: dict[int, str] = field(default_factory=dict)

    def encode(self, value: float) -> int:
        """Engineering value to raw register value, clamped to the point's range.

        A real sensor saturates at the end of its range, so clamping is the honest
        behaviour here; a wrapped uint16 would show a hot bearing as cold.
        """
        clamped = min(max(value, self.min), self.max)
        return round(clamped * self.scale)

    def decode(self, raw: int) -> float:
        """Raw register value to engineering value."""
        if not 0 <= raw <= MAX_RAW:
            raise ValueError(f"{self.name}: raw value {raw} is not an unsigned 16-bit integer")
        return raw / self.scale


@dataclass(frozen=True)
class RegisterMap:
    input_registers: tuple[Point, ...]
    holding_registers: tuple[Point, ...]
    coils: tuple[Point, ...]

    def __getitem__(self, name: str) -> Point:
        for point in self.input_registers + self.holding_registers + self.coils:
            if point.name == name:
                return point
        raise KeyError(name)


def _parse_point(raw: dict, table: str) -> Point:
    try:
        return Point(
            name=raw["name"],
            address=raw["address"],
            table=table,
            scale=raw.get("scale", 1),
            unit=raw.get("unit", ""),
            min=raw.get("min", 0),
            max=raw.get("max", 1),
            description=raw.get("description", ""),
            labels=raw.get("labels", {}),
        )
    except KeyError as missing:
        raise RegisterMapError(f"{table}: point {raw!r} is missing {missing}") from None


def _validate(points: list[Point]) -> None:
    names: set[str] = set()
    addresses: set[tuple[str, int]] = set()
    for p in points:
        if p.name in names:
            raise RegisterMapError(f"duplicate point name: {p.name}")
        names.add(p.name)

        # Each table has its own address space, so the same number in two tables is fine.
        if (p.table, p.address) in addresses:
            raise RegisterMapError(f"{p.table}: duplicate address {p.address} ({p.name})")
        addresses.add((p.table, p.address))

        if not isinstance(p.address, int) or not 0 <= p.address <= MAX_ADDRESS:
            raise RegisterMapError(f"{p.name}: address {p.address!r} is out of range")
        if p.scale <= 0:
            raise RegisterMapError(f"{p.name}: scale must be positive")
        if p.min >= p.max:
            raise RegisterMapError(f"{p.name}: min must be below max")
        if p.min < 0 or p.max * p.scale > MAX_RAW:
            raise RegisterMapError(f"{p.name}: range does not fit an unsigned 16-bit register")


def load_register_map(path: Path = DEFAULT_PATH) -> RegisterMap:
    """Read the YAML file and fail loudly if the map is inconsistent."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    parsed = {table: tuple(_parse_point(p, table) for p in data.get(table, [])) for table in TABLES}
    _validate([p for points in parsed.values() for p in points])
    return RegisterMap(**parsed)
