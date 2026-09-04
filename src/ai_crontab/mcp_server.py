"""The tools a running agent calls back through, over the same core the CLI uses.

Started by the agent, never by a person: the supervisor writes a config naming
`ai-crontab mcp` and the agent's own process launches it for the length of one
run. Which task and which run is calling comes from the environment that config
sets rather than from any tool argument, so a confused or misled run cannot file
notifications as another task or overwrite another task's handoff
(D4 in DESIGN.md).

Nothing here decides anything. Every tool is a call into `core`, so the rules an
agent works under are the same ones the command line works under.
"""

import functools
import os
from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import core
from .config import utcnow
from .core import DuplicateName, TaskNotFound
from .schedule import ScheduleError

INSTRUCTIONS = """\
Your own schedule, and your only channel to the user.

You are one run of one ai-crontab task. These tools already know which task you
are, so none of them asks. Notifications you file wait in an inbox the user
reads when they choose; nothing is delivered to them at the moment you call.
"""

NOTIFY_DESCRIPTION = """\
File a notification into the user's inbox.

Call this only when something needs the user's attention. A run that turns up
nothing worth saying should file nothing: the inbox is a list of things that
need them, not a record of what ran, and a task that stays silent for weeks is
working correctly. Do not file a notification to report that you ran, that you
found nothing, or that you finished successfully.

Priority is "low", "normal" or "high"; anything else is read as "normal".
"""


def identity_from_env(env=None):
    """Which task and run this server is acting for, from its environment.

    Raises RuntimeError if either is missing. Every tool here writes on behalf
    of a run, so a server started by hand has no one to act for, and guessing
    is the failure D4 exists to prevent: refusing to start is the only safe
    answer.
    """
    env = os.environ if env is None else env
    task_id = env.get("AI_CRONTAB_TASK_ID")
    run_id = env.get("AI_CRONTAB_RUN_ID")
    if not task_id or not run_id:
        raise RuntimeError(
            "ai-crontab mcp is started by a run, not by hand: "
            "AI_CRONTAB_TASK_ID and AI_CRONTAB_RUN_ID must both be set"
        )
    return task_id, run_id


def build_server(cfg, task_id, run_id):
    """Define the tools, bound to one task and one run.

    Separate from `serve` so a test can build a server and call its tools
    without a transport underneath. The identity is closed over rather than
    passed per call, which is what keeps it out of the tool schemas the agent
    can see and fill in.
    """
    server = MCPServer(name="ai_crontab", instructions=INSTRUCTIONS)

    @server.tool(
        name="ai_crontab_list_tasks",
        description="List every scheduled task, including your own.",
    )
    @_anticipated
    def list_tasks() -> dict:
        with _store(cfg) as store:
            return _reply(
                store, task_id, run_id,
                tasks=[_task_view(t, cfg.tz) for t in store.list_tasks()],
            )

    @server.tool(
        name="ai_crontab_add_task",
        description=(
            "Schedule a new task. `schedule` is a cron expression read in the "
            "user's timezone, or an ISO timestamp for a task that runs once. "
            "`prompt` is what that task's agent will be told to do, with no one "
            "watching, so write it to stand on its own."
        ),
    )
    @_anticipated
    def add_task(name: str, prompt: str, schedule: str,
                 working_dir: str | None = None,
                 time_limit_sec: int | None = None) -> dict:
        with _store(cfg) as store:
            task = core.add_task(
                store, cfg, name, prompt, schedule,
                working_dir=working_dir, time_limit_sec=time_limit_sec,
                created_by="agent",
            )
            return _reply(store, task_id, run_id, task=_task_view(task, cfg.tz))

    @server.tool(
        name="ai_crontab_modify_task",
        description=(
            "Change a task, named by its id or its name, your own included. "
            "Only the fields you pass are touched. Changing the schedule "
            "recomputes when it next runs; "
            "passing enabled=false stops it running without deleting it."
        ),
    )
    @_anticipated
    def modify_task(task: str, name: str | None = None,
                    prompt: str | None = None, schedule: str | None = None,
                    working_dir: str | None = None,
                    time_limit_sec: int | None = None,
                    enabled: bool | None = None) -> dict:
        changes = {k: v for k, v in (
            ("name", name), ("prompt", prompt), ("schedule", schedule),
            ("working_dir", working_dir), ("time_limit_sec", time_limit_sec),
            ("enabled", enabled),
        ) if v is not None}
        with _store(cfg) as store:
            changed = core.modify_task(store, cfg, task, **changes)
            return _reply(store, task_id, run_id, task=_task_view(changed, cfg.tz))

    @server.tool(
        name="ai_crontab_remove_task",
        description=(
            "Delete a task along with its handoff, scratch files and run "
            "history. Removing your own task does not stop this run."
        ),
    )
    @_anticipated
    def remove_task(task: str) -> dict:
        with _store(cfg) as store:
            gone = core.remove_task(store, cfg, task)
            return _reply(store, task_id, run_id, removed=gone.name)

    @server.tool(name="ai_crontab_notify", description=NOTIFY_DESCRIPTION)
    @_anticipated
    def notify(title: str, body: str = "", priority: str = "normal") -> dict:
        with _store(cfg) as store:
            note_id = core.notify(store, task_id, run_id, title, body, priority)
            return _reply(store, task_id, run_id, notification_id=note_id)

    @server.tool(
        name="ai_crontab_write_handoff",
        description=(
            "Replace your task's handoff with notes for its next run, which "
            "will be shown the text you write here before it starts. Say where "
            "you got to and what is left, not what you did. This replaces the "
            "whole handoff rather than adding to it."
        ),
    )
    @_anticipated
    def write_handoff(content: str) -> dict:
        core.write_handoff(cfg, task_id, content)
        with _store(cfg) as store:
            return _reply(store, task_id, run_id, written_chars=len(content))

    @server.tool(
        name="ai_crontab_read_handoff",
        description=(
            "Read a task's handoff. Your own is already at the top of this "
            "run's prompt, so this is for reading another task's."
        ),
    )
    @_anticipated
    def read_handoff(task: str | None = None) -> dict:
        with _store(cfg) as store:
            wanted = core.resolve(store, task).id if task else task_id
            return _reply(store, task_id, run_id,
                          handoff=core.read_handoff(cfg, wanted))

    @server.tool(
        name="ai_crontab_time_left",
        description=(
            "Seconds before this run is stopped. You will also be told to wrap "
            "up shortly before that, but only at a turn boundary, so check here "
            "before starting anything long."
        ),
    )
    @_anticipated
    def time_left() -> dict:
        with _store(cfg) as store:
            return _reply(store, task_id, run_id)

    return server


