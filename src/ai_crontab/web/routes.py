"""The read-only pages, and the one JSON route the dashboard polls.

Every handler opens the store for its own request, reads, and closes. None of
them decides anything: what a task is, when it next runs and what a run cost
are all answered by `core` and `store`, the way the CLI answers them.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import config as config_module
from .. import core
from ..core import TaskNotFound
from ..model import from_iso
from . import transcript as transcript_module

router = APIRouter()

# How far past its expected interval a tick may be before the dashboard calls
# the scheduler late. Two intervals rather than one: a tick that lands a few
# seconds after the cron minute is normal, and a health light that cries wolf
# every other page load teaches you to ignore it.
LATE_AFTER_INTERVALS = 2


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        return _render(request, "dashboard.html", cfg, {
            "tasks": [_task_view(store, cfg, t) for t in store.list_tasks()],
            "active": store.active_runs(),
            "unread": store.unread_count(),
            "health": _health(store, cfg),
        })


@router.get("/tasks/{ref}", response_class=HTMLResponse)
def task_page(request: Request, ref: str):
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        task = _resolve(store, ref)
        return _render(request, "task.html", cfg, {
            "task": task,
            "view": _task_view(store, cfg, task),
            "handoff": core.read_handoff(cfg, task.id),
            "runs": [_run_view(cfg, r) for r in store.run_history(task.id, 25)],
            "unread": store.unread_count(),
        })


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        task = store.get_task(run.task_id)
        parsed = transcript_module.read(cfg.transcript_path(run.task_id, run.id))
        return _render(request, "run.html", cfg, {
            "run": run,
            "view": _run_view(cfg, run),
            "task": task,
            "transcript": parsed,
            "unread": store.unread_count(),
        })


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request):
    cfg = load_config(request)
    show_all = request.query_params.get("all") is not None
    with core.open_store(cfg) as store:
        notes = store.list_notifications(unread_only=not show_all, limit=200)
        return _render(request, "inbox.html", cfg, {
            "notes": [_note_view(store, cfg, n) for n in notes],
            "show_all": show_all,
            "unread": store.unread_count(),
        })


@router.get("/api/state")
def api_state(request: Request):
    """The dashboard as JSON, for the poll that keeps an in-flight run visible.

    Deliberately the same numbers the dashboard renders and nothing more: this
    is a refresh channel, not an API for programs. A program has the library.
    """
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        health = _health(store, cfg)
        return JSONResponse({
            "unread": store.unread_count(),
            "active": [{"run": r.id, "task": r.task_id} for r in store.active_runs()],
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "enabled": t.enabled,
                    "next_run": _iso_local(cfg, t.next_run),
                }
                for t in store.list_tasks()
            ],
            "health": {
                "state": health["state"],
                "last_tick": _iso_local(cfg, health["last_tick"]),
            },
        })


def load_config(request):
    """The current config, re-read from disk for this one request.

    Per request, not per process: a tick exits in milliseconds but this server
    may be up for a week, and an edit to config.toml that did nothing until a
    restart would be a puzzle rather than a setting.
    """
    return config_module.load(request.app.state.config_root)


def _render(request, template, cfg, context):
    context = {"request": request, "cfg": cfg, **context}
    return request.app.state.templates.TemplateResponse(request, template, context)


def _resolve(store, ref):
    """The task a URL names, as a 404 rather than an exception page."""
    try:
        return core.resolve(store, ref)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _health(store, cfg):
    """Whether the scheduler itself is alive, and when it last proved it.

    The tick records `last_tick` in the store, so this reads what the scheduler
    wrote rather than the `last-tick` file the cron line maintains: one source,
    and it cannot disagree with itself.

    Lateness is a guess, and says so. ai-crontab does not know how often cron
    runs it, because the user owns that line, so it compares against the
    configured `tick_interval_sec`. "unknown" is the honest answer before the
    first tick of a new install, and is not a fault.
    """
    raw = store.get_meta("last_tick")
    last = from_iso(raw) if raw else None
    if last is None:
        return {"state": "unknown", "last_tick": None, "ago_sec": None}
    ago = (config_module.utcnow() - last).total_seconds()
    late = cfg.tick_interval_sec * LATE_AFTER_INTERVALS
    return {
        "state": "late" if ago > late else "ok",
        "last_tick": last,
        "ago_sec": int(ago),
    }


def _task_view(store, cfg, task):
    """The parts of a task a page shows that are not fields on the task."""
    runs = store.run_history(task.id, 1)
    return {
        "task": task,
        "next_run": _local(cfg, task.next_run),
        "last_run": _run_view(cfg, runs[0]) if runs else None,
        "state": "disabled" if not task.enabled else "on schedule",
    }


def _run_view(cfg, run):
    """One run as a row: when it started, how long it took, how it ended."""
    took = None
    if run.ended_at:
        took = int((run.ended_at - run.started_at).total_seconds())
    return {
        "run": run,
        "started": _local(cfg, run.started_at),
        "took_sec": took,
        "cost": run.cost_usd,
    }


def _note_view(store, cfg, note):
    task = store.get_task(note.task_id)
    return {
        "note": note,
        "task_name": task.name if task else note.task_id,
        "created": _local(cfg, note.created_at),
    }


def _local(cfg, when):
    """A stored UTC time in the zone the user reads schedules in."""
    return when.astimezone(cfg.tz) if when else None


def _iso_local(cfg, when):
    """The same instant as text, for the JSON route, which cannot take a datetime."""
    local = _local(cfg, when)
    return local.isoformat() if local else None
