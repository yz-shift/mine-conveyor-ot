import math
from dataclasses import fields, replace

import pytest

from shared.registers import load_register_map
from simulator.plant import (
    ConveyorPlant,
    Fault,
    PlantConfig,
    Readings,
    RunState,
    load_plant_config,
)

DT = 0.1


def quiet_config() -> PlantConfig:
    """Real parameters without randomness, so tests can compare against exact numbers."""
    return replace(
        load_plant_config(),
        feed_sigma=0, noise_speed=0, noise_current=0, noise_temp=0, noise_vibration=0,
    )


def run(plant: ConveyorPlant, seconds: float, dt: float = DT) -> None:
    for _ in range(round(seconds / dt)):
        plant.step(dt)


@pytest.fixture
def cfg():
    return quiet_config()


@pytest.fixture
def plant(cfg):
    return ConveyorPlant(cfg, seed=1)


@pytest.fixture
def running(plant, cfg):
    plant.start()
    run(plant, cfg.nominal_speed / cfg.acceleration + 1)
    assert plant.state is RunState.RUNNING
    return plant


# --- start, stop, speed ------------------------------------------------------------------------

def test_new_plant_is_stopped_at_ambient(plant, cfg):
    r = plant.readings()
    assert plant.state is RunState.STOPPED
    assert (r.belt_speed, r.motor_speed, r.motor_current, r.vibration, r.material_flow) == (0, 0, 0, 0, 0)
    assert r.bearing_temp == cfg.ambient_temp


def test_stopped_plant_stays_still(plant):
    run(plant, 30)
    assert plant.readings().motor_speed == 0


def test_speed_ramps_at_the_acceleration_limit(plant, cfg):
    plant.start()
    run(plant, 2)
    assert plant.state is RunState.STARTING
    assert plant.readings().motor_speed == pytest.approx(2 * cfg.acceleration)


def test_speed_never_jumps_between_steps(plant, cfg):
    plant.start()
    previous = 0.0
    for _ in range(200):
        plant.step(DT)
        current = plant.readings().motor_speed
        assert abs(current - previous) <= cfg.acceleration * DT + 1e-9
        previous = current


def test_reaches_running_exactly_at_setpoint(plant, cfg):
    plant.start()
    run(plant, cfg.nominal_speed / cfg.acceleration + DT)
    assert plant.state is RunState.RUNNING
    assert plant.readings().motor_speed == cfg.nominal_speed


def test_setpoint_change_while_running_ramps_to_new_speed(running, cfg):
    running.set_speed(2.0)
    run(running, 1)
    assert running.readings().motor_speed == pytest.approx(cfg.nominal_speed - cfg.acceleration)
    run(running, 10)
    assert running.readings().motor_speed == 2.0
    assert running.state is RunState.RUNNING


@pytest.mark.parametrize("requested, expected", [(99, 6.0), (-3, 0.0), (3.5, 3.5)])
def test_setpoint_is_clamped_to_the_machine_limits(plant, requested, expected):
    plant.set_speed(requested)
    assert plant.setpoint == expected


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_setpoint_rejects_non_finite_values(plant, bad):
    with pytest.raises(ValueError):
        plant.set_speed(bad)


def test_controlled_stop_ramps_down_then_stops(running, cfg):
    assert running.stop() is True
    assert running.state is RunState.STOPPING
    run(running, 1)
    assert running.readings().motor_speed == pytest.approx(cfg.nominal_speed - cfg.acceleration)
    run(running, cfg.nominal_speed / cfg.acceleration + 1)
    assert running.state is RunState.STOPPED
    assert running.readings().motor_current == 0


def test_start_is_refused_unless_stopped(running):
    assert running.start() is False
    running.stop()
    assert running.start() is False


def test_stop_is_refused_when_already_stopped(plant):
    assert plant.stop() is False
    assert plant.state is RunState.STOPPED


def test_step_rejects_non_positive_time(plant):
    with pytest.raises(ValueError):
        plant.step(0)


# --- emergency stop ----------------------------------------------------------------------------

def test_emergency_stop_is_faster_than_a_controlled_stop(running, cfg):
    running.emergency_stop()
    run(running, 1)
    assert running.readings().motor_speed == pytest.approx(cfg.nominal_speed - cfg.estop_deceleration)
    assert running.readings().fault_code & Fault.EMERGENCY_STOP


def test_emergency_stop_latches_after_the_belt_stops(running):
    running.emergency_stop()
    run(running, 30)
    assert running.readings().motor_speed == 0
    assert running.state is RunState.EMERGENCY_STOP


