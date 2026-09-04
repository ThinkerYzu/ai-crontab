#!/usr/bin/env python3
"""A stand-in for `claude -p` that speaks the same stream-json on both pipes.

Every scheduling and supervision rule can be exercised against this in seconds,
with no network and no API spend. It picks its behaviour from the first word of
the prompt it is given, so a test chooses a mode by writing a task whose prompt
starts with it.

It also reads the `--mcp-config` the supervisor wrote, which is how the `notify`
and `handoff` modes reach the store. That doubles as a check that the config is
well formed and carries the run's identity.
"""

import json
import os
import subprocess
import sys
import time


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def assistant(text):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def result(text, is_error=False, turns=1):
    emit({"type": "result", "subtype": "success", "is_error": is_error,
          "result": text, "num_turns": turns, "total_cost_usd": 0.0})


def read_message(stream):
    """The text of the next user message, or None when the pipe closes."""
    line = stream.readline()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ""
    blocks = event.get("message", {}).get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def mcp_identity(argv):
    """The task id, run id and state root the supervisor put in the MCP config."""
    path = argv[argv.index("--mcp-config") + 1]
    env = json.load(open(path))["mcpServers"]["ai_crontab"]["env"]
    return env["AI_CRONTAB_TASK_ID"], env["AI_CRONTAB_RUN_ID"], env["AI_CRONTAB_ROOT"]


def main():
    argv = sys.argv[1:]
    opening = read_message(sys.stdin)
    if opening is None:
        return 1
    mode = _mode_from(opening)

    if mode == "crash":
        sys.stderr.write("fake agent failing on purpose\n")
        return 1

    if mode == "quiet":
        assistant("nothing to report")
        result("nothing to report")
        return 0

    if mode == "notify":
        task_id, run_id, root = mcp_identity(argv)
        _store_call(root, lambda cfg, store, core: core.notify(
            store, task_id, run_id, "something happened", "the body", "high"))
        assistant("filed one notification")
        result("filed one notification")
        return 0

    if mode == "handoff":
        task_id, run_id, root = mcp_identity(argv)
        _store_call(root, lambda cfg, store, core: core.write_handoff(
            cfg, task_id, "stopped at bug 1993004"))
        assistant("wrote a handoff")
        result("wrote a handoff")
        return 0

    if mode == "echo":
        # Prove the previous run's handoff reached this one: hand it back as
        # the run summary so a test can assert on it.
        assistant(opening.replace("\n", " "))
        result("echoed")
        return 0

    if mode == "wrapper":
        # Work until told to stop, then save a handoff inside the wrap-up
        # window. This is the behaviour the whole warning mechanism exists for.
        task_id, run_id, root = mcp_identity(argv)
        while True:
            message = read_message(sys.stdin)
            if message is None:
                return 0
            if "Time is nearly up" in message:
                _store_call(root, lambda cfg, store, core: core.write_handoff(
                    cfg, task_id, "wrapped up on the warning"))
                assistant("wrapping up")
                result("wrapped up")
                return 0

    if mode == "lingerer":
        # Finish the turn and then go on reading stdin, which is what real
        # `claude --input-format stream-json` does: it waits for another
        # message rather than exiting. Only a supervisor that closes the pipe
        # once the turn is over will see this run end before its deadline.
        assistant("done")
        result("done")
        while read_message(sys.stdin) is not None:
            pass
        return 0

    if mode == "spawner":
        # Leave a child running in this process group, then hang. Only a
        # process-group kill takes the child with it.
        marker = os.environ.get("FAKE_AGENT_MARKER", "/tmp/ai-crontab-spawned")
        subprocess.Popen(
            [sys.executable, "-c",
             f"import time,pathlib;pathlib.Path({marker!r}).write_text(str(__import__('os').getpid()));time.sleep(600)"]
        )
        time.sleep(600)
        return 0

    # "slow", and anything unrecognised: outlast the deadline, ignoring stdin.
    time.sleep(600)
    return 0


def _mode_from(opening):
    """The first word of the task's own prompt.

    The supervisor's preamble comes first in the message, so the prompt is
    found after the marker it puts in front of it.
    """
    marker = "--- Your task ---"
    body = opening.split(marker, 1)[1] if marker in opening else opening
    words = body.strip().split()
    return words[0].strip().lower() if words else "slow"


def _store_call(root, action):
    """Open the run's own store and do one thing to it.

    Stands in for the MCP tools, which are Phase 2. What matters for these
    tests is that the call lands in the right store as the right task, and that
    comes from the config the supervisor wrote, not from this process.
    """
    from ai_crontab import config, core
    cfg = config.load(root)
    with core.open_store(cfg) as store:
        action(cfg, store, core)


if __name__ == "__main__":
    sys.exit(main())
