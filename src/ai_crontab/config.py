"""Where state lives and what the defaults are."""

import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def utcnow():
    """The current instant, timezone aware.

    Scheduling code takes the current time as an argument rather than calling
    this, so that tests can drive the clock. This is only the default the
    entry points pass in.
    """
    return datetime.now(UTC)


def default_root():
    """The state directory, honouring AI_CRONTAB_ROOT.

    The environment variable is how a test, and the MCP server started for one
    run, reach a store other than the user's own.
    """
    env = os.environ.get("AI_CRONTAB_ROOT")
    return Path(env).expanduser() if env else Path.home() / ".ai-crontab"


@dataclass
class Config:
    """Tunables, read once per process from config.toml.

    Every duration is in seconds. `warn_before_sec` and `grace_sec` are
    subtracted from and added to a run's own time limit, so a task with a
    longer limit still gets the same wrap-up window unless it sets its own.

    `model` is None on purpose: a run then gets whatever `claude` itself
    picks, so the choice tracks the CLI rather than being frozen here. Set it
    only to hold a run to one model deliberately.

    `run_path` is the `PATH` every run gets, whichever door started it. It is
    set here rather than inherited so that a run launched from the web
    interface, which was started from a login shell, resolves the same programs
    as one launched by cron, which has almost no `PATH` at all
    ([Decision 11](DESIGN.md)).

    `tick_interval_sec` is what the cron line does, not what this tool does:
    nothing here can check it, because you own the cron line. It exists so the
    web interface can say the scheduler is overdue rather than only saying when
    it last ran. Getting it wrong makes that one indicator wrong and affects
    nothing else.
    """

    root: Path = field(default_factory=default_root)
    time_limit_sec: int = 600
    warn_before_sec: int = 60
    grace_sec: int = 5
    max_concurrent_runs: int = 4
    retention_days: int = 90
    prune_every_sec: int = 86400
    startup_grace_sec: int = 60
    claude_bin: str = "claude"
    run_path: str = "/usr/local/bin:/usr/bin:/bin"
    model: str | None = None
    timezone: str | None = None
    tick_interval_sec: int = 300
    web_port: int = 8474

    @property
    def db_path(self):
        return self.root / "state.db"

    @property
    def tasks_dir(self):
        return self.root / "tasks"

    @property
    def lock_path(self):
        return self.root / "tick.lock"

    @property
    def tz(self):
        """The zone cron expressions are read in.

        Defaults to the machine's local zone, which is what a person means by
        "08:30" when they write a schedule.
        """
        return ZoneInfo(self.timezone) if self.timezone else datetime.now().astimezone().tzinfo

    def task_dir(self, task_id):
        return self.tasks_dir / task_id

    def handoff_path(self, task_id):
        return self.task_dir(task_id) / "handoff.md"

    def scratch_dir(self, task_id):
        return self.task_dir(task_id) / "scratch"

    def transcript_path(self, task_id, run_id):
        return self.task_dir(task_id) / "runs" / f"{run_id}.jsonl"


def load(root=None):
    """Read config.toml, falling back to the defaults for anything absent.

    Creates the state directory if it is missing, so every entry point can
    assume it exists.
    """
    root = Path(root).expanduser() if root else default_root()
    cfg = Config(root=root)
    path = root / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for key, value in data.items():
            if hasattr(cfg, key) and key != "root":
                setattr(cfg, key, value)
    cfg.root.mkdir(parents=True, exist_ok=True)
    cfg.tasks_dir.mkdir(exist_ok=True)
    return cfg