def test_start_is_refused_until_reset(running):
    running.emergency_stop()
    run(running, 30)
    assert running.start() is False
    assert running.stop() is False
    run(running, 5)
    assert running.readings().motor_speed == 0


def test_reset_is_refused_while_still_moving(running):
    running.emergency_stop()
    running.step(DT)
    assert running.reset() is False
    assert running.state is RunState.EMERGENCY_STOP


def test_reset_does_not_restart_the_belt(running):
    running.emergency_stop()
    run(running, 30)
    assert running.reset() is True
    assert running.state is RunState.STOPPED
    assert not running.readings().fault_code & Fault.EMERGENCY_STOP
    run(running, 5)
    assert running.readings().motor_speed == 0


def test_can_start_again_after_reset(running, cfg):
    running.emergency_stop()
    run(running, 30)
    running.reset()
    assert running.start() is True
    run(running, cfg.nominal_speed / cfg.acceleration + 1)
    assert running.state is RunState.RUNNING


def test_reset_is_refused_without_an_emergency_stop(plant):
    assert plant.reset() is False


def test_emergency_stop_cannot_be_injected_as_a_fault(plant):
    with pytest.raises(ValueError):
        plant.inject(Fault.EMERGENCY_STOP, True)


# --- injected faults ---------------------------------------------------------------------------

def test_normal_running_values_match_the_model(running, cfg):
    r = running.readings()
    assert r.material_flow == pytest.approx(cfg.nominal_flow)
    expected_current = (cfg.no_load_current + cfg.current_per_speed * cfg.nominal_speed
                        + cfg.current_per_flow * cfg.nominal_flow)
    assert r.motor_current == pytest.approx(expected_current)
    assert r.fault_code == 0


def test_overload_raises_flow_and_current_then_clears(running, cfg):
    normal = running.readings()
    running.inject(Fault.OVERLOAD, True)
    running.step(DT)
    overloaded = running.readings()
    assert overloaded.material_flow == pytest.approx(cfg.nominal_flow * cfg.overload_factor)
    assert overloaded.motor_current > normal.motor_current
    assert overloaded.fault_code == Fault.OVERLOAD

    running.inject(Fault.OVERLOAD, False)
    running.step(DT)
    assert running.readings().motor_current == pytest.approx(normal.motor_current)
    assert running.readings().fault_code == 0


def test_bearing_settles_at_its_normal_temperature(running, cfg):
    run(running, 1000)
    expected = cfg.ambient_temp + cfg.temp_rise_per_speed * cfg.nominal_speed
    assert running.readings().bearing_temp == pytest.approx(expected, abs=1e-3)
    assert running.readings().vibration == pytest.approx(cfg.vibration_per_speed * cfg.nominal_speed)


def test_bearing_overheat_keeps_heating_and_raises_vibration(running, cfg):
    run(running, 1000)
    normal = running.readings()
    running.inject(Fault.BEARING_OVERHEAT, True)
    run(running, 120)
    hot = running.readings()
    assert hot.bearing_temp > normal.bearing_temp + 20
    assert hot.bearing_temp > cfg.vibration_onset_temp
    assert hot.vibration > normal.vibration
    assert hot.fault_code == Fault.BEARING_OVERHEAT


def test_bearing_recovers_when_the_fault_clears(running):
    run(running, 1000)
    normal = running.readings().bearing_temp
    running.inject(Fault.BEARING_OVERHEAT, True)
    run(running, 120)
    running.inject(Fault.BEARING_OVERHEAT, False)
    run(running, 1000)
    assert running.readings().bearing_temp == pytest.approx(normal, abs=1e-3)


def test_bearing_cools_to_ambient_after_stopping(running, cfg):
    run(running, 200)
    running.stop()
    run(running, 1000)
    assert running.readings().bearing_temp == pytest.approx(cfg.ambient_temp, abs=1e-3)


def test_stopped_machine_does_not_vibrate_while_the_bearing_is_still_hot(running, cfg):
    running.inject(Fault.BEARING_OVERHEAT, True)
    run(running, 120)
    running.stop()
    run(running, cfg.nominal_speed / cfg.acceleration + 1)
    r = running.readings()
    assert r.bearing_temp > cfg.vibration_onset_temp
    assert r.vibration == 0


def test_temperature_does_not_depend_on_the_step_size(cfg):
    fine, coarse = ConveyorPlant(cfg), ConveyorPlant(cfg)
    for plant in (fine, coarse):
        plant.start()
    run(fine, 100, dt=0.1)
    run(coarse, 100, dt=0.1)
    fine.inject(Fault.BEARING_OVERHEAT, True)
    coarse.inject(Fault.BEARING_OVERHEAT, True)
    run(fine, 50, dt=0.01)
    run(coarse, 50, dt=5.0)
    assert fine.readings().bearing_temp == pytest.approx(coarse.readings().bearing_temp)


