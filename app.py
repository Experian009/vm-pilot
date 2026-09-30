"""VM Pilot — web dashboard to monitor and control a GitHub Actions VM.

Features:
  * live status of the VM (running / stopped, elapsed, time left)
  * manual start / stop / restart via the GitHub API (workflow_dispatch)
  * workflow runs history + job log viewer
  * uptime statistics with a per-day chart
  * autopilot: keep-alive + seamless restart before the 6h GitHub timeout
  * cron schedules for automatic starts
  * event log of every manual and automatic action
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)

from autopilot import (format_duration, is_active, parse_ts, run_age_minutes,
                       should_keep_alive, should_pre_restart, summarize,
                       uptime_by_day, validate_cron)
from github_client import GitHubClient, GitHubError
from store import Store, utcnow_iso

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("VM_PILOT_DB", os.path.join(BASE_DIR, "vm_pilot.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vm-pilot-dev-key-change-me")
app.config["JSON_AS_ASCII"] = False

store = Store(DB_PATH)
sched = BackgroundScheduler(timezone="UTC")


# ------------------------------------------------------------------ helpers
def env_token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def manual_token() -> str:
    return (store.get_setting("github_token", "") or "").strip()


def token_source() -> str:
    """Where the effective GitHub token comes from: manual | env | none."""
    if manual_token():
        return "manual"
    if env_token():
        return "env"
    return "none"


def settings() -> dict:
    return {
        "github_token": manual_token() or env_token(),
        "repo_owner": store.get_setting("repo_owner", ""),
        "repo_name": store.get_setting("repo_name", ""),
        "workflow_file": store.get_setting("workflow_file", "main.yml"),
        "workflow_ref": store.get_setting("workflow_ref", "main"),
        "poll_interval_sec": store.get_int("poll_interval_sec", 60),
        "keep_alive_enabled": store.get_bool("keep_alive_enabled"),
        "keep_alive_cooldown_min": store.get_int("keep_alive_cooldown_min", 60),
        "auto_restart_enabled": store.get_bool("auto_restart_enabled"),
        "restart_before_min": store.get_int("restart_before_min", 330),
        "timezone": store.get_setting("timezone", "Asia/Barnaul") or "Asia/Barnaul",
        "dashboard_user": os.environ.get("DASHBOARD_USER") or store.get_setting("dashboard_user", "admin"),
    }


def dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD") or store.get_setting("dashboard_password", "")


def auth_enabled() -> bool:
    return bool(dashboard_password())


def check_password(pw: str) -> bool:
    return hashlib.sha256(pw.encode()).hexdigest() == hashlib.sha256(
        dashboard_password().encode()).hexdigest()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if auth_enabled() and not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def gh() -> GitHubClient:
    s = settings()
    return GitHubClient(s["github_token"], s["repo_owner"], s["repo_name"])


def tz():
    try:
        return ZoneInfo(settings()["timezone"])
    except Exception:
        return ZoneInfo("UTC")


def fmt_ts(iso: str | None) -> str:
    dt = parse_ts(iso)
    if not dt:
        return "—"
    return dt.astimezone(tz()).strftime("%d.%m %H:%M")


def status_payload() -> dict:
    s = settings()
    client = gh()
    active = store.get_active_run()
    age_min = run_age_minutes(active)
    payload = {
        "configured": client.configured,
        "active": bool(active),
        "run": None,
        "keep_alive": s["keep_alive_enabled"],
        "auto_restart": s["auto_restart_enabled"],
        "schedules": len([x for x in store.get_schedules() if x["enabled"]]),
    }
    if active:
        elapsed = int((age_min or 0) * 60)
        payload["run"] = {
            "id": active["id"],
            "number": active.get("run_number"),
            "status": active.get("status"),
            "started": fmt_ts(active.get("started_at") or active.get("created_at")),
            "elapsed": format_duration(elapsed),
            "elapsed_min": round(age_min or 0),
            "limit_min": 360,
            "left_min": max(0, 360 - int(age_min or 0)),
            "pct": min(100, round((age_min or 0) / 360 * 100)),
            "url": active.get("html_url"),
        }
    return payload


# ------------------------------------------------------------------ actions
def do_dispatch(source: str) -> tuple[bool, str]:
    s = settings()
    client = gh()
    if not client.configured:
        return False, "GitHub не настроен — заполни токен и репозиторий в Настройках"
    try:
        client.dispatch(s["workflow_file"], ref=s["workflow_ref"])
        store.add_event("action", f"[{source}] Запущена виртуалка (workflow_dispatch → {s['workflow_ref']})")
        poll_github()  # refresh state immediately
        return True, "Команда запуска отправлена"
    except GitHubError as e:
        store.add_event("error", f"[{source}] Ошибка запуска: {e}")
        return False, str(e)


def do_cancel(run_id: int, source: str) -> tuple[bool, str]:
    client = gh()
    if not client.configured:
        return False, "GitHub не настроен"
    try:
        client.cancel_run(run_id)
        store.set_setting("last_manual_stop", utcnow_iso())
        store.add_event("action", f"[{source}] Остановлена виртуалка (run {run_id})")
        poll_github()
        return True, "Команда остановки отправлена"
    except GitHubError as e:
        store.add_event("error", f"[{source}] Ошибка остановки: {e}")
        return False, str(e)


def do_restart(source: str) -> tuple[bool, str]:
    active = store.get_active_run()
    ok, msg = do_dispatch(source + "/restart")
    if not ok:
        return ok, msg
    if active:
        try:
            gh().cancel_run(active["id"])
            store.add_event("action", f"[{source}] Старый запуск {active['id']} отменён (рестарт)")
        except GitHubError as e:
            store.add_event("error", f"[{source}] Не удалось отменить старый запуск: {e}")
    poll_github()
    return True, "Рестарт: новый запуск создан, старый остановлен"


# ------------------------------------------------------------------ poll job
def poll_github():
    """Sync workflow runs from GitHub; detect transitions; run autopilot."""
    s = settings()
    client = gh()
    if not client.configured:
        return
    try:
        runs = client.list_runs(s["workflow_file"], per_page=20)
    except GitHubError as e:
        store.add_event("error", f"Опрос GitHub: {e}")
        return

    known = {r["id"]: r for r in store.get_runs(limit=50)}
    for r in runs:
        rid = r["id"]
        prev = known.get(rid)
        store.upsert_run(r)
        if prev is None:
            store.add_event("auto", f"Новый запуск #{r.get('run_number')} ({r.get('status')})")
        elif prev.get("status") != r.get("status"):
            if r.get("status") == "completed":
                store.add_event(
                    "auto",
                    f"Запуск #{r.get('run_number')} завершён: {r.get('conclusion')} "
                    f"({format_duration(Store._duration_sec(r))})",
                )
            else:
                store.add_event("auto", f"Запуск #{r.get('run_number')}: {prev.get('status')} → {r.get('status')}")

    # autopilot: seamless restart before the 6h timeout
    active = store.get_active_run()
    now = datetime.now(timezone.utc)
    pre, reason = should_pre_restart(
        active, s["auto_restart_enabled"], s["restart_before_min"], now)
    if pre:
        store.add_event("auto", f"Автопилот: превентивный рестарт — {reason}")
        do_restart("autopilot/pre-restart")
        return

    # autopilot: keep-alive
    last_stop = parse_ts(store.get_setting("last_manual_stop"))
    keep, reason = should_keep_alive(
        active, s["keep_alive_enabled"], last_stop, s["keep_alive_cooldown_min"], now)
    if keep:
        store.add_event("auto", f"Автопилот: keep-alive — {reason}")
        do_dispatch("autopilot/keep-alive")


def scheduled_start(schedule_id: int, label: str):
    store.mark_schedule_run(schedule_id)
    store.add_event("auto", f"Расписание «{label}»: запуск по cron")
    ok, msg = do_dispatch(f"schedule/{label}")
    if not ok:
        store.add_event("error", f"Расписание «{label}» не сработало: {msg}")


def refresh_cron_jobs():
    for job in list(sched.get_jobs()):
        if job.id.startswith("cron-"):
            job.remove()
    for sc in store.get_schedules():
        if not sc["enabled"]:
            continue
        try:
            trig = CronTrigger.from_crontab(sc["cron"], timezone="UTC")
        except Exception as e:
            store.add_event("error", f"Расписание «{sc['label']}»: неверный cron ({e})")
            continue
        sched.add_job(scheduled_start, trig, id=f"cron-{sc['id']}",
                      args=[sc["id"], sc["label"]], replace_existing=True)


# ------------------------------------------------------------------ pages
@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("dashboard"))
    err = ""
    if request.method == "POST":
        if (request.form.get("username") == settings()["dashboard_user"]
                and check_password(request.form.get("password", ""))):
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        err = "Неверный логин или пароль"
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    s = settings()
    p = status_payload()
    stats = summarize(store.get_runs(limit=200))
    chart = uptime_by_day(store.get_runs(limit=500))
    return render_template("dashboard.html", p=p, s=s, stats=stats, chart=chart,
                           fmt_duration=format_duration,
                           auth_on=auth_enabled())


@app.route("/runs")
@login_required
def runs():
    client = gh()
    items = []
    err = ""
    if client.configured:
        try:
            items = client.list_runs(settings()["workflow_file"], per_page=30)
            for r in items:
                store.upsert_run(r)
        except GitHubError as e:
            err = str(e)
            items = store.get_runs(limit=30)
    else:
        items = store.get_runs(limit=30)
    return render_template("runs.html", runs=items, err=err, fmt_ts=fmt_ts,
                           fmt_duration=format_duration)


@app.route("/runs/<int:run_id>")
@login_required
def run_detail(run_id):
    client = gh()
    jobs, logs, err, active_job = [], "", "", None
    job_id = request.args.get("job", type=int)
    if client.configured:
        try:
            jobs = client.list_jobs(run_id)
            if jobs:
                active_job = job_id or jobs[0]["id"]
                raw = client.job_logs(active_job)
                # cap output: keep the tail, it is what matters most
                lines = raw.splitlines()
                if len(lines) > 4000:
                    logs = f"... обрезано, показаны последние 4000 из {len(lines)} строк ...\n\n" \
                           + "\n".join(lines[-4000:])
                else:
                    logs = raw
        except GitHubError as e:
            err = str(e)
    stored = store.get_run(run_id)
    return render_template("run.html", run_id=run_id, jobs=jobs, logs=logs,
                           err=err, active_job=active_job, stored=stored,
                           fmt_ts=fmt_ts)


@app.route("/schedules", methods=["GET", "POST"])
@login_required
def schedules():
    msg, err = "", ""
    if request.method == "POST":
        label = request.form.get("label", "").strip() or "Без названия"
        cron = request.form.get("cron", "").strip()
        ok, cerr = validate_cron(cron)
        if not ok:
            err = cerr
        else:
            store.add_schedule(label, cron, enabled=1)
            refresh_cron_jobs()
            store.add_event("action", f"Добавлено расписание «{label}» ({cron})")
            msg = "Расписание добавлено"
    return render_template("schedules.html", schedules=store.get_schedules(),
                           msg=msg, err=err)


@app.route("/schedules/<int:sid>/toggle", methods=["POST"])
@login_required
def schedule_toggle(sid):
    store.toggle_schedule(sid)
    refresh_cron_jobs()
    return redirect(url_for("schedules"))


@app.route("/schedules/<int:sid>/delete", methods=["POST"])
@login_required
def schedule_delete(sid):
    store.delete_schedule(sid)
    refresh_cron_jobs()
    store.add_event("action", f"Удалено расписание #{sid}")
    return redirect(url_for("schedules"))


@app.route("/events")
@login_required
def events():
    if request.args.get("clear") == "1":
        store.clear_events()
        store.add_event("action", "Журнал событий очищен")
        return redirect(url_for("events"))
    return render_template("events.html", events=store.get_events(limit=300),
                           fmt_ts=fmt_ts)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    msg, err = "", ""
    s = settings()
    if request.method == "POST":
        form = request.form
        store.set_setting("github_token", form.get("github_token", "").strip())
        store.set_setting("repo_owner", form.get("repo_owner", "").strip())
        store.set_setting("repo_name", form.get("repo_name", "").strip())
        store.set_setting("workflow_file", form.get("workflow_file", "").strip() or "main.yml")
        store.set_setting("workflow_ref", form.get("workflow_ref", "").strip() or "main")
        store.set_setting("poll_interval_sec", form.get("poll_interval_sec", "60"))
        store.set_setting("keep_alive_enabled", "1" if form.get("keep_alive_enabled") else "0")
        store.set_setting("keep_alive_cooldown_min", form.get("keep_alive_cooldown_min", "60"))
        store.set_setting("auto_restart_enabled", "1" if form.get("auto_restart_enabled") else "0")
        store.set_setting("restart_before_min", form.get("restart_before_min", "330"))
        store.set_setting("timezone", form.get("timezone", "").strip() or "Asia/Barnaul")
        if form.get("dashboard_password"):
            store.set_setting("dashboard_password", form["dashboard_password"])
        store.add_event("action", "Настройки обновлены")
        apply_poll_interval()
        msg = "Настройки сохранены"
        s = settings()
    conn, conn_err = None, ""
    if gh().configured and request.args.get("test") == "1":
        try:
            conn = gh().test_connection()
            store.add_event("action", f"Проверка подключения: OK ({conn['full_name']})")
        except GitHubError as e:
            conn_err = str(e)
    return render_template("settings.html", s=s, msg=msg, err=err,
                           conn=conn, conn_err=conn_err,
                           token_source=token_source())


# ------------------------------------------------------------------ API
@app.route("/api/status")
@login_required
def api_status():
    return jsonify(status_payload())


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    ok, msg = do_dispatch("manual")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    active = store.get_active_run()
    if not active:
        return jsonify({"ok": False, "msg": "Нет активного запуска"})
    ok, msg = do_cancel(active["id"], "manual")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/restart", methods=["POST"])
@login_required
def api_restart():
    ok, msg = do_restart("manual")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/poll", methods=["POST"])
@login_required
def api_poll():
    poll_github()
    return jsonify({"ok": True})


# ------------------------------------------------------------------ boot
def apply_poll_interval():
    for job in list(sched.get_jobs()):
        if job.id == "poll":
            job.remove()
    interval = settings()["poll_interval_sec"]
    sched.add_job(poll_github, "interval", seconds=interval, id="poll",
                  replace_existing=True, max_instances=1)


def start_scheduler():
    if not sched.running:
        apply_poll_interval()
        refresh_cron_jobs()
        sched.start()
        # first poll soon after boot
        sched.add_job(poll_github, "date", id="poll-once")


# start the background scheduler on import too (gunicorn/uvicorn never run
# the __main__ block). Workers must be 1, otherwise jobs would duplicate.
if os.environ.get("VM_PILOT_NO_SCHEDULER") != "1":
    start_scheduler()


if __name__ == "__main__":
    store.add_event("info", "VM Pilot запущен")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
