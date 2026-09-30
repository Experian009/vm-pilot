"""Unit tests for the GitHub API client (HTTP is mocked)."""
import unittest
from unittest.mock import patch

from github_client import GitHubClient, GitHubError


class FakeResp:
    def __init__(self, status=200, payload=None, text="", ctype="application/json"):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers = {"Content-Type": ctype}

    def json(self):
        return self._payload


def make_client():
    return GitHubClient("tok", "owner", "repo")


class TestGitHubClient(unittest.TestCase):
    def test_configured(self):
        self.assertTrue(make_client().configured)
        self.assertFalse(GitHubClient("", "o", "r").configured)

    def _patch(self, client, resp):
        return patch.object(client.s, "request", return_value=resp)

    def test_dispatch_ok(self):
        c = make_client()
        with self._patch(c, FakeResp(204)):
            c.dispatch("main.yml", ref="main")  # must not raise

    def test_dispatch_sends_ref(self):
        c = make_client()
        with patch.object(c.s, "request", return_value=FakeResp(204)) as m:
            c.dispatch("main.yml", ref="main")
        args, kw = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn("/actions/workflows/main.yml/dispatches", args[1])
        self.assertEqual(kw["json"], {"ref": "main"})

    def test_list_runs_parses(self):
        c = make_client()
        payload = {"workflow_runs": [{"id": 1, "status": "in_progress"}]}
        with self._patch(c, FakeResp(200, payload)):
            runs = c.list_runs("main.yml")
        self.assertEqual(runs, [{"id": 1, "status": "in_progress"}])

    def test_cancel_ok(self):
        c = make_client()
        with self._patch(c, FakeResp(202)):
            c.cancel_run(99)

    def test_401_message(self):
        c = make_client()
        with self._patch(c, FakeResp(401, {"message": "Bad credentials"})):
            with self.assertRaises(GitHubError) as ctx:
                c.test_connection()
        self.assertIn("401", str(ctx.exception))
        self.assertIn("токен", str(ctx.exception))

    def test_network_error_wrapped(self):
        import requests as rq

        c = make_client()
        with patch.object(c.s, "request", side_effect=rq.exceptions.ConnectionError("down")):
            with self.assertRaises(GitHubError):
                c.list_runs("main.yml")

    def test_job_logs_returns_text(self):
        c = make_client()
        with self._patch(c, FakeResp(200, text="line1\nline2", ctype="text/plain")):
            logs = c.job_logs(5)
        self.assertIn("line1", logs)


if __name__ == "__main__":
    unittest.main()
