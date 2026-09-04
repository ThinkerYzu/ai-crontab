"""One run: start the agent, warn it, kill it, and always record an ending."""

import json
import os
import signal
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

from . import core, model, prompt, schedule
from .config import utcnow

SUMMARY_LIMIT = 4000


def user_message(text):
    """One stream-json user message, as the line the agent reads from stdin."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def supervisor_alive(pid, run_id):
    """Whether the process supervising this run is still there.

    Matching the run id against the process's own command line rather than
    trusting the pid alone is what makes pid reuse harmless: a recycled pid
    belongs to some other program, whose command line cannot contain this run's
    id. Without the check a reused pid would keep a task locked forever, and
    the symptom would be a task that quietly stopped running.
    """
    if pid is None:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return run_id.encode() in cmdline


def write_mcp_config(cfg, task, run_id):
    """Write the per-run MCP config and return its path.

    The task and run ids go in the server's environment, not in the tool
    arguments, so a run cannot file notifications or overwrite a handoff as any
    task but its own.
    """
    path = cfg.task_dir(task.id) / "runs" / f"{run_id}.mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mcpServers": {
            "ai_crontab": {
                "command": _own_binary(),
                "args": ["mcp"],
                "env": {
                    "AI_CRONTAB_ROOT": str(cfg.root),
                    "AI_CRONTAB_TASK_ID": task.id,
                    "AI_CRONTAB_RUN_ID": run_id,
                },
            }
        }
    }, indent=2))
    return path


def agent_command(cfg, task, mcp_config):
    """The argv that starts one agent run."""
    argv = [
        cfg.claude_bin, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--permission-prompts", "none",
        "--mcp-config", str(mcp_config),
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    return argv


class _Reader(threading.Thread):
    """Drains the agent's stdout into the transcript and keeps the last word.

    Runs on its own thread because the supervisor's main loop has to stay free
    to enforce the deadline: a blocking read on an agent that has stopped
    talking would let the limit pass unnoticed.
    """

    def __init__(self, stream, transcript_path):
        super().__init__(daemon=True)
        self.stream = stream
        self.transcript_path = transcript_path
        self.summary = None
        self.result = None
        self.finished = threading.Event()

    def run(self):
        transcript = open(self.transcript_path, "a")
        try:
            for line in self.stream:
                transcript.write(line)
                transcript.flush()
                self._observe(line)
        except (ValueError, OSError):
            pass
        finally:
            transcript.close()

    def _observe(self, line):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.summary = block["text"].strip()[:SUMMARY_LIMIT]
        elif event.get("type") == "result":
            self.result = event
            self.finished.set()


def supervise(cfg, store, run_id, now=None):
    """Carry one run from launch to a recorded ending.

    Called as `ai-crontab run <run-id>` by the tick, in a process of its own,
    because the tick exits within milliseconds and the next one may be an hour
    away: nothing else would be alive to enforce the run's time limit.

    Returns the state the run finished in. The run is always left with an
    ending written, whatever happened, so that a broken run can never be
    mistaken for a quiet one.
    """
    now = now or utcnow()
    run = store.get_run(run_id)
    if run is None or run.state != model.ACTIVE:
        return None
    task = store.get_task(run.task_id)
    store.adopt_run(run_id, os.getpid())

    deadline = now + timedelta(seconds=task.time_limit_sec)
    warn_at = deadline - timedelta(seconds=min(cfg.warn_before_sec,
                                               task.time_limit_sec // 2))
    state, exit_code, error = model.FAILED, None, None
    reader = None
    stderr_file = None
    try:
        mcp_config = write_mcp_config(cfg, task, run_id)
        opening = prompt.build(
            cfg, task, now, deadline, handoff=core.read_handoff(cfg, task.id)
        )
        transcript = cfg.transcript_path(task.id, run_id)
        transcript.parent.mkdir(parents=True, exist_ok=True)

        stderr_file = open(transcript.with_suffix(".stderr"), "w")
        proc = subprocess.Popen(
            agent_command(cfg, task, mcp_config),
            cwd=task.working_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        reader = _Reader(proc.stdout, transcript)
        reader.start()
        _send(proc, user_message(opening))

        state, exit_code, error = _wait(cfg, proc, reader, warn_at, deadline)
    except Exception as exc:  # a supervisor that dies silently is the one thing we cannot have
        state, error = model.FAILED, f"{type(exc).__name__}: {exc}"
    finally:
        if stderr_file is not None:
            stderr_file.close()
        if reader is not None:
            reader.join(timeout=5)
        result = reader.result if reader else None
        if state == model.OK and result and result.get("is_error"):
            state, error = model.FAILED, str(result.get("result", ""))[:SUMMARY_LIMIT]
        store.finish_run(
            run_id, state, utcnow(),
            exit_code=exit_code,
            summary=(reader.summary if reader else None),
            error=error,
            num_turns=(result or {}).get("num_turns"),
            cost_usd=(result or {}).get("total_cost_usd"),
        )
        _advance_schedule(cfg, store, task)
    return state


def _wait(cfg, proc, reader, warn_at, deadline):
    """Poll the agent until it exits or its deadline arrives.

    Pushes the wrap-up message once, at `warn_at`, and does not wait for a
    reply: the message is delivered at the agent's next turn boundary, which
    may never come if it is stuck inside a long tool call. The kill is what
    actually guarantees the run ends.
    """
    warned = False
    closed = False
    while True:
        code = proc.poll()
        if code is not None:
            return (model.OK if code == 0 else model.FAILED), code, None
        now = utcnow()

        # The agent has answered, so close its input and let it exit.
        #
        # `--input-format stream-json` leaves claude reading stdin after it
        # finishes a turn, waiting for another message that is never coming.
        # Without this every successful run sits until its deadline and is
        # recorded as a timeout, which is what the first live run did. The
        # result event is the agent saying its turn is over, and it is the only
        # signal that the work is done rather than merely slow.
        if not closed and reader.finished.is_set():
            _close_stdin(proc)
            closed = True
        elif not warned and now >= warn_at:
            _send(proc, user_message(
                prompt.wrap_up((deadline - now).total_seconds())
            ))
            warned = True
        if now >= deadline:
            _kill(proc, cfg.grace_sec)
            return model.TIMEOUT, proc.poll(), "exceeded its time limit"
        time.sleep(0.2)


def _send(proc, line):
    """Write one message to the agent, ignoring a pipe that has already closed."""
    try:
        proc.stdin.write(line)
        proc.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _close_stdin(proc):
    """Close the agent's input, which is what lets it exit of its own accord."""
    try:
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _kill(proc, grace_sec):
    """End the run and everything it started.

    Signals the process group rather than the agent alone, because a run that
    launched a browser or a build would otherwise leave those behind on every
    timeout. SIGTERM first gives the agent a chance to flush its output.
    """
    try:
        group = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace_sec)
            return
        except subprocess.TimeoutExpired:
            continue


def _advance_schedule(cfg, store, task):
    """Point a task at its next run once this one has finished.

    Done here rather than in the tick so that a task never comes due again
    while the run it is waiting on is still going: the tick's own guard is the
    active run, and this closes the window after that run ends.

    A task the agent disabled or deleted during the run is left alone.
    """
    current = store.get_task(task.id)
    if current is None or not current.enabled:
        return
    now = utcnow()
    current.next_run = schedule.next_run_after_running(current, now, cfg.tz)
    current.updated_at = now
    store.update_task(current)


def _own_binary():
    """The ai-crontab command, as an absolute path cron and MCP can both use."""
    import shutil
    import sys
    found = shutil.which("ai-crontab")
    if found:
        return found
    return str(Path(sys.executable).parent / "ai-crontab")
