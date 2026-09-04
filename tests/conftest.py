"""A throwaway state directory and a fake agent, for every test."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_crontab import config, core

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


@pytest.fixture
def cfg(tmp_path):
    """A config rooted in a temporary directory, pointed at the fake agent.

    Written to config.toml rather than set on the object, because a supervisor
    runs in a process of its own and reads the file: settings that live only in
    the test process would leave it talking to the real `claude`.

    The limits are shortened so the supervisor tests finish in seconds rather
    than the ten minutes a real task gets.
    """
    root = tmp_path / "state"
    root.mkdir(parents=True)
    (root / "config.toml").write_text(
        f'claude_bin = "{_agent_launcher(tmp_path)}"\n'
        "time_limit_sec = 3\n"
        "warn_before_sec = 1\n"
        "grace_sec = 1\n"
        "startup_grace_sec = 2\n"
        "prune_every_sec = 0\n"
    )
    return config.load(root)


def _agent_launcher(tmp_path):
    """A wrapper that runs the fake agent under this test run's interpreter.

    The supervisor executes `claude_bin` as a program, so a bare script would
    run under whatever `#!/usr/bin/env python3` finds, which is not the
    virtualenv holding ai-crontab's dependencies.
    """
    path = tmp_path / "fake-agent"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGENT}" "$@"\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_spawn(store):
    """Stand in for launching a supervisor, without running an agent.

    The placeholder has to be a real process carrying the run id on its command
    line, because that is how the reaper tells a live supervisor from a dead
    one. A test that only pretended to spawn would have its runs reaped as
    lost, and would be testing the reaper by accident.
    """
    placeholders = []

    def spawn(cfg, run_id):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", run_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        placeholders.append(proc)
        store.adopt_run(run_id, proc.pid)

    yield spawn
    for proc in placeholders:
        proc.kill()
        proc.wait()


@pytest.fixture
def store(cfg):
    with core.open_store(cfg) as s:
        yield s


@pytest.fixture(autouse=True)
def fake_agent_on_path(monkeypatch):
    """Make the fake agent executable as a program, and importable from it.

    The fake agent imports ai_crontab to stand in for the MCP tools, so it
    needs this checkout on its PYTHONPATH when it runs as a subprocess.
    """
    src = str(Path(__file__).parent.parent / "src")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    FAKE_AGENT.chmod(0o755)


@pytest.fixture
def make_task(cfg, store):
    """Add a task whose prompt selects a fake agent mode."""
    def add(mode="quiet", name=None, schedule="*/5 * * * *", **kw):
        return core.add_task(
            store, cfg, name or f"task-{mode}-{len(store.list_tasks())}",
            mode, schedule, working_dir=str(cfg.root), **kw
        )
    return add


@pytest.fixture
def soon(cfg):
    """An ISO timestamp a moment in the future, for a one-time task."""
    return (datetime.now(UTC) + timedelta(minutes=1)).astimezone(cfg.tz).isoformat()


@pytest.fixture
def at():
    """Build a UTC instant from an offset in minutes, for readable schedules."""
    base = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    def offset(minutes=0):
        return base + timedelta(minutes=minutes)
    return offset


def run_supervisor(cfg, run_id):
    """Drive one run to completion in a child process, as the tick would.

    Uses a real subprocess rather than calling supervise() in-process, because
    the kill path signals a process group and the reaper identifies a
    supervisor by its command line. Neither is exercised in-process.
    """
    return subprocess.run(
        [sys.executable, "-m", "ai_crontab", "--root", str(cfg.root), "run", run_id],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
    )
