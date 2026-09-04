"""The three things the store holds, and how times cross its boundary."""

from dataclasses import dataclass
from datetime import UTC, datetime

CRON = "cron"
ONCE = "once"

ACTIVE = "active"
OK = "ok"
FAILED = "failed"
TIMEOUT = "timeout"
LOST = "lost"

PRIORITIES = ("low", "normal", "high")


def to_iso(when):
    """A UTC instant as the text the database stores, or None."""
    return None if when is None else when.astimezone(UTC).isoformat()


def from_iso(text):
    """Parse a stored timestamp back into an aware datetime, or None.

    Rows written before this code existed cannot occur, so anything unparseable
    is a bug rather than something to tolerate.
    """
    return None if text is None else datetime.fromisoformat(text)


@dataclass
class Task:
    """One scheduled prompt.

    `next_run` is the whole of the scheduling state: a task is due when it is
    enabled and `next_run` has passed. A `None` means the task will never run
    again, which covers both a retired one-time task and a disabled one.
    """

    id: str
    name: str
    prompt: str
    schedule_kind: str
    schedule: str
    working_dir: str
    time_limit_sec: int
    enabled: bool
    next_run: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime

    @property
    def is_retired(self):
        return self.next_run is None and self.enabled


@dataclass
class Run:
    """One attempt at a task.

    A row whose `state` is `active` is the task's lock: while it exists no
    second run of the same task may start. `supervisor_pid` is None between the
    tick creating the row and the supervisor adopting it, which is why reaping
    treats a young pid-less run as still starting up rather than lost.
    """

    id: str
    task_id: str
    state: str
    supervisor_pid: int | None
    started_at: datetime
    ended_at: datetime | None
    exit_code: int | None
    num_turns: int | None
    cost_usd: float | None
    summary: str | None
    error: str | None


@dataclass
class Notification:
    """Something a run decided the user needs to see."""

    id: int
    task_id: str
    run_id: str | None
    priority: str
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None

    @property
    def is_unread(self):
        return self.read_at is None
