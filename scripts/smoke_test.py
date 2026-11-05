"""Check a running system from the outside: python scripts/smoke_test.py [http://host:port]

Standard library only, so the same check runs against `python run.py` and against docker compose.
"""

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT = 60  # seconds, for each step; a container build can leave the services slow to come up
TEST_SPEED = 1.0  # m/s; low, so the belt reaches it in a couple of seconds


def request(base: str, path: str, body: dict | None = None, method: str = "GET"):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        raw = response.read()
    return json.loads(raw) if path.startswith("/api/") else raw.decode("utf-8")


def wait_for(base: str, description: str, predicate) -> dict:
    deadline = time.monotonic() + TIMEOUT
    state = None
    while time.monotonic() < deadline:
        try:
            state = request(base, "/api/state")
            if predicate(state):
                print(f"ok: {description}", flush=True)
                return state
        except (urllib.error.URLError, OSError):
            pass  # not up yet
        time.sleep(0.5)
    raise SystemExit(f"FAILED: {description}\nlast state: {state}")


def has_active_alarm(state: dict, name: str) -> bool:
    return any(a["name"] == name and a["state"].startswith("active") for a in state["alarms"])


def run(base: str) -> None:
    wait_for(base, "HMI is talking to the plant", lambda s: s["connected"] and s["run_state"] == "stopped")

    if "Conveyor HMI" not in request(base, "/"):
        raise SystemExit("FAILED: the web page is not being served")
    print("ok: web page is served", flush=True)

    request(base, "/api/speed", {"speed": TEST_SPEED}, "POST")
    request(base, "/api/commands/start", method="POST")
    wait_for(base, "belt runs at the requested speed",
             lambda s: s["run_state"] == "running" and abs(s["readings"]["belt_speed"] - TEST_SPEED) < 0.1)

    request(base, "/api/commands/estop", method="POST")
    wait_for(base, "emergency stop raises its alarm", lambda s: has_active_alarm(s, "emergency_stop"))
    wait_for(base, "belt comes to rest", lambda s: s["readings"]["belt_speed"] == 0)

    request(base, "/api/commands/reset", method="POST")
    wait_for(base, "reset clears the alarm and does not restart", lambda s: s["run_state"] == "stopped"
             and not has_active_alarm(s, "emergency_stop"))

    actions = [o["action"] for o in request(base, "/api/operations")]
    if "command:estop" not in actions:
        raise SystemExit(f"FAILED: emergency stop is missing from the operator log: {actions}")
    print("ok: operations are in the log", flush=True)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)
    print("smoke test passed")
