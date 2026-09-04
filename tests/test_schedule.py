"""The schedule maths: catch-up, retirement, and daylight saving."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai_crontab import schedule
from ai_crontab.model import CRON, ONCE

PACIFIC = ZoneInfo("America/Los_Angeles")


def test_classify_tells_cron_from_a_timestamp():
    assert schedule.classify("30 8 * * 1-5")[0] == CRON
    assert schedule.classify("2026-09-04T09:00")[0] == ONCE
    with pytest.raises(schedule.ScheduleError):
        schedule.classify("next tuesday please")


def test_cron_is_read_in_the_users_timezone():
    """08:30 means half past eight where the user is, not in UTC."""
    after = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    fires = schedule.next_cron_time_after("30 8 * * *", after, PACIFIC)
    assert fires.astimezone(PACIFIC).hour == 8
    assert fires.astimezone(PACIFIC).minute == 30


def test_a_missed_week_produces_one_run_not_seven():
    """FR-6: catch up, once.

    A daily task last due seven days ago must come back onto its schedule with
    a single next time, computed forward from now.
    """
    task = _daily_task()
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    following = schedule.next_run_after_running(task, now, PACIFIC)
    assert following > now
    assert following - now < timedelta(days=1)


def test_a_one_time_task_retires_after_running():
    task = _daily_task()
    task.schedule_kind = ONCE
    task.schedule = "2026-09-04T09:00"
    assert schedule.next_run_after_running(task, datetime.now(UTC), PACIFIC) is None


def test_a_one_time_task_in_the_past_is_born_retired():
    """Writing "at 09:00" yesterday is a mistake, not an instruction to run now."""
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    assert schedule.first_run_time(ONCE, "2026-09-03T09:00", now, PACIFIC) is None
    assert schedule.first_run_time(ONCE, "2026-09-05T09:00", now, PACIFIC) is not None


def test_the_next_time_is_strictly_after_now():
    """A schedule must never resolve to the instant it was asked about.

    Otherwise a task launched exactly on its cron minute would be due again the
    moment its run ended, and run in a loop.
    """
    on_the_minute = datetime(2026, 9, 4, 15, 30, tzinfo=UTC)
    following = schedule.next_cron_time_after("* * * * *", on_the_minute, UTC)
    assert following > on_the_minute


def test_spring_forward_still_yields_a_real_instant():
    """2026-03-08 has no 02:30 in California; the schedule must still resolve."""
    before = datetime(2026, 3, 8, 8, 0, tzinfo=UTC)  # 00:00 Pacific
    fires = schedule.next_cron_time_after("30 2 * * *", before, PACIFIC)
    assert fires.tzinfo is not None
    assert fires > before


def test_fall_back_picks_one_instant_not_two():
    """The repeated hour must produce a single next time, not a double run."""
    before = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    first = schedule.next_cron_time_after("30 1 * * *", before, PACIFIC)
    second = schedule.next_cron_time_after("30 1 * * *", first, PACIFIC)
    assert second - first >= timedelta(hours=23)


def _daily_task():
    from ai_crontab.model import Task
    now = datetime(2026, 9, 4, tzinfo=UTC)
    return Task(id="t_x", name="daily", prompt="p", schedule_kind=CRON,
                schedule="30 8 * * *", working_dir="/tmp", time_limit_sec=600,
                enabled=True, next_run=now, created_by="user",
                created_at=now, updated_at=now)
