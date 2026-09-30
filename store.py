"""SQLite storage for VM Pilot: settings, workflow runs, events, schedules.

Thread-safe via a single lock. Timestamps are stored as UTC ISO strings.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    run_number INTEGER,
    name TEXT,
    status TEXT,
    conclusion TEXT,
    event TEXT,
    created_at TEXT,
    started_at TEXT,
    updated_at TEXT,
    html_url TEXT,
    head_branch TEXT,
    head_sha TEXT,
    duration_sec INTEGER,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    created_at TEXT NOT NULL
);
"""

DEFAULTS = {
    "repo_owner": "Experian009",
    "repo_name": "",
    "workflow_file": "main.yml",
    "workflow_ref": "main",
    "poll_interval_sec": "60",
    "keep_alive_enabled": "0",
    "keep_alive_cooldown_min": "60",
    "auto_restart_enabled": "1",
    "restart_before_min": "330",
    "timezone": "Asia/Barnaul",
    "dashboard_user": "admin",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._lock, self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    # -- settings ------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        if default is None:
            default = DEFAULTS.get(key)
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def get_bool(self, key: str) -> bool:
        return str(self.get_setting(key, "0")) == "1"

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    # -- runs ----------------------------------------------------------
    @staticmethod
    def _duration_sec(run: dict) -> int | None:
        try:
            created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            updated = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
            return max(0, int((updated - created).total_seconds()))
        except Exception:
            return None

    def upsert_run(self, run: dict) -> None:
        head = run.get("head_commit") or {}
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO runs(id, run_number, name, status, conclusion, event,
                                    created_at, started_at, updated_at, html_url,
                                    head_branch, head_sha, duration_sec, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     run_number=excluded.run_number, name=excluded.name,
                     status=excluded.status, conclusion=excluded.conclusion,
                     event=excluded.event, created_at=excluded.created_at,
                     started_at=excluded.started_at, updated_at=excluded.updated_at,
                     html_url=excluded.html_url, head_branch=excluded.head_branch,
                     head_sha=excluded.head_sha, duration_sec=excluded.duration_sec,
                     last_seen=excluded.last_seen""",
                (
                    run.get("id"),
                    run.get("run_number"),
                    run.get("name"),
                    run.get("status"),
                    run.get("conclusion"),
                    run.get("event"),
                    run.get("created_at"),
                    run.get("started_at"),
                    run.get("updated_at"),
                    run.get("html_url"),
                    run.get("head_branch"),
                    (head.get("id") if isinstance(head, dict) else run.get("head_sha")),
                    self._duration_sec(run),
                    utcnow_iso(),
                ),
            )

    def get_runs(self, limit: int = 50) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: int) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_active_run(self) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM runs WHERE status IN ('queued','in_progress','pending','waiting','requested') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # -- events --------------------------------------------------------
    def add_event(self, kind: str, message: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO events(ts, kind, message) VALUES(?,?,?)",
                (utcnow_iso(), kind, message),
            )

    def get_events(self, limit: int = 200) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_events(self) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM events")

    # -- schedules -----------------------------------------------------
    def add_schedule(self, label: str, cron: str, enabled: int = 1) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO schedules(label, cron, enabled, created_at) VALUES(?,?,?,?)",
                (label.strip(), cron.strip(), 1 if enabled else 0, utcnow_iso()),
            )
            return cur.lastrowid

    def update_schedule(self, sid: int, label: str, cron: str, enabled: int) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE schedules SET label=?, cron=?, enabled=? WHERE id=?",
                (label.strip(), cron.strip(), 1 if enabled else 0, sid),
            )

    def toggle_schedule(self, sid: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE schedules SET enabled = 1 - enabled WHERE id=?", (sid,))

    def delete_schedule(self, sid: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM schedules WHERE id=?", (sid,))

    def get_schedules(self) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def mark_schedule_run(self, sid: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE schedules SET last_run=? WHERE id=?", (utcnow_iso(), sid))
