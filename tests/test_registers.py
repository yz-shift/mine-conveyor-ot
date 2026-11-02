import pytest

from shared.registers import RegisterMapError, load_register_map


def write_map(tmp_path, body):
    path = tmp_path / "registers.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_project_map_loads():
    registers = load_register_map()
    assert registers["belt_speed"].table == "input_registers"
    assert registers["speed_setpoint"].table == "holding_registers"
    assert registers["cmd_estop"].table == "coils"


def test_project_map_has_every_required_point():
    registers = load_register_map()
    required = [
        "belt_speed", "motor_current", "bearing_temp", "vibration", "material_flow",
        "speed_setpoint", "cmd_start", "cmd_stop", "cmd_estop",
        "inject_overload", "inject_bearing_overheat", "inject_belt_slip",
    ]
    for name in required:
        assert registers[name].name == name


def test_duplicate_address_in_one_table_is_rejected(tmp_path):
    path = write_map(tmp_path, """
input_registers:
  - {name: a, address: 0, max: 10}
  - {name: b, address: 0, max: 10}
""")
    with pytest.raises(RegisterMapError, match="duplicate address 0"):
        load_register_map(path)


def test_same_address_in_different_tables_is_allowed(tmp_path):
    path = write_map(tmp_path, """
input_registers:
  - {name: a, address: 0, max: 10}
holding_registers:
  - {name: b, address: 0, max: 10}
coils:
  - {name: c, address: 0}
""")
    assert load_register_map(path)["c"].address == 0


def test_duplicate_name_across_tables_is_rejected(tmp_path):
    path = write_map(tmp_path, """
input_registers:
  - {name: a, address: 0, max: 10}
coils:
  - {name: a, address: 1}
""")
    with pytest.raises(RegisterMapError, match="duplicate point name"):
        load_register_map(path)


@pytest.mark.parametrize("point", [
    "{name: a, address: 70000, max: 10}",
    "{name: a, address: -1, max: 10}",
    "{name: a, address: 0, max: 10, scale: 0}",
    "{name: a, address: 0, min: 5, max: 5}",
    "{name: a, address: 0, min: -1, max: 5}",
    "{name: a, address: 0, max: 700, scale: 100}",
])
def test_invalid_points_are_rejected(tmp_path, point):
    path = write_map(tmp_path, f"input_registers:\n  - {point}\n")
    with pytest.raises(RegisterMapError):
        load_register_map(path)


def test_missing_field_names_the_problem(tmp_path):
    path = write_map(tmp_path, "coils:\n  - {name: a}\n")
    with pytest.raises(RegisterMapError, match="address"):
        load_register_map(path)


def test_fixed_point_round_trip():
    point = load_register_map()["bearing_temp"]
    assert point.encode(72.34) == 723
    assert point.decode(723) == pytest.approx(72.3)


def test_encode_saturates_at_the_range_limits():
    point = load_register_map()["bearing_temp"]
    assert point.encode(999) == 2000
    assert point.encode(-5) == 0


def test_decode_rejects_values_outside_uint16():
    point = load_register_map()["bearing_temp"]
    with pytest.raises(ValueError):
        point.decode(70000)
