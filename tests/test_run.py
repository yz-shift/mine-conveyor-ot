"""run.py as a user runs it: a real process tree on real ports."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"


def free_ports(count: int) -> list[int]:
    """Distinct free ports. All sockets stay open until every port is chosen; closing each one at once
    lets the system hand the same port out twice, which made CI fail once with "address already in use"."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for s in sockets:
            s.bind((HOST, 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


def free_port() -> int:
    return free_ports(1)[0]


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        # On Linux this ignores connections in TIME_WAIT but is still refused by a live listener.
        # On Windows it would succeed even against a listener, so it is left off there.
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def wait_until_free(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_free(port):
            return True
        time.sleep(0.1)
    return False


class System:
    def __init__(self, tmp_path) -> None:
        self.modbus_port, self.http_port = free_ports(2)
        self.env = {**os.environ, "MODBUS_HOST": HOST, "MODBUS_PORT": str(self.modbus_port), "HMI_HOST": HOST,
                    "HMI_PORT": str(self.http_port), "HMI_DB_PATH": str(tmp_path / "hmi.sqlite3")}

    def start(self) -> subprocess.Popen:
        # A process group of its own, so the test can signal it without also signalling pytest.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        return subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=self.env, creationflags=flags,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

    def smoke(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "scripts/smoke_test.py", f"http://{HOST}:{self.http_port}"], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def ask_to_stop(process: subprocess.Popen) -> None:
    process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)


@pytest.fixture
def system(tmp_path):
    return System(tmp_path)


def test_one_command_brings_up_a_working_system_and_stops_it_cleanly(system):
    process = system.start()
    try:
        result = system.smoke()
        assert result.returncode == 0, result.stdout + result.stderr
        assert process.poll() is None
    finally:
        ask_to_stop(process)
        output, _ = process.communicate(timeout=15)
    assert process.returncode == 0, output
    assert f"http://{HOST}:{system.http_port}" in output
    assert wait_until_free(system.modbus_port), "simulator was left running"
    assert wait_until_free(system.http_port), "HMI was left running"


def test_if_the_simulator_cannot_start_the_hmi_is_stopped_too(system):
    with socket.socket() as squatter:
        squatter.bind((HOST, system.modbus_port))
        squatter.listen()
        process = system.start()
        output, _ = process.communicate(timeout=30)
    assert process.returncode != 0, output
    assert "simulator.server exited" in output
    assert wait_until_free(system.http_port), "HMI was left running"


def test_if_the_hmi_cannot_start_the_simulator_is_stopped_too(system):
    with socket.socket() as squatter:
        squatter.bind((HOST, system.http_port))
        squatter.listen()
        process = system.start()
        output, _ = process.communicate(timeout=30)
    assert process.returncode != 0, output
    assert "hmi.app exited" in output
    assert wait_until_free(system.modbus_port), "simulator was left running"


def test_smoke_test_fails_when_nothing_is_running(system, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import smoke_test

    monkeypatch.setattr(smoke_test, "TIMEOUT", 0.5)
    with pytest.raises(SystemExit, match="FAILED"):
        smoke_test.run(f"http://{HOST}:{system.http_port}")
