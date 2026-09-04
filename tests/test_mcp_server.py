"""The agent's side of the fence: the eight tools, and the identity behind them."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from ai_crontab import core, mcp_server
from ai_crontab.config import utcnow
from ai_crontab.web.app import make_app


def call(server, tool, /, **arguments):
    """Invoke one tool and return the JSON object it answered with.

    Goes through `call_tool` rather than the closure it wraps, so the argument
    schema and the JSON encoding are exercised too: a tool that only works when
    called as a Python function is not one the agent can use.

    The first two parameters are positional only because `name` is a tool
    argument here, and would otherwise collide with this function's own.
    """
    result = asyncio.run(server.call_tool(tool, arguments))
    return json.loads(result.content[0].text)


@pytest.fixture
def run(cfg, store, make_task):
    """A task with an active run, and a server bound to both.

    Returns the triple a test needs to check what a tool did: the task, the run
    id the notifications should carry, and the server to call.
    """
    task = make_task(name="watcher")
    run_id = store.claim_run(task.id, utcnow())
    return task, run_id, mcp_server.build_server(cfg, task.id, run_id)


# -- identity ------------------------------------------------------------


def test_identity_comes_from_the_environment():
    assert mcp_server.identity_from_env(
        {"AI_CRONTAB_TASK_ID": "t_1", "AI_CRONTAB_RUN_ID": "r_2"}
    ) == ("t_1", "r_2")


@pytest.mark.parametrize("env", [
    {},
    {"AI_CRONTAB_TASK_ID": "t_1"},
    {"AI_CRONTAB_RUN_ID": "r_2"},
    {"AI_CRONTAB_TASK_ID": "", "AI_CRONTAB_RUN_ID": "r_2"},
])
def test_a_server_with_no_run_to_act_for_refuses_to_start(env):
    with pytest.raises(RuntimeError):
        mcp_server.identity_from_env(env)


def test_the_cli_refuses_too_and_says_why(cfg):
    """Started by hand, `ai-crontab mcp` must fail loudly rather than serve.

    Run as a subprocess with the identity stripped from the environment,
    because that is the only way to see what a mistakenly configured agent
    would get.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("AI_CRONTAB_")}
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "ai_crontab", "--root", str(cfg.root), "mcp"],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=env,
    )
    assert proc.returncode == 1
    assert "AI_CRONTAB_TASK_ID" in proc.stderr
    assert proc.stdout == ""


def test_no_tool_asks_the_caller_which_task_it_is(run):
    """D4: identity is closed over, so it can never appear as an argument.

    The tools that write on the caller's behalf are the ones that matter here.
    If a task id ever became fillable on these, a run that misread its preamble
    could file notifications as another task.
    """
    _, _, server = run
    tools = {t.name: t.input_schema for t in asyncio.run(server.list_tools())}
    for name in ("ai_crontab_notify", "ai_crontab_write_handoff",
                 "ai_crontab_time_left"):
        properties = tools[name].get("properties", {})
        assert not any("task" in p or "run" in p for p in properties)


def test_there_are_eight_tools(run):
    _, _, server = run
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "ai_crontab_list_tasks", "ai_crontab_add_task", "ai_crontab_modify_task",
        "ai_crontab_remove_task", "ai_crontab_notify", "ai_crontab_write_handoff",
        "ai_crontab_read_handoff", "ai_crontab_time_left",
    }


# -- the schedule --------------------------------------------------------


def test_list_tasks_shows_the_agent_its_own_schedule(run):
    task, _, server = run
    listed = call(server, "ai_crontab_list_tasks")["tasks"]
    assert [t["id"] for t in listed] == [task.id]
    assert listed[0]["state"] == "scheduled"


def test_a_task_the_agent_adds_is_marked_as_its_own(run, store):
    _, _, server = run
    added = call(server, "ai_crontab_add_task",
                 name="follow-up", prompt="check whether anyone replied",
                 schedule="0 9 * * *")["task"]
    assert added["created_by"] == "agent"
    assert store.find_task("follow-up").prompt == "check whether anyone replied"