def serve(cfg, env=None):
    """Run the stdio server for the length of one agent run.

    Returns once the agent closes the connection. Nothing may be printed to
    stdout from here on: stdout is the protocol.
    """
    task_id, run_id = identity_from_env(env)
    build_server(cfg, task_id, run_id).run("stdio")


@contextmanager
def _store(cfg):
    """A database connection for the length of one tool call.

    Opened per call rather than held for the process because the SDK may run a
    tool on any worker thread and a SQLite connection belongs to the thread
    that made it. The cost is one open against a file already in the page
    cache, a handful of times per run.
    """
    with core.open_store(cfg) as store:
        yield store


def _anticipated(fn):
    """Turn the mistakes an agent can make into errors it can read.

    A ToolError comes back as text the model sees; anything else the SDK
    reduces to "Error executing tool", so without this a schedule that does not
    parse would reach the agent with no way to tell what was wrong with it.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (TaskNotFound, DuplicateName, ScheduleError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def _reply(store, task_id, run_id, **fields):
    """One tool result, with the time remaining attached.

    Every result carries `time_left_sec` because a run busy elsewhere never
    thinks to ask for it, and the wrap-up message only arrives at a turn
    boundary (D3). It is a cheap second reminder on a channel the agent is
    already reading, not a replacement for either.
    """
    return {**fields, "time_left_sec": _time_left(store, task_id, run_id)}


def _time_left(store, task_id, run_id):
    """Seconds until the supervisor stops this run, or None if it cannot tell.

    Counted from the run's start time in the store, which is when the tick
    claimed the run rather than when the supervisor started the agent a moment
    later. The answer is therefore slightly short, which is the right direction
    to be wrong about a deadline.
    """
    run = store.get_run(run_id)
    task = store.get_task(task_id)
    if run is None or task is None:
        return None
    left = task.time_limit_sec - (utcnow() - run.started_at).total_seconds()
    return max(0, int(left))


def _task_view(task, tz):
    """One task as the agent sees it: times in the user's zone, no internals."""
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "schedule": task.schedule,
        "schedule_kind": task.schedule_kind,
        "next_run": (task.next_run.astimezone(tz).isoformat()
                     if task.next_run else None),
        "working_dir": task.working_dir,
        "time_limit_sec": task.time_limit_sec,
        "enabled": task.enabled,
        "created_by": task.created_by,
        "state": _task_state(task),
    }


def _task_state(task):
    """Whether a task will run again, in one word.

    `enabled` alone does not say it: an enabled one-time task that has already
    fired has nothing left to do, which is retired rather than scheduled.
    """
    if not task.enabled:
        return "disabled"
    return "scheduled" if task.next_run else "retired"
