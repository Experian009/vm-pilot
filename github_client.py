"""Thin wrapper around the GitHub REST API used by VM Pilot.

Only the endpoints needed to monitor and control a single Actions
workflow are implemented. All network failures and API errors are
surfaced as GitHubError with a human-readable message.
"""
from __future__ import annotations

import requests

API = "https://api.github.com"
TIMEOUT = 20


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str, owner: str, repo: str):
        self.token = (token or "").strip()
        self.owner = (owner or "").strip()
        self.repo = (repo or "").strip()
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "vm-pilot/1.0",
            }
        )

    @property
    def configured(self) -> bool:
        return bool(self.token and self.owner and self.repo)

    # -- internals -----------------------------------------------------
    def _req(self, method: str, path: str, **kw):
        url = API + path
        try:
            r = self.s.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise GitHubError(f"Сетевая ошибка: {e}")
        if r.status_code in (202, 204):
            return None
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text[:200])
            except Exception:
                msg = r.text[:200]
            if r.status_code == 401:
                msg = "Неверный токен. " + str(msg)
            elif r.status_code == 403:
                msg = "Доступ запрещён (проверь scopes токена: нужен Actions read/write). " + str(msg)
            elif r.status_code == 404:
                msg = "Не найдено (проверь owner/repo и имя workflow-файла). " + str(msg)
            raise GitHubError(f"GitHub API {r.status_code}: {msg}")
        ctype = r.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return r.json()
        return r.text

    # -- public API ----------------------------------------------------
    def test_connection(self) -> dict:
        d = self._req("GET", f"/repos/{self.owner}/{self.repo}")
        return {
            "full_name": d.get("full_name"),
            "private": d.get("private"),
            "default_branch": d.get("default_branch"),
        }

    def list_runs(self, workflow: str, per_page: int = 20) -> list:
        d = self._req(
            "GET",
            f"/repos/{self.owner}/{self.repo}/actions/workflows/{workflow}/runs",
            params={"per_page": per_page},
        )
        return d.get("workflow_runs", [])

    def get_run(self, run_id: int) -> dict:
        return self._req("GET", f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}")

    def dispatch(self, workflow: str, ref: str = "main", inputs: dict | None = None) -> None:
        """Trigger a workflow_dispatch event. 204 = accepted."""
        body = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        self._req(
            "POST",
            f"/repos/{self.owner}/{self.repo}/actions/workflows/{workflow}/dispatches",
            json=body,
        )

    def cancel_run(self, run_id: int) -> None:
        """Cancel an in-progress run. 202 = accepted."""
        self._req("POST", f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/cancel")

    def list_jobs(self, run_id: int) -> list:
        d = self._req(
            "GET",
            f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/jobs",
            params={"per_page": 50},
        )
        return d.get("jobs", [])

    def job_logs(self, job_id: int) -> str:
        """Download job logs (follows the 302 redirect to the log archive)."""
        return self._req("GET", f"/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}/logs")
