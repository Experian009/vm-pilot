"""Autopilot decision logic for VM Pilot.

Pure functions (no I/O) so they can be unit-tested. The APScheduler
wiring lives in app.py and calls into these helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone

ACTIVE_STATUSES = {"queued", "in_progress", "pending", "waiting", "requested"}
DONE_CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "action_required"}


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_active(run: dict | None) -> bool:
    return bool(run) and run.get("status") in ACTIVE_STATUSES


def run_age_minutes(run: dict | None, now: datetime | None = None) -> float | None:
    """Minutes since the run started (falls back to created_at)."""
    if not run:
        return None
    now = now or datetime.now(timezone.utc)
    start = parse_ts(run.get("started_at")) or parse_ts(run.get("created_at"))
    if not start:
        return None
    return max(0.0, (now - start).total_seconds() / 60.0)


def should_pre_restart(run: dict | None, enabled: bool, restart_before_min: int,
                       now: datetime | None = None) -> tuple[bool, str]:
    """Restart the VM *before* GitHub kills it at the 6h timeout.

    Returns (True, reason) when the active run is older than the
    threshold so a fresh run can take over seamlessly.
    """
    if not enabled or not is_active(run):
        return False, ""
    age = run_age_minutes(run, now)
    if age is None:
        return False, ""
    if age >= restart_before_min:
        return True, f"возраст запуска {age:.0f} мин ≥ лимита {restart_before_min} мин"
    return False, ""


def should_keep_alive(active_run: dict | None, enabled: bool,
                      last_manual_stop: datetime | None,
                      cooldown_min: int,
                      now: datetime | None = None) -> tuple[bool, str]:
    """Start the VM when nothing is running (keep-alive mode).

    Respects a cooldown after a *manual* stop so the autopilot does not
    immediately undo what the user just did.
    """
    now = now or datetime.now(timezone.utc)
    if not enabled:
        return False, ""
    if is_active(active_run):
        return False, ""
    if last_manual_stop:
        since_stop = (now - last_manual_stop).total_seconds() / 60.0
        if since_stop < cooldown_min:
            return False, f"пауза после ручной остановки ({since_stop:.0f}/{cooldown_min} мин)"
    return True, "нет активного запуска"


def format_duration(total_seconds: int | None) -> str:
    if total_seconds is None:
        return "—"
    total_seconds = max(0, int(total_seconds))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин {s:02d} с"
    return f"{s} с"


def summarize(runs: list[dict], now: datetime | None = None) -> dict:
    """Uptime stats over the stored run history."""
    now = now or datetime.now(timezone.utc)
    total_sec = 0
    finished = 0
    by_conclusion: dict[str, int] = {}
    active_sec = 0
    for r in runs:
        if is_active(r):
            age = run_age_minutes(r, now)
            if age:
                active_sec = int(age * 60)
                total_sec += active_sec
        elif r.get("status") == "completed":
            finished += 1
            c = r.get("conclusion") or "unknown"
            by_conclusion[c] = by_conclusion.get(c, 0) + 1
            if r.get("duration_sec"):
                total_sec += r["duration_sec"]
    return {
        "total_runs": len(runs),
        "finished_runs": finished,
        "by_conclusion": by_conclusion,
        "total_uptime_sec": total_sec,
        "active_uptime_sec": active_sec,
    }


def uptime_by_day(runs: list[dict], days: int = 14,
                  now: datetime | None = None) -> list[dict]:
    """Uptime in seconds per calendar day (UTC) for the last `days` days."""
    now = now or datetime.now(timezone.utc)
    buckets: dict[str, int] = {}
    for i in range(days):
        d = (now.replace(hour=0, minute=0, second=0, microsecond=0)
             .timestamp() - i * 86400)
        key = datetime.fromtimestamp(d, tz=timezone.utc).strftime("%Y-%m-%d")
        buckets[key] = 0
    for r in runs:
        start = parse_ts(r.get("started_at")) or parse_ts(r.get("created_at"))
        end = parse_ts(r.get("updated_at"))
        if not start or not end or end <= start:
            continue
        # attribute the whole run to its start day (good enough for 6h runs)
        key = start.strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key] += int((end - start).total_seconds())
    return [{"day": k, "uptime_sec": v} for k, v in sorted(buckets.items())]


def validate_cron(expr: str) -> tuple[bool, str]:
    """Validate a 5-field cron expression. Returns (ok, error_message)."""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return False, "нужно 5 полей: минута час день месяц день_недели (например «0 */6 * * *»)"
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
    except ImportError:
        return True, ""
    except Exception as e:
        return False, f"неверное выражение: {e}"
    return True, ""
