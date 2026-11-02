"""Physical model of one conveyor. Pure logic: no network and no clock, time only moves in step()."""

import math
import random
from dataclasses import dataclass, fields
from enum import IntEnum, IntFlag
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "plant.yaml"
SPEED_EPSILON = 1e-9
FEED_LIMITS = (0.7, 1.3)


class RunState(IntEnum):
    STOPPED = 0
    STARTING = 1
    RUNNING = 2
    STOPPING = 3
    EMERGENCY_STOP = 4


class Fault(IntFlag):
    OVERLOAD = 1
    BEARING_OVERHEAT = 2
    BELT_SLIP = 4
    EMERGENCY_STOP = 8


@dataclass(frozen=True)
class PlantConfig:
    nominal_speed: float
    max_speed: float
    acceleration: float
    estop_deceleration: float
    belt_response: float
    nominal_flow: float
    feed_reversion: float
    feed_sigma: float
    no_load_current: float
    current_per_speed: float
    current_per_flow: float
    ambient_temp: float
    temp_rise_per_speed: float
    thermal_time_constant: float
    vibration_per_speed: float
    vibration_onset_temp: float
    vibration_per_degree: float
    overload_factor: float
    overheat_cooling_factor: float
    slip_ratio: float
    noise_speed: float
    noise_current: float
    noise_temp: float
    noise_vibration: float

    def __post_init__(self) -> None:
        for f in fields(self):
            if getattr(self, f.name) < 0:
                raise ValueError(f"{f.name} must not be negative")
        # These divide or set a rate, so zero would freeze or break the model.
        for name in ("nominal_speed", "max_speed", "acceleration", "estop_deceleration",
                     "belt_response", "thermal_time_constant", "overheat_cooling_factor"):
            if getattr(self, name) == 0:
                raise ValueError(f"{name} must be positive")
        if self.nominal_speed > self.max_speed:
            raise ValueError("nominal_speed must not exceed max_speed")
        if self.slip_ratio >= 1:
            raise ValueError("slip_ratio must be below 1")


def load_plant_config(path: Path = DEFAULT_PATH) -> PlantConfig:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    try:
        return PlantConfig(**data)
    except TypeError as e:
        raise ValueError(f"{path}: {e}") from None


@dataclass(frozen=True)
class Readings:
    """What the sensors report. Field names match the input registers in registers.yaml."""

    belt_speed: float
    motor_speed: float
    motor_current: float
    bearing_temp: float
    vibration: float
    material_flow: float
    run_state: int
    fault_code: int


def _move_toward(value: float, target: float, max_change: float) -> float:
    if abs(target - value) <= max_change:
        return target
    return value + math.copysign(max_change, target - value)


