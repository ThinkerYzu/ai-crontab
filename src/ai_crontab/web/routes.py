"""The pages, the one JSON route the dashboard polls, and the routes that write.

Every handler opens the store for its own request and closes it again. None of
them decides anything: what a task is, when it next runs, what a run cost and
what adding one means are all answered by `core`, `tick` and `store`, the way
the CLI answers them.

The writing half is guarded before it is routed, in `app.guard`. Read that
before adding a route here.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import config as config_module
from .. import core
from .. import tick as tick_module
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


# -- the routes that write ----------------------------------------------
#
# Every one of these is a call into `core` or `tick` with the form decoded in
# front of it, so what the browser can do to a task is exactly what the CLI can
# do to it. None of them decides anything the other doors do not.
#
# They answer with JSON rather than a redirect because the guard requires a
# header an HTML form cannot set, so the page posts with `fetch` and follows
# the `redirect` itself. See `static/app.js`.


@router.post("/tasks/new")
async def create_task(request: Request):
    cfg = load_config(request)
    form = await request.form()
    with core.open_store(cfg) as store:
        task = core.add_task(
            store, cfg,
            name=_required(form, "name"),
            prompt=_required(form, "prompt"),
            schedule_text=_required(form, "schedule"),
            working_dir=_field(form, "working_dir"),
            time_limit_sec=_int_field(form, "time_limit_sec"),
        )
    return _answer(f"/tasks/{task.id}", f"added {task.name}")


@router.post("/tasks/{ref}")
async def edit_task(request: Request, ref: str):
    """Change whatever fields the form carried, and nothing else.

    A form that submits only `enabled` is the enable and disable buttons; one
    that submits the whole task is the edit form. `core.modify_task` touches
    only the keys it is given, so both are the same route.

    A field left blank is left alone rather than written as blank. There is no
    task worth having with no prompt, and a form that cleared one by being
    submitted half-filled would be a bad way to find that out.
    """
    cfg = load_config(request)
    form = await request.form()
    changes = {name: _field(form, name)
               for name in ("name", "prompt", "schedule", "working_dir")
               if _field(form, name) is not None}
    if _field(form, "time_limit_sec") is not None:
        changes["time_limit_sec"] = _int_field(form, "time_limit_sec")
    if "enabled" in form:
        changes["enabled"] = _field(form, "enabled") == "true"

    with core.open_store(cfg) as store:
        task = core.modify_task(store, cfg, _resolve(store, ref).id, **changes)
    return _answer(f"/tasks/{task.id}", "saved")


@router.post("/tasks/{ref}/delete")
async def delete_task(request: Request, ref: str):
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        task = core.remove_task(store, cfg, _resolve(store, ref).id)
    return _answer("/", f"removed {task.name}")


@router.post("/tasks/{ref}/run")
async def run_task_now(request: Request, ref: str):
    """Start a run this instant, by the same path the tick uses.

    `tick.run_now` builds the child's environment from the config rather than
    passing on this server's own, which is what makes this the run cron would
    have started ([Decision 11](DESIGN.md)). A `None` means the task already
    has a run in flight, which is the overlap rule doing its job rather than an
    error worth a stack trace.
    """
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        task = _resolve(store, ref)
        run_id = tick_module.run_now(cfg, store, task.id)
    if run_id is None:
        raise HTTPException(status_code=409, detail="already running")
    return _answer(f"/runs/{run_id}", f"started {run_id}")


@router.post("/inbox")
async def mark_whole_inbox_read(request: Request):
    cfg = load_config(request)
    with core.open_store(cfg) as store:
        marked = store.mark_all_read(config_module.utcnow())
    return _answer("/inbox", f"{marked} marked read")


@router.post("/inbox/{note_id}")
async def change_notification(request: Request, note_id: int):
    """Mark one notification read, or delete it.

    Nothing here writes a notification: only a run does that, through the MCP
    tools. The inbox is yours to clear, not to fill.

    Marking an already-read notification read is a success, not a 404. Two
    tabs open on the same inbox is an ordinary way to arrive here twice, and
    the second arrival has nothing to complain about.
    """
    cfg = load_config(request)
    form = await request.form()
    action = _field(form, "action") or "read"
    if action not in ("read", "delete"):
        raise HTTPException(status_code=400, detail=f"no such action {action!r}")
    with core.open_store(cfg) as store:
        if store.get_notification(note_id) is None:
            raise HTTPException(status_code=404,
                                detail=f"no notification {note_id}")
        if action == "delete":
            store.delete_notification(note_id)
        else:
            store.mark_read(note_id, config_module.utcnow())
    return _answer("/inbox", "deleted" if action == "delete" else "marked read")


def _answer(where, message):
    """The reply to a write: where the page should go next, and what to say."""
    return JSONResponse({"redirect": where, "message": message})


def _field(form, name):
    """One form field, with blank treated as absent.

    A text input the user left alone arrives as an empty string, and passing
    that on would blank the stored value rather than leave it alone.
    """
    value = form.get(name)
    value = value.strip() if isinstance(value, str) else value
    return value or None


def _required(form, name):
    value = _field(form, name)
    if value is None:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return value


def _int_field(form, name):
    value = _field(form, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"{name} must be a number") from None