def test_a_schedule_that_does_not_parse_comes_back_readable(run):
    """The agent has to be told what was wrong, not just that something was.

    Anything but a ToolError reaches it as "Error executing tool", which leaves
    a run with no way to correct a schedule it typed badly.
    """
    _, _, server = run
    with pytest.raises(ToolError, match="not a cron expression"):
        asyncio.run(server.call_tool("ai_crontab_add_task", {
            "name": "bad", "prompt": "x", "schedule": "every other tuesday",
        }))


def test_a_duplicate_name_comes_back_readable(run):
    task, _, server = run
    with pytest.raises(ToolError, match="already exists"):
        asyncio.run(server.call_tool("ai_crontab_add_task", {
            "name": task.name, "prompt": "x", "schedule": "0 9 * * *",
        }))


def test_an_unknown_task_comes_back_readable(run):
    _, _, server = run
    with pytest.raises(ToolError, match="no task"):
        asyncio.run(server.call_tool("ai_crontab_remove_task", {"task": "nonesuch"}))


def test_the_agent_can_rewrite_its_own_task(run, store):
    task, _, server = run
    changed = call(server, "ai_crontab_modify_task", task=task.name,
                   prompt="watch the other queue", schedule="0 7 * * *")["task"]
    assert changed["schedule"] == "0 7 * * *"
    assert store.get_task(task.id).prompt == "watch the other queue"


def test_the_agent_can_retire_itself(run, store):
    """Disabling is how a task says its job is done without deleting the record."""
    task, _, server = run
    state = call(server, "ai_crontab_modify_task",
                 task=task.name, enabled=False)["task"]["state"]
    assert state == "disabled"
    assert store.get_task(task.id).next_run is None


def test_removing_a_task_takes_its_files_with_it(run, cfg, store):
    task, _, server = run
    core.write_handoff(cfg, task.id, "notes")
    assert call(server, "ai_crontab_remove_task", task=task.id)["removed"] == task.name
    assert store.get_task(task.id) is None
    assert not cfg.task_dir(task.id).exists()


# -- the inbox and the handoff -------------------------------------------


def test_a_notification_carries_the_run_that_filed_it(run, store):
    task, run_id, server = run
    note_id = call(server, "ai_crontab_notify",
                   title="queue grew", body="14 new bugs", priority="high")["notification_id"]
    note = store.get_notification(note_id)
    assert (note.task_id, note.run_id, note.priority) == (task.id, run_id, "high")
    assert note.is_unread


def test_a_priority_the_agent_invented_becomes_normal(run, store):
    """A notification is never worth losing over its label."""
    _, _, server = run
    note_id = call(server, "ai_crontab_notify", title="x", priority="URGENT!!")["notification_id"]
    assert store.get_notification(note_id).priority == "normal"


def test_the_handoff_round_trips(run, cfg):
    task, _, server = run
    call(server, "ai_crontab_write_handoff", content="got as far as bug 1992198")
    assert core.read_handoff(cfg, task.id) == "got as far as bug 1992198"
    assert call(server, "ai_crontab_read_handoff")["handoff"] == "got as far as bug 1992198"


def test_a_task_with_no_handoff_yet_reads_as_nothing(run):
    _, _, server = run
    assert call(server, "ai_crontab_read_handoff")["handoff"] is None


def test_the_agent_can_read_another_tasks_handoff(run, cfg, make_task):
    _, _, server = run
    other = make_task(name="sibling")
    core.write_handoff(cfg, other.id, "left the queue half done")
    assert call(server, "ai_crontab_read_handoff",
                task="sibling")["handoff"] == "left the queue half done"


def test_writing_a_handoff_only_ever_touches_the_callers_own(run, cfg, make_task):
    """The whole point of D4: `content` is the only argument, so there is no
    other task to write to even if the run wanted one."""
    _, _, server = run
    other = make_task(name="sibling")
    call(server, "ai_crontab_write_handoff", content="mine")
    assert core.read_handoff(cfg, other.id) is None


# -- the deadline --------------------------------------------------------


