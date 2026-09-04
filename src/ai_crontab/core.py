"""Every operation, in one place.

The CLI and the MCP server both call this module and add no rules of their own,
so a change to what "add a task" means lands in exactly one place.
"""

import os
import shutil
from datetime import timedelta
from pathlib import Path

from . import model, schedule
from .config import utcnow
from .store import Store, new_id


class TaskNotFound(LookupError):
    pass


class DuplicateName(ValueError):
    pass


def open_store(cfg):
    return Store(cfg.db_path)


def resolve(store, ref):
    """Find a task by id or name, or say which reference failed."""
    task = store.find_task(ref)
    if task is None:
        raise TaskNotFound(f"no task {ref!r}")
    return task


def add_task(store, cfg, name, prompt, schedule_text, working_dir=None,
             time_limit_sec=None, created_by="user", now=None):
    """Create a task and work out when it first runs.

    The schedule may be a cron expression or a one-time timestamp; which one it
    is follows from the text. Returns the stored task, whose `next_run` is None
    when a one-time time has already passed.
    """
    now = now or utcnow()
    if store.find_task(name) is not None:
        raise DuplicateName(f"a task named {name!r} already exists")
    kind, schedule_text = schedule.classify(schedule_text)
    task = model.Task(
        id=new_id("t"),
        name=name,
        prompt=prompt,
        schedule_kind=kind,
        schedule=schedule_text,
        working_dir=str(Path(working_dir or os.getcwd()).expanduser().resolve()),
        time_limit_sec=time_limit_sec or cfg.time_limit_sec,
        enabled=True,
        next_run=schedule.first_run_time(kind, schedule_text, now, cfg.tz),
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    store.add_task(task)
    _make_task_dirs(cfg, task.id)
    return task


def modify_task(store, cfg, ref, now=None, **changes):
    """Change a task's fields, recomputing `next_run` if the schedule moved.

    Only the keys present in `changes` are touched. Passing `enabled=True`
    revives a disabled task by putting it back on its schedule from now.
    """
    now = now or utcnow()
    task = resolve(store, ref)
    reschedule = False

    if "name" in changes and changes["name"] != task.name:
        if store.find_task(changes["name"]) is not None:
            raise DuplicateName(f"a task named {changes['name']!r} already exists")
        task.name = changes["name"]
    if "prompt" in changes:
        task.prompt = changes["prompt"]
    if "working_dir" in changes:
        task.working_dir = str(Path(changes["working_dir"]).expanduser().resolve())
    if "time_limit_sec" in changes and changes["time_limit_sec"]:
        task.time_limit_sec = int(changes["time_limit_sec"])
    if "schedule" in changes and changes["schedule"]:
        task.schedule_kind, task.schedule = schedule.classify(changes["schedule"])
        reschedule = True
    if "enabled" in changes:
        was_enabled = task.enabled
        task.enabled = bool(changes["enabled"])
        reschedule = reschedule or (task.enabled and not was_enabled)
        if not task.enabled:
            task.next_run = None

    if reschedule and task.enabled:
        task.next_run = schedule.first_run_time(
            task.schedule_kind, task.schedule, now, cfg.tz
        )
    task.updated_at = now
    store.update_task(task)
    return task


def remove_task(store, cfg, ref):
    """Delete a task along with its handoff, scratch files and transcripts."""
    task = resolve(store, ref)
    store.remove_task(task.id)
    shutil.rmtree(cfg.task_dir(task.id), ignore_errors=True)
    return task


def notify(store, task_id, run_id, title, body="", priority="normal", now=None):
    """File a notification into the inbox.

    Nothing is delivered anywhere: the entry waits until the user lists it.
    An unrecognised priority becomes `normal` rather than an error, since a
    notification is never worth losing over its label.
    """
    if priority not in model.PRIORITIES:
        priority = "normal"
    return store.add_notification(
        task_id, run_id, priority, title, body, now or utcnow()
    )


def write_handoff(cfg, task_id, content):
    """Replace a task's handoff with what its current run wants to leave behind.

    Written through a temporary file and renamed, so a run killed at its
    deadline leaves either the old handoff or the new one, never half of one.
    """
    _make_task_dirs(cfg, task_id)
    path = cfg.handoff_path(task_id)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(path)
    return path


def read_handoff(cfg, task_id):
    """What the previous run left, or None if it left nothing."""
    path = cfg.handoff_path(task_id)
    return path.read_text() if path.exists() else None


def prune(cfg, store, now=None):
    """Delete records past the retention period, and their transcripts.

    Returns the number of runs removed. Called from the tick, which rate limits
    it: see `tick.prune_if_due`.
    """
    now = now or utcnow()
    cutoff = now - timedelta(days=cfg.retention_days)
    removed = store.prune(cutoff)
    for task_dir in cfg.tasks_dir.glob("*/runs"):
        for run_id in removed:
            (task_dir / f"{run_id}.jsonl").unlink(missing_ok=True)
    return len(removed)


def _make_task_dirs(cfg, task_id):
    cfg.scratch_dir(task_id).mkdir(parents=True, exist_ok=True)
    (cfg.task_dir(task_id) / "runs").mkdir(parents=True, exist_ok=True)