def test_belt_slip_leaves_the_belt_behind_the_motor(running, cfg):
    running.inject(Fault.BELT_SLIP, True)
    run(running, 5)
    r = running.readings()
    assert r.motor_speed == cfg.nominal_speed
    assert r.belt_speed == pytest.approx(cfg.nominal_speed * (1 - cfg.slip_ratio))
    assert r.material_flow < cfg.nominal_flow
    assert r.fault_code == Fault.BELT_SLIP


def test_belt_catches_up_when_slip_clears(running, cfg):
    running.inject(Fault.BELT_SLIP, True)
    run(running, 5)
    running.inject(Fault.BELT_SLIP, False)
    run(running, 5)
    assert running.readings().belt_speed == cfg.nominal_speed


def test_several_faults_combine_in_the_bit_field(running):
    running.inject(Fault.OVERLOAD, True)
    running.inject(Fault.BELT_SLIP, True)
    running.emergency_stop()
    assert running.readings().fault_code == Fault.OVERLOAD | Fault.BELT_SLIP | Fault.EMERGENCY_STOP


# --- randomness --------------------------------------------------------------------------------

def noisy_trace(seed, poll_every=1):
    plant = ConveyorPlant(load_plant_config(), seed=seed)
    plant.start()
    trace = []
    for i in range(200):
        plant.step(DT)
        if i % poll_every == 0:
            plant.readings()
        trace.append(plant._material_flow())
    return trace


def test_same_seed_gives_the_same_run():
    assert noisy_trace(7) == noisy_trace(7)


def test_different_seeds_give_different_runs():
    assert noisy_trace(7) != noisy_trace(8)


def test_polling_rate_does_not_change_the_plant():
    assert noisy_trace(7, poll_every=1) == noisy_trace(7, poll_every=13)


def test_noise_is_visible_but_small(cfg):
    plant = ConveyorPlant(load_plant_config(), seed=3)
    plant.start()
    run(plant, 20)
    samples = [plant.readings().motor_speed for _ in range(200)]
    assert len(set(samples)) > 1
    assert all(abs(s - cfg.nominal_speed) < 0.1 for s in samples)


def test_stationary_machine_reads_exactly_zero():
    plant = ConveyorPlant(load_plant_config(), seed=3)
    for _ in range(50):
        r = plant.readings()
        assert (r.belt_speed, r.motor_speed, r.motor_current, r.vibration) == (0, 0, 0, 0)


def test_long_run_with_every_fault_stays_sane():
    registers = load_register_map()
    plant = ConveyorPlant(load_plant_config(), seed=5)
    plant.set_speed(6)
    plant.start()
    for fault in (Fault.OVERLOAD, Fault.BEARING_OVERHEAT, Fault.BELT_SLIP):
        plant.inject(fault, True)
    for _ in range(36000):
        plant.step(DT)
        r = plant.readings()
        for f in fields(Readings):
            value = getattr(r, f.name)
            assert math.isfinite(value) and value >= 0
            assert 0 <= registers[f.name].encode(value) <= 0xFFFF


# --- contract with the register map ------------------------------------------------------------

def test_reading_fields_match_the_input_registers():
    names = {p.name for p in load_register_map().input_registers}
    assert {f.name for f in fields(Readings)} == names


def test_run_states_match_the_register_labels():
    labels = load_register_map()["run_state"].labels
    assert {int(s): s.name.lower() for s in RunState} == labels


def test_fault_bits_match_the_register_labels():
    labels = load_register_map()["fault_code"].labels
    assert {int(f): f.name.lower() for f in Fault} == labels


def test_machine_limit_fits_the_setpoint_register(cfg):
    point = load_register_map()["speed_setpoint"]
    assert cfg.max_speed <= point.max


# --- configuration -----------------------------------------------------------------------------

def test_missing_config_key_is_reported(tmp_path):
    path = tmp_path / "plant.yaml"
    path.write_text("nominal_speed: 4.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plant.yaml"):
        load_plant_config(path)


@pytest.mark.parametrize("change", [
    {"acceleration": 0},
    {"thermal_time_constant": 0},
    {"overheat_cooling_factor": 0},
    {"noise_temp": -1},
    {"slip_ratio": 1},
    {"nominal_speed": 9},
])
def test_invalid_config_is_rejected(change):
    with pytest.raises(ValueError):
        replace(load_plant_config(), **change)
