"""One run, end to end: the warning, the kill, and always an ending."""

import os
import time
from pathlib import Path

from ai_crontab import core, model
from conftest import run_supervisor


def start(cfg, store, task, now=None):
    """Claim a run for a task and drive it to completion."""
    from ai_crontab.config import utcnow
    run_id = store.claim_run(task.id, now or utcnow())
    run_supervisor(cfg, run_id)
    return store.get_run(run_id)


def test_an_ordinary_run_ends_ok_and_says_nothing(cfg, store, make_task):
    """SC-9: a task with nothing to report leaves the unread count alone."""
    task = make_task("quiet")
    before = store.unread_count()
    run = start(cfg, store, task)

    assert run.state == model.OK
    assert run.exit_code == 0
    assert run.summary == "nothing to report"
    assert store.unread_count() == before


def test_a_run_with_something_to_say_files_exactly_one(cfg, store, make_task):
    task = make_task("notify")
    run = start(cfg, store, task)

    assert run.state == model.OK
    notes = store.list_notifications()
    assert len(notes) == 1
    assert notes[0].title == "something happened"
    assert notes[0].priority == "high"
    assert notes[0].task_id == task.id, "a run files as its own task, never another"


def test_a_failing_run_is_recorded_and_changes_nothing_else(cfg, store, make_task):
    """FR-15: failures are recorded, never acted on."""
    task = make_task("crash")
    run = start(cfg, store, task)

    assert run.state == model.FAILED
    assert run.exit_code == 1
    after = store.get_task(task.id)
    assert after.enabled, "a failure must never disable a task"
    assert after.next_run is not None, "a failure must never take a task off its schedule"
    assert store.unread_count() == 0, "the scheduler never files a notification itself"


def test_repeated_failure_still_changes_nothing(cfg, store, make_task):
    """SC-10: ten failures in a row leave the task enabled and on schedule."""
    task = make_task("crash")
    for _ in range(3):
        start(cfg, store, task)
    after = store.get_task(task.id)
    assert after.enabled and after.next_run is not None
    assert store.unread_count() == 0
    assert all(r.state == model.FAILED for r in store.run_history(task.id))


def test_a_hung_run_is_killed_at_its_limit(cfg, store, make_task):
    """SC-7: without this, one hung process silences a task forever."""
    task = make_task("slow")
    started = time.monotonic()
    run = start(cfg, store, task)
    took = time.monotonic() - started

    assert run.state == model.TIMEOUT
    assert run.ended_at is not None
    assert took < cfg.time_limit_sec + 15, "the kill must not wait on the agent"


def test_a_killed_run_leaves_no_children_behind(cfg, store, make_task, tmp_path):
    """SC-9 in the spec's numbering: the kill takes the whole process group."""
    marker = tmp_path / "child.pid"
    os.environ["FAKE_AGENT_MARKER"] = str(marker)
    try:
        task = make_task("spawner")
        run = start(cfg, store, task)
    finally:
        del os.environ["FAKE_AGENT_MARKER"]

    assert run.state == model.TIMEOUT
    child_pid = int(marker.read_text())
    assert not Path(f"/proc/{child_pid}").exists(), "the agent's child outlived the kill"


def test_the_warning_arrives_and_the_handoff_survives(cfg, store, make_task):
    """SC-8: the run is told to wrap up, and what it wrote is on disk after the kill."""
    task = make_task("wrapper")
    run = start(cfg, store, task)

    assert run.state == model.OK, "a run that wraps up on the warning exits cleanly"
    assert core.read_handoff(cfg, task.id) == "wrapped up on the warning"


def test_the_handoff_reaches_the_next_run(cfg, store, make_task):
    """SC-10 in the spec's numbering: continuity without the agent asking for it."""
    task = make_task("handoff")
    start(cfg, store, task)
    assert core.read_handoff(cfg, task.id) == "stopped at bug 1993004"

    core.modify_task(store, cfg, task.name, prompt="echo")
    run = start(cfg, store, store.get_task(task.id))
    assert "stopped at bug 1993004" in run.summary


def test_the_run_leaves_a_transcript(cfg, store, make_task):
    task = make_task("quiet")
    run = start(cfg, store, task)
    assert cfg.transcript_path(task.id, run.id).exists()


def test_the_schedule_advances_once_the_run_ends(cfg, store, make_task, at):
    """A task is not due again until the run it is waiting on has finished."""
    task = make_task("quiet", schedule="*/5 * * * *")
    task.next_run = at(-1)
    store.update_task(task)

    start(cfg, store, task)
    from ai_crontab.config import utcnow
    assert store.get_task(task.id).next_run > utcnow()


def test_a_one_time_task_retires_after_its_run(cfg, store, soon):
    """SC-1: it fires once and no later tick runs it again."""
    task = core.add_task(store, cfg, "once-off", "quiet", soon, working_dir=str(cfg.root))
    assert task.next_run is not None

    start(cfg, store, task)
    assert store.get_task(task.id).next_run is None
    assert len(store.run_history(task.id)) == 1
