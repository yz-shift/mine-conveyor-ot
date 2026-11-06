"""SQLite record of alarm events and operator actions. Append only; timestamps are Unix seconds.

Calls run on the event loop. Each is one small local write, and keeping a single thread
means the connection needs no locking.
"""

import sqlite3
from pathlib import Path

from hmi.alarms import AlarmEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_events (
    id INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    alarm TEXT NOT NULL,
    severity TEXT NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT NOT NULL
);
"""


class Logbook:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def record_alarm(self, event: AlarmEvent) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO alarm_events (timestamp, alarm, severity, kind, value, message) VALUES (?, ?, ?, ?, ?, ?)",
                (event.timestamp, event.alarm, event.severity, event.kind, event.value, event.message),
            )

    def record_operation(self, timestamp: float, action: str, detail: str, error: str | None = None) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO operations (timestamp, action, detail, ok, error) VALUES (?, ?, ?, ?, ?)",
                (timestamp, action, detail, error is None, error or ""),
            )

    def record_acknowledgement(self, event: AlarmEvent) -> None:
        """The alarm event and the operator action in one transaction: both are written or neither is."""
        with self._db:
            self._db.execute(
                "INSERT INTO alarm_events (timestamp, alarm, severity, kind, value, message) VALUES (?, ?, ?, ?, ?, ?)",
                (event.timestamp, event.alarm, event.severity, event.kind, event.value, event.message),
            )
            self._db.execute(
                "INSERT INTO operations (timestamp, action, detail, ok, error) VALUES (?, ?, ?, ?, ?)",
                (event.timestamp, "acknowledge", event.alarm, True, ""),
            )

    def recent_alarm_events(self, limit: int = 100) -> list[dict]:
        return self._recent("alarm_events", limit)

    def recent_operations(self, limit: int = 100) -> list[dict]:
        return self._recent("operations", limit)

    def _recent(self, table: str, limit: int) -> list[dict]:
        # `table` only ever comes from the two methods above, never from a request.
        rows = self._db.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._db.close()
