"""Start the simulator and the HMI together, and stop both when either one stops: python run.py"""

import signal
import subprocess
import sys
import time
from pathlib import Path

from shared.settings import load_settings

ROOT = Path(__file__).resolve().parent
MODULES = ("simulator.server", "hmi.app")
WATCH_PERIOD = 0.2  # seconds
STOP_TIMEOUT = 5  # seconds


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def watch(processes: list[subprocess.Popen]) -> int:
    """Block until any process exits and return an exit code for the whole system. Half a system
    is never useful: an HMI with no plant only shows an outage, and a plant with no HMI is unobserved."""
    while True:
        for process in processes:
            code = process.poll()
            if code is not None:
                print(f"{process.args[-1]} exited with code {code}, stopping everything", file=sys.stderr)
                return code or 1  # neither process should ever finish by itself
        time.sleep(WATCH_PERIOD)


def stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    # Ctrl+Break on Windows and SIGTERM elsewhere would otherwise end this process without stopping the children.
    signal.signal(signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM, _interrupt)
    processes: list[subprocess.Popen] = []
    try:
        processes.extend(subprocess.Popen([sys.executable, "-m", module], cwd=ROOT) for module in MODULES)
        settings = load_settings()
        print(f"HMI: http://{settings.hmi_host}:{settings.hmi_port}  (Ctrl+C to stop)")
        return watch(processes)
    except KeyboardInterrupt:
        return 0
    finally:
        stop(processes)


if __name__ == "__main__":
    sys.exit(main())
