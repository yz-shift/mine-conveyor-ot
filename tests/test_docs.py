"""The documents are hand written, so these tests stop them drifting away from the YAML files."""

from pathlib import Path

import pytest

from hmi.alarms import load_alarm_config
from shared.registers import load_register_map

DOCS = Path(__file__).resolve().parent.parent / "docs"
REGISTERS = load_register_map()
ALARMS = load_alarm_config(REGISTERS)


def tables(document: str) -> dict[str, list[list[str]]]:
    """Every markdown table in the document, keyed by the text of its first header cell, without backticks."""
    found: dict[str, list[list[str]]] = {}
    rows: list[list[str]] = []
    lines = (DOCS / document).read_text(encoding="utf-8").splitlines() + [""]
    for line in lines:
        if line.startswith("|"):
            rows.append([cell.strip().replace("`", "") for cell in line.strip("|").split("|")])
        elif rows:
            found.setdefault(rows[0][0], []).extend(rows[2:])
            rows = []
    return found


def point_rows() -> dict[str, list[str]]:
    return {row[0]: row for row in tables("registers.md")["Point"]}


def test_every_register_is_documented_with_the_right_numbers():
    rows = point_rows()
    for point in REGISTERS.input_registers + REGISTERS.holding_registers:
        name, address, scale, unit, value_range, _ = rows[point.name]
        assert int(address) == point.address, name
        assert int(scale) == point.scale, name
        assert unit == point.unit, name
        assert value_range == f"{point.min} to {point.max}", name


def test_every_coil_is_documented_with_the_right_address():
    rows = point_rows()
    for point in REGISTERS.coils:
        name, address, kind, _ = rows[point.name]
        assert int(address) == point.address, name
        assert kind == ("Pulse" if name.startswith("cmd_") else "Level"), name


def test_registers_doc_has_no_points_the_map_lacks():
    known = {p.name for p in REGISTERS.input_registers + REGISTERS.holding_registers + REGISTERS.coils}
    assert set(point_rows()) == known


@pytest.mark.parametrize("point, header", [("run_state", "Value"), ("fault_code", "Bit value")])
def test_state_and_fault_values_are_documented(point, header):
    documented = {int(row[0]): row[1] for row in tables("registers.md")[header]}
    assert documented == REGISTERS[point].labels


def test_every_alarm_is_documented_with_the_right_limits():
    rows = {row[0]: row for row in tables("alarms.md")["Alarm"]}
    assert set(rows) == {a.name for a in ALARMS.alarms}
    for alarm in ALARMS.alarms:
        _, signal, severity, raises, clears, delay, message = rows[alarm.name]
        assert signal == alarm.signal, alarm.name
        assert severity == alarm.severity, alarm.name
        assert float(raises) == alarm.threshold, alarm.name
        assert float(clears) == pytest.approx(alarm.threshold - alarm.deadband), alarm.name
        assert float(delay) == alarm.delay, alarm.name
        assert message == alarm.message, alarm.name


def test_slip_minimum_speed_in_the_doc_matches_the_config():
    assert f"`slip_min_speed` ({ALARMS.slip_min_speed} m/s)" in (DOCS / "alarms.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("document", ["architecture.md", "alarms.md", "registers.md", "../README.md"])
def test_files_named_in_the_docs_exist(document):
    root = DOCS.parent
    text = (DOCS / document).read_text(encoding="utf-8")
    for prefix in ("simulator/", "hmi/", "config/", "scripts/", "tests/"):
        start = 0
        while (start := text.find(prefix, start)) != -1:
            end = start
            while text[end].isalnum() or text[end] in "/._":
                end += 1
            path = text[start:end].rstrip(".")
            assert (root / path).exists(), f"{document} names {path}"
            start = end


def test_architecture_doc_states_the_timing_the_code_uses():
    from hmi.service import POLL_PERIOD
    from simulator.server import SCAN_PERIOD

    text = (DOCS / "architecture.md").read_text(encoding="utf-8")
    assert f"Scan cycle, {round(SCAN_PERIOD * 1000)} ms" in text
    assert f"Poll loop, {round(POLL_PERIOD)} s" in text
