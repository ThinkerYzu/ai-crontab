"""The command line: a thin front end over core, tick and supervisor."""

import argparse
import sys

from . import config, core, schedule, supervisor, tick
from .core import DuplicateName, TaskNotFound
from .schedule import ScheduleError


def main(argv=None):
    """Parse a command line, run it, and return the process exit status.

    Returns 1 for anything the user got wrong (an unknown task, a schedule that
    does not parse) so that a shell or a cron line can tell.
    """
    args = _parser().parse_args(argv)
    cfg = config.load(args.root)
    with core.open_store(cfg) as store:
        try:
            return args.handler(cfg, store, args) or 0
        except (TaskNotFound, DuplicateName, ScheduleError) as exc:
            print(f"ai-crontab: {exc}", file=sys.stderr)
            return 1


def _parser():
    parser = argparse.ArgumentParser(
        prog="ai-crontab",
        description="Run AI agent tasks on a schedule, unattended.",
    )
    parser.add_argument("--root", help="state directory (default ~/.ai-crontab)")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("add", help="add a task")
    p.add_argument("--name", required=True)
    p.add_argument("--prompt", required=True, help="what the agent should do")
    p.add_argument("--schedule", required=True,
                   help="cron expression, or an ISO timestamp for a one-time task")
    p.add_argument("--dir", dest="working_dir", help="working directory for the run")
    p.add_argument("--time-limit", type=int, help="seconds; default from config")
    p.set_defaults(handler=_add)

    p = subs.add_parser("list", help="list tasks")
    p.set_defaults(handler=_list)

    p = subs.add_parser("show", help="show one task in full")
    p.add_argument("task")
    p.set_defaults(handler=_show)

    p = subs.add_parser("modify", help="change a task")
    p.add_argument("task")
    p.add_argument("--name")
    p.add_argument("--prompt")
    p.add_argument("--schedule")
    p.add_argument("--dir", dest="working_dir")
    p.add_argument("--time-limit", type=int, dest="time_limit_sec")
    p.set_defaults(handler=_modify)

    p = subs.add_parser("remove", help="delete a task and everything it kept")
    p.add_argument("task")
    p.set_defaults(handler=_remove)

    p = subs.add_parser("enable", help="put a task back on its schedule")
    p.add_argument("task")
    p.set_defaults(handler=lambda c, s, a: _set_enabled(c, s, a, True))

    p = subs.add_parser("disable", help="stop a task running, without deleting it")
    p.add_argument("task")
    p.set_defaults(handler=lambda c, s, a: _set_enabled(c, s, a, False))

    p = subs.add_parser("run-now", help="run a task immediately")
    p.add_argument("task")
    p.set_defaults(handler=_run_now)

    p = subs.add_parser("history", help="recent runs of a task")
    p.add_argument("task")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(handler=_history)

    p = subs.add_parser("handoff", help="show what a task left for its next run")
    p.add_argument("task")
    p.set_defaults(handler=_handoff)

    p = subs.add_parser("tick", help="run one scheduling pass (this is what cron calls)")
    p.set_defaults(handler=_tick)

    p = subs.add_parser("run", help="supervise one run (started by tick, not by hand)")
    p.add_argument("run_id")
    p.set_defaults(handler=_run)

    p = subs.add_parser("notify", help="the inbox")
    notify_subs = p.add_subparsers(dest="notify_command", required=True)
    q = notify_subs.add_parser("list", help="unread notifications")
    q.add_argument("-a", "--all", action="store_true", help="include ones already read")
    q.set_defaults(handler=_notify_list)
    q = notify_subs.add_parser("read", help="show one in full and mark it read")
    q.add_argument("id", type=int)
    q.set_defaults(handler=_notify_read)
    q = notify_subs.add_parser("mark-read", help="mark one, or all, as read")
    q.add_argument("id", type=int, nargs="?")
    q.set_defaults(handler=_notify_mark_read)
    q = notify_subs.add_parser("delete", help="delete one")
    q.add_argument("id", type=int)
    q.set_defaults(handler=_notify_delete)

    return parser


def _add(cfg, store, args):
    task = core.add_task(
        store, cfg, args.name, args.prompt, args.schedule,
        working_dir=args.working_dir, time_limit_sec=args.time_limit,
    )
    print(f"{task.id}  {task.name}  {schedule.describe(task, cfg.tz)}")
    if task.next_run is None:
        print("note: that time has already passed, so this task will not run.")


