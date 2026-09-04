"""Cron to inbox, through the real spawn path.

The other tick tests stand in for launching a supervisor so they can control
time. This one lets the tick start real processes, which is the only way to
find out whether the detaching, the identity in the MCP config, and the
schedule advancing all fit together.
"""

import time

from ai_crontab import config, core, model, tick


def wait_for(predicate, timeout=30):
    """Poll until the predicate holds, or give up and let the assert report."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_a_tick_carries_a_due_task_to_the_inbox(cfg, store, make_task):
    task = make_task("notify", schedule="* * * * *")
    _make_due(store, task)

    started = tick.tick(cfg, store)
    assert len(started) == 1

    assert wait_for(lambda: store.get_run(started[0]).state != model.ACTIVE)
    assert store.get_run(started[0]).state == model.OK
    assert store.unread_count() == 1


def test_the_tick_does_not_wait_for_the_run(cfg, store, make_task):
    """A ten-minute task must not hold the tick for ten minutes."""
    task = make_task("slow", schedule="* * * * *")
    _make_due(store, task)

    began = time.monotonic()
    tick.tick(cfg, store)
    assert time.monotonic() - began < 2, "the tick blocked on the run it started"
    assert store.count_active_runs() == 1


def test_a_second_tick_starts_nothing_while_the_first_run_is_going(cfg, store, make_task):
    task = make_task("slow", schedule="* * * * *")
    _make_due(store, task)

    tick.tick(cfg, store)
    assert tick.tick(cfg, store) == []
    assert store.count_active_runs() == 1


def test_the_task_comes_back_on_schedule_after_a_timeout(cfg, store, make_task):
    """FR-15 through the real path: a timeout is recorded and changes nothing."""
    task = make_task("slow", schedule="* * * * *")
    _make_due(store, task)
    started = tick.tick(cfg, store)

    assert wait_for(lambda: store.get_run(started[0]).state != model.ACTIVE)
    assert store.get_run(started[0]).state == model.TIMEOUT
    after = store.get_task(task.id)
    assert after.enabled
    assert after.next_run > config.utcnow()


def test_two_tasks_due_at_once_both_run(cfg, store, make_task):
    first = make_task("notify", name="first", schedule="* * * * *")
    second = make_task("notify", name="second", schedule="* * * * *")
    _make_due(store, first)
    _make_due(store, second)

    started = tick.tick(cfg, store)
    assert len(started) == 2
    assert wait_for(lambda: store.count_active_runs() == 0)
    assert store.unread_count() == 2


def _make_due(store, task):
    task.next_run = config.utcnow()
    store.update_task(task)
