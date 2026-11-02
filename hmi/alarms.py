"""Alarm engine with deadband, on-delay and acknowledgement. Pure logic: the caller supplies signals and time."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from shared.registers import RegisterMap

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "alarms.yaml"
DERIVED_SIGNALS = ("slip_ratio", "emergency_stop", "comm_lost")


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class AlarmState(StrEnum):
    """An alarm stays visible until it is both over and acknowledged, so a brief one cannot go unseen."""

    NORMAL = "normal"
    ACTIVE_UNACKED = "active_unacked"
    ACTIVE_ACKED = "active_acked"
    CLEARED_UNACKED = "cleared_unacked"


class EventKind(StrEnum):
    RAISED = "raised"
    ACKNOWLEDGED = "acknowledged"
    CLEARED = "cleared"


@dataclass(frozen=True)
class AlarmDefinition:
    name: str
    signal: str
    severity: Severity
    threshold: float
    deadband: float
    delay: float
    message: str

    def __post_init__(self) -> None:
        if self.deadband < 0:
            raise ValueError(f"{self.name}: deadband must not be negative")
        if self.delay < 0:
            raise ValueError(f"{self.name}: delay must not be negative")


@dataclass(frozen=True)
class AlarmConfig:
    slip_min_speed: float
    alarms: tuple[AlarmDefinition, ...]


@dataclass(frozen=True)
class AlarmEvent:
    alarm: str
    severity: Severity
    kind: EventKind
    value: float
    timestamp: float
    message: str


@dataclass(frozen=True)
class AlarmStatus:
    name: str
    severity: Severity
    state: AlarmState
    value: float
    raised_at: float
    message: str


def load_alarm_config(registers: RegisterMap, path: Path = DEFAULT_PATH) -> AlarmConfig:
    """Read the YAML file and fail loudly if an alarm watches a signal that does not exist."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    try:
        alarms = tuple(AlarmDefinition(**{**a, "severity": Severity(a["severity"])}) for a in data["alarms"])
        config = AlarmConfig(slip_min_speed=data["slip_min_speed"], alarms=alarms)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from None

    known = {p.name for p in registers.input_registers} | set(DERIVED_SIGNALS)
    names: set[str] = set()
    for alarm in alarms:
        if alarm.signal not in known:
            raise ValueError(f"{alarm.name}: unknown signal {alarm.signal}")
        if alarm.name in names:
            raise ValueError(f"duplicate alarm name: {alarm.name}")
        names.add(alarm.name)
    return config


def derive_signals(readings: Mapping[str, float], registers: RegisterMap, slip_min_speed: float) -> dict[str, float]:
    """The register values plus the signals worked out from them. Only uses what crosses the wire,
    so alarms come from what a real HMI could measure, not from knowing a fault was injected."""
    motor = readings["motor_speed"]
    slip = max(0.0, (motor - readings["belt_speed"]) / motor) if motor >= slip_min_speed else 0.0
    estop_bit = next(bit for bit, label in registers["fault_code"].labels.items() if label == "emergency_stop")
    return {
        **readings,
        "slip_ratio": slip,
        "emergency_stop": 1.0 if int(readings["fault_code"]) & estop_bit else 0.0,
        "comm_lost": 0.0,
    }


class _Tracker:
    def __init__(self, definition: AlarmDefinition) -> None:
        self.definition = definition
        self.state = AlarmState.NORMAL
        self.above_since: float | None = None
        self.value = 0.0
        self.raised_at = 0.0


class AlarmEngine:
    def __init__(self, definitions: Iterable[AlarmDefinition]) -> None:
        self._trackers = {d.name: _Tracker(d) for d in definitions}

    def update(self, signals: Mapping[str, float], now: float, timestamp: float) -> list[AlarmEvent]:
        """Evaluate every alarm. `now` is a monotonic clock for delays, so a wall clock
        adjustment cannot fire or hold back an alarm; `timestamp` is wall time for the record."""
        events = []
        for tracker in self._trackers.values():
            value = signals.get(tracker.definition.signal)
            if value is None:
                # Not measured right now, so nothing is known about whether it stayed high.
                tracker.above_since = None
                continue
            tracker.value = value
            event = self._evaluate(tracker, value, now, timestamp)
            if event:
                events.append(event)
        return events

    def _evaluate(self, tracker: _Tracker, value: float, now: float, timestamp: float) -> AlarmEvent | None:
        d = tracker.definition
        if tracker.state in (AlarmState.ACTIVE_UNACKED, AlarmState.ACTIVE_ACKED):
            if value > d.threshold - d.deadband:
                return None
            acked = tracker.state is AlarmState.ACTIVE_ACKED
            tracker.state = AlarmState.NORMAL if acked else AlarmState.CLEARED_UNACKED
            return self._event(tracker, EventKind.CLEARED, timestamp)

        if value <= d.threshold:
            tracker.above_since = None
            return None
        if tracker.above_since is None:
            tracker.above_since = now
        if now - tracker.above_since < d.delay:
            return None
        tracker.above_since = None
        tracker.state = AlarmState.ACTIVE_UNACKED
        tracker.raised_at = timestamp
        return self._event(tracker, EventKind.RAISED, timestamp)

    def acknowledge(self, name: str, timestamp: float) -> AlarmEvent | None:
        """Returns None if there was nothing to acknowledge. Raises KeyError for an unknown alarm."""
        tracker = self._trackers[name]
        if tracker.state is AlarmState.ACTIVE_UNACKED:
            tracker.state = AlarmState.ACTIVE_ACKED
        elif tracker.state is AlarmState.CLEARED_UNACKED:
            tracker.state = AlarmState.NORMAL
        else:
            return None
        return self._event(tracker, EventKind.ACKNOWLEDGED, timestamp)

    def visible(self) -> list[AlarmStatus]:
        """Alarms the operator should see, critical first and then newest first."""
        visible = [
            AlarmStatus(t.definition.name, t.definition.severity, t.state, t.value, t.raised_at, t.definition.message)
            for t in self._trackers.values() if t.state is not AlarmState.NORMAL
        ]
        return sorted(visible, key=lambda s: (s.severity is not Severity.CRITICAL, -s.raised_at))

    @staticmethod
    def _event(tracker: _Tracker, kind: EventKind, timestamp: float) -> AlarmEvent:
        d = tracker.definition
        return AlarmEvent(d.name, d.severity, kind, tracker.value, timestamp, d.message)
