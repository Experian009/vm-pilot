"""Unit tests for VM Pilot storage layer."""
import os
import tempfile
import unittest

from store import Store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_settings_roundtrip(self):
        self.assertEqual(self.db.get_setting("repo_owner"), "Experian009")  # default
        self.db.set_setting("repo_owner", "someone")
        self.assertEqual(self.db.get_setting("repo_owner"), "someone")
        self.assertTrue(self.db.get_bool("keep_alive_enabled") is False)
        self.db.set_setting("keep_alive_enabled", "1")
        self.assertTrue(self.db.get_bool("keep_alive_enabled"))
        self.assertEqual(self.db.get_int("poll_interval_sec", 60), 60)

    def test_runs_upsert_and_active(self):
        run = {
            "id": 123, "run_number": 7, "name": "Windows-RDP-BORE",
            "status": "in_progress", "conclusion": None, "event": "workflow_dispatch",
            "created_at": "2026-09-30T10:00:00Z", "started_at": "2026-09-30T10:01:00Z",
            "updated_at": "2026-09-30T12:00:00Z",
            "html_url": "https://github.com/x/y/actions/runs/123",
            "head_branch": "main", "head_sha": "abc123",
        }
        self.db.upsert_run(run)
        active = self.db.get_active_run()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], 123)
        # same run completed -> no longer active
        run2 = dict(run, status="completed", conclusion="success")
        self.db.upsert_run(run2)
        self.assertIsNone(self.db.get_active_run())
        runs = self.db.get_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["conclusion"], "success")
        self.assertEqual(runs[0]["duration_sec"], 7200)

    def test_events(self):
        self.db.add_event("info", "hello")
        self.db.add_event("error", "boom")
        evs = self.db.get_events()
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["message"], "boom")  # newest first
        self.db.clear_events()
        self.assertEqual(self.db.get_events(), [])

    def test_schedules_crud(self):
        sid = self.db.add_schedule("night", "0 2 * * *")
        scs = self.db.get_schedules()
        self.assertEqual(len(scs), 1)
        self.assertEqual(scs[0]["label"], "night")
        self.assertEqual(scs[0]["enabled"], 1)
        self.db.toggle_schedule(sid)
        self.assertEqual(self.db.get_schedules()[0]["enabled"], 0)
        self.db.update_schedule(sid, "night2", "0 3 * * *", 1)
        sc = self.db.get_schedules()[0]
        self.assertEqual((sc["label"], sc["cron"], sc["enabled"]), ("night2", "0 3 * * *", 1))
        self.db.mark_schedule_run(sid)
        self.assertIsNotNone(self.db.get_schedules()[0]["last_run"])
        self.db.delete_schedule(sid)
        self.assertEqual(self.db.get_schedules(), [])


if __name__ == "__main__":
    unittest.main()