def _list(cfg, store, args):
    tasks = store.list_tasks()
    if not tasks:
        print("no tasks")
        return
    width = max(len(t.name) for t in tasks)
    for task in tasks:
        flag = " " if task.enabled else "-"
        print(f"{flag} {task.id}  {task.name:<{width}}  {schedule.describe(task, cfg.tz)}")


def _show(cfg, store, args):
    task = core.resolve(store, args.task)
    print(f"id           {task.id}")
    print(f"name         {task.name}")
    print(f"schedule     {task.schedule}  ({task.schedule_kind})")
    print(f"next run     {schedule.describe(task, cfg.tz)}")
    print(f"working dir  {task.working_dir}")
    print(f"time limit   {task.time_limit_sec}s")
    print(f"enabled      {task.enabled}")
    print(f"created by   {task.created_by}")
    print(f"\nprompt:\n{task.prompt}")


def _modify(cfg, store, args):
    changes = {k: v for k, v in vars(args).items()
               if k in ("name", "prompt", "schedule", "working_dir", "time_limit_sec")
               and v is not None}
    task = core.modify_task(store, cfg, args.task, **changes)
    print(f"{task.id}  {task.name}  {schedule.describe(task, cfg.tz)}")


def _set_enabled(cfg, store, args, enabled):
    task = core.modify_task(store, cfg, args.task, enabled=enabled)
    print(f"{task.name}  {schedule.describe(task, cfg.tz)}")


def _remove(cfg, store, args):
    task = core.remove_task(store, cfg, args.task)
    print(f"removed {task.name}")


def _run_now(cfg, store, args):
    run_id = tick.run_now(cfg, store, args.task)
    if run_id is None:
        print("already running", file=sys.stderr)
        return 1
    print(run_id)


def _history(cfg, store, args):
    task = core.resolve(store, args.task)
    runs = store.run_history(task.id, args.n)
    if not runs:
        print("no runs yet")
        return
    for run in runs:
        started = run.started_at.astimezone(cfg.tz)
        took = f"{(run.ended_at - run.started_at).total_seconds():.0f}s" if run.ended_at else "-"
        print(f"{started:%Y-%m-%d %H:%M}  {run.state:<8} {took:>6}  {run.id}"
              + (f"  {run.error}" if run.error else ""))


def _handoff(cfg, store, args):
    task = core.resolve(store, args.task)
    text = core.read_handoff(cfg, task.id)
    print(text if text else "no handoff")


def _tick(cfg, store, args):
    for run_id in tick.tick(cfg, store):
        print(run_id)


def _run(cfg, store, args):
    state = supervisor.supervise(cfg, store, args.run_id)
    if state is None:
        print(f"no active run {args.run_id}", file=sys.stderr)
        return 1
    print(state)


def _notify_list(cfg, store, args):
    items = store.list_notifications(unread_only=not args.all)
    if not items:
        print("nothing unread" if not args.all else "no notifications")
        return
    for note in items:
        mark = " " if note.is_unread else "."
        when = note.created_at.astimezone(cfg.tz)
        task = store.get_task(note.task_id)
        name = task.name if task else note.task_id
        print(f"{mark}{note.id:>4}  {when:%m-%d %H:%M}  {note.priority:<6} "
              f"{name}: {note.title}")


def _notify_read(cfg, store, args):
    note = store.get_notification(args.id)
    if note is None:
        print(f"no notification {args.id}", file=sys.stderr)
        return 1
    task = store.get_task(note.task_id)
    when = note.created_at.astimezone(cfg.tz)
    print(f"[{note.priority}] {note.title}")
    print(f"{task.name if task else note.task_id}  {when:%Y-%m-%d %H:%M %Z}\n")
    print(note.body)
    store.mark_read(args.id, config.utcnow())


def _notify_mark_read(cfg, store, args):
    if args.id is None:
        print(f"{store.mark_all_read(config.utcnow())} marked read")
    elif not store.mark_read(args.id, config.utcnow()):
        print(f"no unread notification {args.id}", file=sys.stderr)
        return 1


def _notify_delete(cfg, store, args):
    if not store.delete_notification(args.id):
        print(f"no notification {args.id}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