class ConveyorPlant:
    """State machine plus physics. Commands return False when the plant refuses them."""

    def __init__(self, config: PlantConfig, seed: int | None = None) -> None:
        self._cfg = config
        # Two generators, so how often the HMI polls cannot change how the plant behaves.
        self._process_rng = random.Random(seed)
        self._sensor_rng = random.Random(None if seed is None else seed + 1)
        self._state = RunState.STOPPED
        self._setpoint = config.nominal_speed
        self._injected = Fault(0)
        self._motor = 0.0
        self._belt = 0.0
        self._feed = 1.0
        self._temp = config.ambient_temp

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def setpoint(self) -> float:
        return self._setpoint

    @property
    def faults(self) -> Fault:
        if self._state is RunState.EMERGENCY_STOP:
            return self._injected | Fault.EMERGENCY_STOP
        return self._injected

    def start(self) -> bool:
        if self._state is not RunState.STOPPED:
            return False
        self._state = RunState.STARTING
        return True

    def stop(self) -> bool:
        if self._state not in (RunState.STARTING, RunState.RUNNING):
            return False
        self._state = RunState.STOPPING
        return True

    def emergency_stop(self) -> None:
        self._state = RunState.EMERGENCY_STOP

    def reset(self) -> bool:
        """Clear a latched emergency stop. The belt must be stationary, and it stays stopped afterwards."""
        if self._state is not RunState.EMERGENCY_STOP or self._motor > 0 or self._belt > 0:
            return False
        self._state = RunState.STOPPED
        return True

    def set_speed(self, speed: float) -> None:
        if not math.isfinite(speed):
            raise ValueError(f"speed must be a finite number, got {speed}")
        self._setpoint = min(max(speed, 0.0), self._cfg.max_speed)

    def inject(self, fault: Fault, active: bool) -> None:
        if fault & Fault.EMERGENCY_STOP:
            raise ValueError("use emergency_stop(); it is a latched command, not an injected fault")
        if active:
            self._injected |= fault
        else:
            self._injected &= ~fault

    def step(self, dt: float) -> None:
        if dt <= 0:
            raise ValueError("dt must be positive")
        self._update_speeds(dt)
        self._update_feed(dt)
        self._update_temperature(dt)
        self._update_state()

    def _update_speeds(self, dt: float) -> None:
        cfg = self._cfg
        driving = self._state in (RunState.STARTING, RunState.RUNNING)
        target = self._setpoint if driving else 0.0
        rate = cfg.estop_deceleration if self._state is RunState.EMERGENCY_STOP else cfg.acceleration
        self._motor = _move_toward(self._motor, target, rate * dt)

        slip = cfg.slip_ratio if self._injected & Fault.BELT_SLIP else 0.0
        self._belt = _move_toward(self._belt, self._motor * (1 - slip), cfg.belt_response * dt)

    def _update_feed(self, dt: float) -> None:
        # Mean-reverting random walk: upstream supply wanders around nominal but does not drift away.
        cfg = self._cfg
        shock = cfg.feed_sigma * math.sqrt(dt) * self._process_rng.gauss(0, 1)
        feed = self._feed + cfg.feed_reversion * (1 - self._feed) * dt + shock
        self._feed = min(max(feed, FEED_LIMITS[0]), FEED_LIMITS[1])

    def _update_temperature(self, dt: float) -> None:
        # First-order thermal model, solved exactly so the result does not depend on the step size.
        cfg = self._cfg
        normal_cooling = 1 / cfg.thermal_time_constant
        heat = normal_cooling * cfg.temp_rise_per_speed * self._motor
        cooling = normal_cooling
        if self._injected & Fault.BEARING_OVERHEAT:
            cooling *= cfg.overheat_cooling_factor
        steady = cfg.ambient_temp + heat / cooling
        self._temp = steady + (self._temp - steady) * math.exp(-cooling * dt)

    def _update_state(self) -> None:
        if self._state is RunState.STARTING and abs(self._motor - self._setpoint) < SPEED_EPSILON:
            self._state = RunState.RUNNING
        elif self._state is RunState.STOPPING and self._motor == 0 and self._belt == 0:
            self._state = RunState.STOPPED

    def _material_flow(self) -> float:
        cfg = self._cfg
        flow = cfg.nominal_flow * (self._belt / cfg.nominal_speed) * self._feed
        return flow * cfg.overload_factor if self._injected & Fault.OVERLOAD else flow

    def _motor_current(self, flow: float) -> float:
        # Lifting power depends on tonnes per hour, friction of the empty belt on speed.
        cfg = self._cfg
        if self._motor == 0:
            return 0.0
        return cfg.no_load_current + cfg.current_per_speed * self._motor + cfg.current_per_flow * flow

    def _vibration(self) -> float:
        cfg = self._cfg
        if self._motor == 0:
            return 0.0
        wear = cfg.vibration_per_degree * max(0.0, self._temp - cfg.vibration_onset_temp)
        return cfg.vibration_per_speed * self._motor + wear

    def _sensor(self, value: float, sigma: float) -> float:
        return max(0.0, value + self._sensor_rng.gauss(0, sigma))

    def readings(self) -> Readings:
        """Sensor values with noise. A stationary machine reads exactly zero, as a real drive would report."""
        cfg = self._cfg
        flow = self._material_flow()
        moving = self._motor > 0 or self._belt > 0
        sensor = self._sensor if moving else lambda value, _sigma: value
        return Readings(
            belt_speed=sensor(self._belt, cfg.noise_speed),
            motor_speed=sensor(self._motor, cfg.noise_speed),
            motor_current=sensor(self._motor_current(flow), cfg.noise_current),
            bearing_temp=self._sensor(self._temp, cfg.noise_temp),
            vibration=sensor(self._vibration(), cfg.noise_vibration),
            material_flow=flow,
            run_state=int(self._state),
            fault_code=int(self.faults),
        )
