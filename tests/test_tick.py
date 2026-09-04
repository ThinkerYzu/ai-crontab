"""Due selection, the concurrency ceiling, the overlap rule, and reaping."""

from datetime import timedelta

from ai_crontab import core, model, tick


def collect(cfg, store, now, spawn):
    """Run one tick and return the run ids it started."""
    return tick.tick(cfg, store, now=now, spawn=spawn)


def test_a_task_runs_when_it_is_due_and_not_before(cfg, store, make_task, at, fake_spawn):
    task = make_task("quiet", schedule="*/5 * * * *")
    _set_next_run(store, task, at(5))
    assert collect(cfg, store, at(0), fake_spawn) == []
    assert len(collect(cfg, store, at(6), fake_spawn)) == 1


def test_a_running_task_is_not_started_again(cfg, store, make_task, at, fake_spawn):
    """FR-7: an overlapping run is dropped, not queued."""
    task = make_task("slow")
    _set_next_run(store, task, at(0))
    assert len(collect(cfg, store, at(1), fake_spawn)) == 1
    assert collect(cfg, store, at(2), fake_spawn) == [], "second tick must not start a second run"
    assert store.count_active_runs() == 1


def test_the_dropped_run_is_not_queued_behind_the_first(cfg, store, make_task, at, fake_spawn):
    """Ten ticks against one long run leave one run in flight, not ten queued."""
    task = make_task("slow")
    _set_next_run(store, task, at(0))
    for minute in range(1, 11):
        collect(cfg, store, at(minute), fake_spawn)
    assert store.count_active_runs() == 1
    assert len(store.run_history(task.id)) == 1


def test_the_ceiling_caps_concurrent_runs(cfg, store, make_task, at, fake_spawn):
    for n in range(6):
        task = make_task("slow", name=f"t{n}")
        _set_next_run(store, task, at(0))
    started = collect(cfg, store, at(1), fake_spawn)
    assert len(started) == cfg.max_concurrent_runs


def test_a_task_over_the_ceiling_stays_due(cfg, store, make_task, at, fake_spawn):
    """The store is the queue: not launching is the whole implementation of "not yet"."""
    tasks = []
    for n in range(6):
        task = make_task("slow", name=f"t{n}")
        _set_next_run(store, task, at(0))
        tasks.append(task)
    collect(cfg, store, at(1), fake_spawn)
    still_due = {t.id for t in store.due_tasks(at(2))}
    assert len(still_due) == 6 - cfg.max_concurrent_runs
    for task_id in still_due:
        assert store.get_task(task_id).next_run is not None


def test_the_most_overdue_task_goes_first(cfg, store, make_task, at, fake_spawn):
    """Anti-starvation: a frequent task must not crowd out a daily one."""
    stale = make_task("slow", name="stale")
    fresh = make_task("slow", name="fresh")
    _set_next_run(store, stale, at(-600))
    _set_next_run(store, fresh, at(-1))
    cfg.max_concurrent_runs = 1
    started = collect(cfg, store, at(0), fake_spawn)
    assert store.get_run(started[0]).task_id == stale.id


def test_a_disabled_task_is_never_due(cfg, store, make_task, at, fake_spawn):
    task = make_task("quiet")
    _set_next_run(store, task, at(0))
    core.modify_task(store, cfg, task.name, enabled=False)
    assert collect(cfg, store, at(1), fake_spawn) == []


def test_a_dead_supervisor_is_reaped_and_frees_the_task(cfg, store, make_task, at, fake_spawn):
    """A machine that died mid-run must not lock a task out forever."""
    task = make_task("quiet")
    run_id = store.claim_run(task.id, at(0))
    store.adopt_run(run_id, 999999)          # a pid that is not running

    lost = tick.reap(cfg, store, at(1))
    assert lost == [run_id]
    assert store.get_run(run_id).state == model.LOST
    assert store.count_active_runs() == 0


def test_a_recycled_pid_does_not_keep_a_task_locked(cfg, store, make_task, at, fake_spawn):
    """Identity is the run id on the supervisor's command line, not the pid.

    This test's own process exists and is not supervising anything, which is
    exactly the shape of a pid that has been reused since the run started.
    """
    import os
    task = make_task("quiet")
    run_id = store.claim_run(task.id, at(0))
    store.adopt_run(run_id, os.getpid())

    assert tick.reap(cfg, store, at(1)) == [run_id]


def test_a_supervisor_that_has_not_started_yet_is_left_alone(cfg, store, make_task, at, fake_spawn):
    """A pid-less run is a supervisor still starting, until the grace runs out."""
    task = make_task("quiet")
    run_id = store.claim_run(task.id, at(0))

    assert tick.reap(cfg, store, at(0) + timedelta(seconds=1)) == []
    assert tick.reap(cfg, store, at(0) + timedelta(seconds=120)) == [run_id]


def test_two_ticks_cannot_start_the_same_task_twice(cfg, store, make_task, at, fake_spawn):
    """The unique partial index decides, whatever the timing."""
    task = make_task("slow")
    _set_next_run(store, task, at(0))
    assert store.claim_run(task.id, at(1)) is not None
    assert store.claim_run(task.id, at(1)) is None


def test_a_second_tick_declines_while_the_first_holds_the_lock(cfg, store, make_task, at, fake_spawn):
    task = make_task("quiet")
    _set_next_run(store, task, at(0))
    with tick._TickLock(cfg.lock_path) as held:
        assert held
        assert collect(cfg, store, at(1), fake_spawn) == []


def _set_next_run(store, task, when):
    task.next_run = when
    store.update_task(task)
    return task
