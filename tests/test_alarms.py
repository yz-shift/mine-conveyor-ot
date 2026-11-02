from dataclasses import replace

import pytest

from hmi.alarms import (
    AlarmDefinition,
    AlarmEngine,
    AlarmState,
    EventKind,
    Severity,
    derive_signals,
    load_alarm_config,
)
from shared.registers import load_register_map

REGISTERS = load_register_map()
TEMP = AlarmDefinition("temp_high", "temp", Severity.WARNING, threshold=80, deadband=5, delay=3, message="Hot")
NO_DELAY = replace(TEMP, delay=0)


def kinds(events):
    return [e.kind for e in events]


def run(engine, value, now, signal="temp"):
    return engine.update({signal: value}, now, 1000 + now)


def state(engine):
    visible = engine.visible()
    return visible[0].state if visible else AlarmState.NORMAL


# --- Trigger and delay ---

def test_below_threshold_stays_normal():
    engine = AlarmEngine([NO_DELAY])
    assert run(engine, 80, 0) == []
    assert state(engine) is AlarmState.NORMAL


def test_raises_above_threshold_with_no_delay():
    engine = AlarmEngine([NO_DELAY])
    [event] = run(engine, 80.1, 0)
    assert event.kind is EventKind.RAISED
    assert event.alarm == "temp_high"
    assert event.severity is Severity.WARNING
    assert event.value == 80.1
    assert event.timestamp == 1000
    assert state(engine) is AlarmState.ACTIVE_UNACKED


def test_does_not_raise_before_the_delay_has_passed():
    engine = AlarmEngine([TEMP])
    assert run(engine, 90, 0) == []
    assert run(engine, 90, 2.9) == []
    assert kinds(run(engine, 90, 3.0)) == [EventKind.RAISED]


def test_dip_below_threshold_restarts_the_delay():
    engine = AlarmEngine([TEMP])
    run(engine, 90, 0)
    run(engine, 90, 2)
    run(engine, 79, 2.5)
    assert run(engine, 90, 3) == []
    assert run(engine, 90, 5.9) == []
    assert kinds(run(engine, 90, 6)) == [EventKind.RAISED]


def test_raises_only_once_while_it_stays_high():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    assert run(engine, 90, 1) == []
    assert run(engine, 95, 2) == []


# --- Deadband and clearing ---

def test_stays_active_inside_the_deadband():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    assert run(engine, 79, 1) == []
    assert run(engine, 75.1, 2) == []
    assert state(engine) is AlarmState.ACTIVE_UNACKED


def test_clears_at_the_bottom_of_the_deadband():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    [event] = run(engine, 75, 1)
    assert event.kind is EventKind.CLEARED
    assert event.value == 75


def test_noise_around_the_threshold_gives_one_raise_and_no_chatter():
    engine = AlarmEngine([NO_DELAY])
    events = []
    for i, value in enumerate([81, 79, 81, 79, 81, 79, 81]):
        events += run(engine, value, i)
    assert kinds(events) == [EventKind.RAISED]


# --- Acknowledgement ---

def test_acknowledging_keeps_an_active_alarm_visible():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    event = engine.acknowledge("temp_high", 2000)
    assert event.kind is EventKind.ACKNOWLEDGED
    assert event.timestamp == 2000
    assert state(engine) is AlarmState.ACTIVE_ACKED


def test_acknowledged_alarm_disappears_when_it_clears():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    engine.acknowledge("temp_high", 2000)
    assert kinds(run(engine, 70, 1)) == [EventKind.CLEARED]
    assert engine.visible() == []


def test_unacknowledged_alarm_stays_visible_after_it_clears():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    run(engine, 70, 1)
    assert state(engine) is AlarmState.CLEARED_UNACKED
    assert engine.acknowledge("temp_high", 2000).kind is EventKind.ACKNOWLEDGED
    assert engine.visible() == []


def test_cleared_unacknowledged_alarm_raises_again_if_it_returns():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    run(engine, 70, 1)
    assert kinds(run(engine, 90, 2)) == [EventKind.RAISED]
    assert state(engine) is AlarmState.ACTIVE_UNACKED


def test_acknowledging_twice_does_nothing_the_second_time():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    engine.acknowledge("temp_high", 2000)
    assert engine.acknowledge("temp_high", 2001) is None


def test_acknowledging_a_normal_alarm_does_nothing():
    assert AlarmEngine([NO_DELAY]).acknowledge("temp_high", 2000) is None


def test_acknowledging_an_unknown_alarm_raises():
    with pytest.raises(KeyError):
        AlarmEngine([NO_DELAY]).acknowledge("nope", 2000)


