"""Unit tests for autopilot decision logic (pure functions)."""
import unittest
from datetime import datetime, timedelta, timezone

from autopilot import (format_duration, is_active, run_age_minutes,
                       should_keep_alive, should_pre_restart, summarize,
                       uptime_by_day, validate_cron)


def mk_run(status="in_progress", minutes_ago=60, conclusion=None):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "id": 1, "run_number": 3, "status": status, "conclusion": conclusion,
        "created_at": start, "started_at": start,
        "updated_at": now.isoformat(), "duration_sec": minutes_ago * 60,
    }


class TestAutopilot(unittest.TestCase):
    def test_is_active(self):
        self.assertTrue(is_active(mk_run("in_progress")))
        self.assertTrue(is_active(mk_run("queued")))
        self.assertFalse(is_active(mk_run("completed", conclusion="success")))
        self.assertFalse(is_active(None))

    def test_pre_restart(self):
        now = datetime.now(timezone.utc)
        old = mk_run(minutes_ago=340)
        ok, reason = should_pre_restart(old, True, 330, now)
        self.assertTrue(ok)
        self.assertIn("340", reason)
        young = mk_run(minutes_ago=60)
        ok, _ = should_pre_restart(young, True, 330, now)
        self.assertFalse(ok)
        ok, _ = should_pre_restart(old, False, 330, now)
        self.assertFalse(ok)
        ok, _ = should_pre_restart(mk_run("completed", conclusion="success"), True, 330, now)
        self.assertFalse(ok)

    def test_keep_alive(self):
        now = datetime.now(timezone.utc)
        # no active run -> start
        ok, _ = should_keep_alive(None, True, None, 60, now)
        self.assertTrue(ok)
        # active run -> do nothing
        ok, _ = should_keep_alive(mk_run(), True, None, 60, now)
        self.assertFalse(ok)
        # disabled
        ok, _ = should_keep_alive(None, False, None, 60, now)
        self.assertFalse(ok)
        # manual stop recently -> cooldown
        stop = now - timedelta(minutes=10)
        ok, reason = should_keep_alive(None, True, stop, 60, now)
        self.assertFalse(ok)
        self.assertIn("пауза", reason)
        # cooldown expired -> start
        stop = now - timedelta(minutes=61)
        ok, _ = should_keep_alive(None, True, stop, 60, now)
        self.assertTrue(ok)

    def test_format_duration(self):
        self.assertEqual(format_duration(3661), "1 ч 01 мин")
        self.assertEqual(format_duration(90), "1 мин 30 с")
        self.assertEqual(format_duration(5), "5 с")
        self.assertEqual(format_duration(None), "—")

    def test_summarize(self):
        runs = [mk_run("completed", minutes_ago=120, conclusion="success"),
                mk_run("completed", minutes_ago=60, conclusion="failure"),
                mk_run("in_progress", minutes_ago=30)]
        s = summarize(runs)
        self.assertEqual(s["finished_runs"], 2)
        self.assertEqual(s["by_conclusion"]["success"], 1)
        self.assertGreater(s["total_uptime_sec"], 120 * 60)

    def test_uptime_by_day(self):
        days = uptime_by_day([mk_run("completed", minutes_ago=60, conclusion="success")], days=3)
        self.assertEqual(len(days), 3)
        self.assertEqual(days[-1]["uptime_sec"], 3600)

    def test_validate_cron(self):
        ok, _ = validate_cron("0 */6 * * *")
        self.assertTrue(ok)
        ok, msg = validate_cron("every hour")
        self.assertFalse(ok)
        self.assertIn("5 полей", msg)
        ok, _ = validate_cron("61 * * * *")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
