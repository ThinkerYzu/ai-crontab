"""The inbox, retention, and the operations the three front ends share."""

from datetime import timedelta

from ai_crontab import core, model


def test_a_task_can_be_found_by_id_or_name(cfg, store, make_task):
    task = make_task("quiet", name="triage")
    assert store.find_task("triage").id == task.id
    assert store.find_task(task.id).name == "triage"


def test_names_are_unique(cfg, store, make_task):
    make_task("quiet", name="triage")
    try:
        core.add_task(store, cfg, "triage", "quiet", "*/5 * * * *")
    except core.DuplicateName:
        return
    raise AssertionError("a duplicate name must be refused")


def test_removing_a_task_takes_its_files_with_it(cfg, store, make_task):
    task = make_task("quiet")
    core.write_handoff(cfg, task.id, "notes")
    assert cfg.task_dir(task.id).exists()

    core.remove_task(store, cfg, task.name)
    assert not cfg.task_dir(task.id).exists()
    assert store.find_task(task.name) is None


def test_disabling_and_re_enabling_puts_a_task_back_on_schedule(cfg, store, make_task):
    task = make_task("quiet", schedule="*/5 * * * *")
    core.modify_task(store, cfg, task.name, enabled=False)
    assert store.get_task(task.id).next_run is None

    core.modify_task(store, cfg, task.name, enabled=True)
    assert store.get_task(task.id).next_run is not None


def test_unread_comes_first_and_high_priority_before_low(cfg, store, make_task, at):
    task = make_task("quiet")
    low = core.notify(store, task.id, None, "low one", priority="low", now=at(0))
    high = core.notify(store, task.id, None, "high one", priority="high", now=at(1))

    listed = [n.id for n in store.list_notifications()]
    assert listed == [high, low]

    store.mark_read(high, at(2))
    assert [n.id for n in store.list_notifications()] == [low]
    assert len(store.list_notifications(unread_only=False)) == 2


def test_an_unknown_priority_becomes_normal_rather_than_an_error(cfg, store, make_task):
    """A notification is never worth losing over its label."""
    task = make_task("quiet")
    note_id = core.notify(store, task.id, None, "t", priority="URGENT!!")
    assert store.get_notification(note_id).priority == "normal"


def test_pruning_keeps_what_the_user_has_not_seen(cfg, store, make_task, at):
    """SC-16: old records go; an unread notification is not a record."""
    task = make_task("quiet")
    old = at(0) - timedelta(days=cfg.retention_days + 1)
    read_note = core.notify(store, task.id, None, "old and read", now=old)
    unread_note = core.notify(store, task.id, None, "old and unread", now=old)
    store.mark_read(read_note, old)
    run_id = store.claim_run(task.id, old)
    store.finish_run(run_id, model.OK, old)

    core.prune(cfg, store, now=at(0))

    assert store.get_notification(read_note) is None
    assert store.get_notification(unread_note) is not None
    assert store.get_run(run_id) is None


def test_pruning_never_touches_a_running_run(cfg, store, make_task, at):
    task = make_task("quiet")
    old = at(0) - timedelta(days=cfg.retention_days + 1)
    run_id = store.claim_run(task.id, old)

    core.prune(cfg, store, now=at(0))
    assert store.get_run(run_id) is not None


def test_the_handoff_survives_being_rewritten(cfg, store, make_task):
    task = make_task("quiet")
    core.write_handoff(cfg, task.id, "first")
    core.write_handoff(cfg, task.id, "second")
    assert core.read_handoff(cfg, task.id) == "second"


def test_no_handoff_reads_as_nothing_rather_than_failing(cfg, store, make_task):
    task = make_task("quiet")
    assert core.read_handoff(cfg, task.id) is None
