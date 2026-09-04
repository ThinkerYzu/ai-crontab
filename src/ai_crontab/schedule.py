"""Turning a schedule and the present moment into the next run time."""

from datetime import UTC, datetime

from croniter import croniter

from .model import CRON, ONCE


class ScheduleError(ValueError):
    """A schedule that cannot be parsed."""


def classify(text):
    """Decide whether a schedule string is a cron expression or a timestamp.

    Returns the pair (kind, normalised text). Anything croniter accepts is
    cron, which is more than the five fields most people write: a sixth field
    for seconds, named weekdays like `MON-FRI`, and the `@daily` family of
    shorthands. Anything else is parsed as an ISO timestamp for a one-time
    task, so `"30 8 * * 1-5"`, `"@daily"` and `"2026-09-04T09:00"` are all
    accepted wherever a schedule is asked for.

    Only the format is checked here. A one-time time that has already passed
    parses fine and is refused later, by `first_run_time` returning None, which
    creates the task retired rather than firing it immediately.
    """
    text = text.strip()
    if croniter.is_valid(text):
        return CRON, text
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise ScheduleError(
            f"not a cron expression or an ISO timestamp: {text!r}"
        ) from None
    return ONCE, text


def next_cron_time_after(expression, after, tz):
    """The first time the expression fires strictly after the given instant.

    `after` and the result are both aware UTC datetimes; the expression itself
    is read in `tz`, because "30 8 * * 1-5" means half past eight where the
    user is, not in UTC.

    Across a daylight saving change the wall clock is not a bijection, and this
    resolves it the way Python does: an ambiguous time takes the first of its
    two instants, and a time that does not exist is mapped through the gap. A
    task can therefore run twice or not at all on those two days a year, which
    the catch-up rule already tolerates.
    """
    local = after.astimezone(tz).replace(tzinfo=None)
    upcoming = croniter(expression, local).get_next(datetime)
    return upcoming.replace(tzinfo=tz).astimezone(UTC)


def first_run_time(kind, schedule, now, tz):
    """When a newly added task should first run, or None if that is in the past.

    A one-time task whose moment has already gone is created retired rather
    than firing immediately, because "at 09:00" written yesterday is a mistake,
    not an instruction to run now.
    """
    if kind == CRON:
        return next_cron_time_after(schedule, now, tz)
    when = datetime.fromisoformat(schedule)
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    when = when.astimezone(UTC)
    return when if when > now else None


def next_run_after_running(task, now, tz):
    """Where a task's schedule points once the current run has been launched.

    Computed forward from `now` rather than from the time that just fired, so a
    task that missed a week of slots comes back onto its schedule with one run
    instead of replaying every slot it missed.

    A one-time task has nowhere left to point and returns None, which retires
    it.
    """
    if task.schedule_kind == ONCE:
        return None
    return next_cron_time_after(task.schedule, now, tz)


def describe(task, tz):
    """A schedule rendered for a person, in their own timezone."""
    if task.next_run is None:
        return "retired" if task.enabled else "disabled"
    local = task.next_run.astimezone(tz)
    return f"{task.schedule}  next {local:%Y-%m-%d %H:%M %Z}"