def test_full_cycle_can_repeat():
    engine = AlarmEngine([NO_DELAY])
    for start in (0, 10):
        assert kinds(run(engine, 90, start)) == [EventKind.RAISED]
        engine.acknowledge("temp_high", 2000)
        assert kinds(run(engine, 70, start + 1)) == [EventKind.CLEARED]
        assert engine.visible() == []


# --- Missing signals ---

def test_missing_signal_holds_an_active_alarm():
    engine = AlarmEngine([NO_DELAY])
    run(engine, 90, 0)
    assert engine.update({}, 1, 1001) == []
    assert state(engine) is AlarmState.ACTIVE_UNACKED


def test_missing_signal_restarts_the_delay():
    engine = AlarmEngine([TEMP])
    run(engine, 90, 0)
    engine.update({}, 2, 1002)
    assert run(engine, 90, 3) == []
    assert kinds(run(engine, 90, 6)) == [EventKind.RAISED]


# --- Ordering ---

def test_critical_alarms_come_first_then_newest():
    critical = AlarmDefinition("c", "c", Severity.CRITICAL, 1, 0, 0, "")
    old = AlarmDefinition("old", "a", Severity.WARNING, 1, 0, 0, "")
    new = AlarmDefinition("new", "b", Severity.WARNING, 1, 0, 0, "")
    engine = AlarmEngine([old, new, critical])
    engine.update({"a": 2}, 0, 100)
    engine.update({"b": 2}, 1, 200)
    engine.update({"c": 2}, 2, 150)
    assert [s.name for s in engine.visible()] == ["c", "new", "old"]


# --- Derived signals ---

READINGS = dict(belt_speed=2.8, motor_speed=4.0, motor_current=100.0, bearing_temp=30.0,
                vibration=1.0, material_flow=100.0, run_state=2, fault_code=0)


def derive(**changes):
    return derive_signals({**READINGS, **changes}, REGISTERS, slip_min_speed=0.5)


def test_slip_ratio_is_the_fraction_of_speed_lost():
    assert derive()["slip_ratio"] == pytest.approx(0.3)


def test_slip_ratio_is_zero_below_the_minimum_speed():
    assert derive(motor_speed=0.4, belt_speed=0.1)["slip_ratio"] == 0


def test_slip_ratio_is_not_negative_when_noise_puts_the_belt_ahead():
    assert derive(belt_speed=4.1)["slip_ratio"] == 0


def test_emergency_stop_signal_reads_only_its_own_bit():
    assert derive(fault_code=8)["emergency_stop"] == 1
    assert derive(fault_code=12)["emergency_stop"] == 1
    assert derive(fault_code=7)["emergency_stop"] == 0


def test_register_values_pass_through():
    signals = derive()
    assert signals["bearing_temp"] == 30
    assert signals["comm_lost"] == 0


# --- Configuration ---

def write_config(tmp_path, alarm):
    path = tmp_path / "alarms.yaml"
    path.write_text(f"slip_min_speed: 0.5\nalarms:\n  - {alarm}\n", encoding="utf-8")
    return path


def test_shipped_config_loads():
    config = load_alarm_config(REGISTERS)
    assert len(config.alarms) == 12
    assert {a.severity for a in config.alarms} == {Severity.WARNING, Severity.CRITICAL}


def test_shipped_deadbands_can_actually_clear():
    # A signal that reads zero when the belt stops must get below threshold - deadband.
    for alarm in load_alarm_config(REGISTERS).alarms:
        assert alarm.threshold - alarm.deadband >= 0, alarm.name


def test_unknown_signal_is_rejected(tmp_path):
    path = write_config(tmp_path, "{name: a, signal: nope, severity: warning, threshold: 1, deadband: 0, delay: 0, message: m}")
    with pytest.raises(ValueError, match="unknown signal"):
        load_alarm_config(REGISTERS, path)


def test_unknown_severity_is_rejected(tmp_path):
    path = write_config(tmp_path, "{name: a, signal: vibration, severity: huge, threshold: 1, deadband: 0, delay: 0, message: m}")
    with pytest.raises(ValueError, match="huge"):
        load_alarm_config(REGISTERS, path)


def test_negative_deadband_is_rejected(tmp_path):
    path = write_config(tmp_path, "{name: a, signal: vibration, severity: warning, threshold: 1, deadband: -1, delay: 0, message: m}")
    with pytest.raises(ValueError, match="deadband"):
        load_alarm_config(REGISTERS, path)


def test_duplicate_alarm_name_is_rejected(tmp_path):
    alarm = "{name: a, signal: vibration, severity: warning, threshold: 1, deadband: 0, delay: 0, message: m}"
    path = write_config(tmp_path, f"{alarm}\n  - {alarm}")
    with pytest.raises(ValueError, match="duplicate"):
        load_alarm_config(REGISTERS, path)
