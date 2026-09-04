"""What system cron calls: find what is due, start it, and get out of the way."""

import fcntl
import os
import subprocess
import sys
from datetime import timedelta

from . import core, model, supervisor
from .config import utcnow


class _TickLock:
    """Holds tick.lock for the length of one tick, or declines to run.

    Non-blocking on purpose: if a previous tick is somehow still going, the
    right thing is for this one to exit rather than queue up behind it, since
    cron will call again shortly.
    """

    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


def tick(cfg, store, now=None, spawn=None):
    """Run one scheduling pass.

    Reaps runs whose supervisor died, launches every task that is due up to the
    concurrency ceiling, and prunes if enough time has passed. Returns the run
    ids it started.

    `spawn` exists for tests; by default each run is started as a detached
    `ai-crontab run` process. Nothing is waited on, so the length of a tick
    does not depend on the length of a task.

    A task that is due but over the ceiling is simply not launched. Its
    `next_run` is left where it is, so it stays due and the following tick
    reconsiders it, most overdue first. That is the whole of the queueing: the
    store already records that a task is waiting.
    """
    now = now or utcnow()
    spawn = spawn or spawn_supervisor
    started = []

    with _TickLock(cfg.lock_path) as acquired:
        if not acquired:
            return started
        store.set_meta("last_tick", now.isoformat())
        reap(cfg, store, now)

        room = cfg.max_concurrent_runs - store.count_active_runs()
        for task in store.due_tasks(now):
            if room <= 0:
                break
            run_id = store.claim_run(task.id, now)
            if run_id is None:
                continue
            spawn(cfg, run_id)
            started.append(run_id)
            room -= 1

        prune_if_due(cfg, store, now)
    return started


def reap(cfg, store, now):
    """Close out runs whose supervisor is gone, so their tasks are not stuck.

    Two cases end up here: the machine went down mid-run, and the supervisor
    itself died. Both leave an active row that would otherwise lock the task
    out forever, and a task that silently stops running is the failure this
    whole design is arranged to prevent.

    A run with no pid yet is a supervisor still starting up, and is only
    declared lost once it has had `startup_grace_sec` to record one.
    """
    lost = []
    for run in store.active_runs():
        if run.supervisor_pid is None:
            if now - run.started_at > timedelta(seconds=cfg.startup_grace_sec):
                lost.append(run)
        elif not supervisor.supervisor_alive(run.supervisor_pid, run.id):
            lost.append(run)
    for run in lost:
        store.finish_run(run.id, model.LOST, now,
                         error="supervisor gone; machine or process died mid-run")
    return [run.id for run in lost]


def prune_if_due(cfg, store, now):
    """Delete records past their retention, at most once a period.

    The tick is the only thing guaranteed to run, so it owns pruning, but on a
    one-minute interval it must not do the work every time.
    """
    last = store.get_meta("last_prune")
    if last is not None:
        from .model import from_iso
        if now - from_iso(last) < timedelta(seconds=cfg.prune_every_sec):
            return 0
    removed = core.prune(cfg, store, now)
    store.set_meta("last_prune", now.isoformat())
    return removed


def spawn_supervisor(cfg, run_id):
    """Start a supervisor for one run and return immediately.

    The child gets its own session so it outlives this tick, and its stdio goes
    nowhere: cron would mail any output, which is exactly the interruption the
    whole inbox exists to avoid.
    """
    argv = [sys.executable, "-m", "ai_crontab", "run", run_id]
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "AI_CRONTAB_ROOT": str(cfg.root)},
    )


def run_now(cfg, store, ref, now=None):
    """Start a task immediately, by the same path the tick uses.

    Returns the run id, or None when the task already has a run in flight.
    """
    now = now or utcnow()
    task = core.resolve(store, ref)
    run_id = store.claim_run(task.id, now)
    if run_id is not None:
        spawn_supervisor(cfg, run_id)
    return run_id