def test_every_result_carries_the_time_remaining(run):
    """D3's cheap supplement: a run that never asks still gets told.

    The task's limit is three seconds in the tests, so anything from zero to
    three is right; what matters is that the field is there on an ordinary
    result rather than only on `time_left`.
    """
    _, _, server = run
    assert 0 <= call(server, "ai_crontab_notify", title="x")["time_left_sec"] <= 3


def test_time_left_never_goes_negative(cfg, store, make_task):
    """A run past its deadline is being killed, not owed a negative number."""
    task = make_task(name="overdue")
    run_id = store.claim_run(task.id, utcnow() - timedelta(hours=1))
    server = mcp_server.build_server(cfg, task.id, run_id)
    assert call(server, "ai_crontab_time_left")["time_left_sec"] == 0


def test_time_left_is_measured_against_the_tasks_own_limit(cfg, store, make_task):
    task = make_task(name="patient", time_limit_sec=3600)
    run_id = store.claim_run(task.id, utcnow())
    server = mcp_server.build_server(cfg, task.id, run_id)
    assert 3590 <= call(server, "ai_crontab_time_left")["time_left_sec"] <= 3600


# -- over a real pipe ----------------------------------------------------


def test_a_launched_server_answers_a_real_client(cfg, store, make_task):
    """The whole path the agent takes: a subprocess, stdio, initialize, a call.

    Everything above this calls the server in-process, which cannot catch a
    server that fails to launch, prints to stdout, or reads its root from the
    wrong place. This launches it the way the supervisor's MCP config does,
    with the identity in the environment and no `--root`, and talks to it with
    a real client.
    """
    from mcp import Client, StdioServerParameters

    task = make_task(name="piped")
    run_id = store.claim_run(task.id, utcnow())
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_crontab", "mcp"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
            "AI_CRONTAB_ROOT": str(cfg.root),
            "AI_CRONTAB_TASK_ID": task.id,
            "AI_CRONTAB_RUN_ID": run_id,
        },
    )

    async def exchange():
        async with Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "ai_crontab_notify", {"title": "from a real pipe"}
            )
            return [t.name for t in tools.tools], result

    names, result = asyncio.run(exchange())
    assert len(names) == 8
    filed = json.loads(result.content[0].text)["notification_id"]
    assert store.get_notification(filed).task_id == task.id


def test_the_four_doors_produce_the_same_task(cfg, store, run):
    """SC-15 and SC-17: four front ends, not four behaviours.

    Everything but the identifying fields has to match. A daily cron is used
    because all four compute `next_run` forward from their own moment, and for
    a daily schedule those moments land on the same instant.

    The web interface joined this test rather than getting one of its own,
    which is the point of it: a door that is only compared against itself can
    drift from the other three without anything noticing.
    """
    _, _, server = run
    shared = dict(prompt="read the queue", schedule="0 9 * * *",
                  working_dir=str(cfg.root))

    subprocess.run(
        [sys.executable, "-m", "ai_crontab", "--root", str(cfg.root), "add",
         "--name", "by-cli", "--prompt", shared["prompt"],
         "--schedule", shared["schedule"], "--dir", shared["working_dir"]],
        capture_output=True, text=True, timeout=60, check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
    )
    core.add_task(store, cfg, "by-library", shared["prompt"], shared["schedule"],
                  working_dir=shared["working_dir"])
    call(server, "ai_crontab_add_task", name="by-mcp", **shared)
    TestClient(make_app(root=cfg.root), base_url="http://127.0.0.1:8474").post(
        "/tasks/new",
        data={"name": "by-web", "prompt": shared["prompt"],
              "schedule": shared["schedule"], "working_dir": shared["working_dir"]},
        headers={"host": "127.0.0.1:8474", "x-ai-crontab": "1"},
    )

    def comparable(task):
        return (task.prompt, task.schedule_kind, task.schedule, task.working_dir,
                task.time_limit_sec, task.enabled, task.next_run)

    stored = [comparable(store.find_task(n))
              for n in ("by-cli", "by-library", "by-mcp", "by-web")]
    assert stored[0] == stored[1] == stored[2] == stored[3]
