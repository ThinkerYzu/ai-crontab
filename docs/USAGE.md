# Using ai-crontab

Everything the [README](../README.md) does not cover: the full command set, the
config file, how to write a prompt that works with nobody watching, the tools
an agent calls back through, and how to check a running installation.

- [Setting up](#setting-up)
- [Managing tasks](#managing-tasks)
- [The inbox](#the-inbox)
- [Writing a task prompt](#writing-a-task-prompt)
- [The handoff](#the-handoff)
- [Tools the agent can call](#tools-the-agent-can-call)
- [Configuration](#configuration)
- [Where state lives](#where-state-lives)
- [Checking a running installation](#checking-a-running-installation)
- [Troubleshooting](#troubleshooting)

---

## Setting up

### The cron line

```cron
*/5 * * * * /path/to/.venv/bin/ai-crontab tick >> ~/.ai-crontab/tick.log 2>&1 && date -Is > ~/.ai-crontab/last-tick
```

Use the absolute path to the binary. Cron's `PATH` is `/usr/bin:/bin`, which
will not find a virtual environment.

The interval is yours to choose and does not change behavior, only how late a
task starts. Every tick runs everything that became due since the last one, so
a five-minute interval and an hourly one differ in whether a task due at 09:00
starts at 09:05 or 10:00, never in whether it runs at all.

The two halves of the line record different things, and one file cannot do both
jobs:

| File | Written | Answers |
|------|---------|---------|
| `tick.log` | Appended, only when a tick has something to say | What went wrong |
| `last-tick` | Rewritten every tick that exits 0 | Whether the scheduler is alive |

`tick.log` cannot report liveness, because appending nothing to a file leaves
its modification time alone. On a quiet week the log keeps whatever timestamp
it was created with while cron runs faithfully every five minutes. The `&&`
before `last-tick` puts the heartbeat behind the tick's exit status, so a tick
that starts failing leaves it stale while the error goes to the log.

### `claude_bin`

This is the one setting worth getting right before anything else, because
getting it wrong produces a task that works every way you test it by hand and
fails only once scheduled:

```toml
# ~/.ai-crontab/config.toml
claude_bin = "/home/you/.local/bin/claude"
```

The default is a bare `claude`, which your login shell resolves from
`~/.local/bin` and cron does not.

Verify it the only way that actually proves anything, by running a tick with
the environment stripped to what cron provides:

```bash
ai-crontab add --name probe --dir ~ \
  --schedule "$(date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ)" \
  --prompt 'Run `id -un`. Write a one-line handoff. File no notification.'

env -i HOME=$HOME LOGNAME=$USER USER=$USER SHELL=/bin/sh PATH=/usr/bin:/bin \
  /bin/sh -c "/path/to/.venv/bin/ai-crontab tick"

ai-crontab history probe    # should reach `ok`
ai-crontab remove probe
```

---

## Managing tasks

Every command that names a task accepts either its id (`t_279c79`) or its name
(`gtk-triage`).

| Command | What it does |
|---------|--------------|
| `add` | Create a task. Needs `--name`, `--prompt`, `--schedule` |
| `list` | Every task, with its schedule and next run |
| `show <task>` | One task in full, including its prompt |
| `modify <task>` | Change any of `--name`, `--prompt`, `--schedule`, `--dir`, `--time-limit` |
| `remove <task>` | Delete the task and everything it kept |
| `disable <task>` | Stop it running, without deleting it |
| `enable <task>` | Put it back on its schedule |
| `run-now <task>` | Run it immediately, ignoring the schedule |
| `history <task>` | Recent runs, newest first. `-n` sets how many |
| `handoff <task>` | What the last run left for the next one |
| `tick` | One scheduling pass. This is what cron calls |

### Schedules

`--schedule` takes either a cron expression, read in your local timezone, or an
ISO timestamp for a task that runs once:

```bash
ai-crontab add --name morning --schedule "30 8 * * *"      --prompt "..."
ai-crontab add --name weekly  --schedule "0 9 * * 1"       --prompt "..."
ai-crontab add --name once    --schedule "2026-09-10T14:00:00Z" --prompt "..."
```

A one-time task retires after it runs. It stays in `list` with no next run, so
you can read its history, until you remove it.

### `run-now` returns before the run does

`run-now` prints a run id and exits in milliseconds. The run continues in the
background under its own supervisor, which is the same path a tick uses, so it
tests the real thing rather than bypassing it:

```
$ ai-crontab run-now gtk-triage
r_71a8baf9
$ ai-crontab history gtk-triage
2026-09-03 22:59  ok          85s  r_71a8baf9
```

### Run states

| State | Meaning | What the next tick does |
|-------|---------|-------------------------|
| `ok` | The agent finished its turn | Runs again at the next due time |
| `failed` | The agent exited non-zero, or reported an error | Runs again at the next due time |
| `timeout` | The run hit its limit and was killed | Runs again at the next due time |
| `lost` | The supervisor died without recording an ending | Frees the task, runs again |
| `active` | Still running. Also the task's lock | Will not start a second copy |

None of these disable a task. A task that has failed ten times in a row is
still enabled and still on schedule, because the alternative is a scheduler
that quietly stops doing things, and a task that only speaks when a condition
is true is indistinguishable from a broken one.

---

## The inbox

```bash
ai-crontab notify list          # unread
ai-crontab notify list -a       # including ones already read
ai-crontab notify read 3        # show one in full, and mark it read
ai-crontab notify mark-read 3   # mark one read
ai-crontab notify mark-read     # mark all read
ai-crontab notify delete 3
```

`list` shows only unread entries unless you pass `-a`. Within a listing, high
priority comes first and newest first after that. Priorities are `low`,
`normal` and `high`; anything else an agent invents becomes `normal` rather
than an error.

Notifications and run history are kept for 90 days. Pruning happens inside a
tick, at most once a day, and never touches an unread notification or a running
run.

---

## Writing a task prompt

A task prompt is the whole security boundary and the whole specification. Three
things separate one that works from one that hangs.

**Nobody can answer a question.** There is no terminal and no user. An agent
that stops to ask has hung, and will be killed at its time limit. Say so in the
prompt, and give it enough to decide with:

> Nobody is there to answer a question. Decide and act.

**Say when to stay silent, explicitly.** The default instinct is to report.
Tell the task what silence means, or the inbox fills with "checked, all clear"
within a week:

> If nothing is newer than the watermark, file nothing at all. Most runs will
> be silent, and a silent run is a correct run, not a failed one.

**Say what to do when the work fails.** A network error is usually not
something that needs a human:

> If the fetch fails, write that in the handoff, leave the watermark alone, and
> file nothing. An outage is not something the user needs to be told about.

### A worked example

```bash
ai-crontab add --name gtk-triage --schedule "30 8 * * *" \
  --dir ~/.ai-crontab/work/gtk-triage \
  --prompt 'Watch the untriaged Core :: Widget: Gtk bug queue, and speak only
when it grows.

1. Fetch https://bugzilla.mozilla.org/rest/bug?... with curl into your scratch
   directory, and parse it with jq.
2. Your handoff records a watermark, an ISO date. Report every bug whose
   creation_time is newer than it. If there is no handoff, use the last 7 days.
3. If there are none, file nothing.
4. If there are some, file one notification listing each: bug number, date,
   severity, summary, link. One notification per run, not one per bug.
5. Always call ai_crontab_write_handoff before you finish. Record the new
   watermark and the current queue size.

If the fetch fails, say so in the handoff and file nothing. Nobody is there to
answer a question.'
```

Note `--dir`. The run's working directory is where the agent starts, and giving
a task its own directory rather than a repository you care about limits what a
mistake can reach. It also decides which project-scoped MCP servers the run
picks up, since `claude` keys those on the directory.

---

## The handoff

Every task has one document that the agent writes for the next run of the same
task, plus a scratch directory for working files. The scheduler injects the
previous handoff into the next run's prompt, so continuity never depends on the
agent remembering to look for it.

```bash
ai-crontab handoff gtk-triage
```

This is what makes the ten-minute limit a checkpoint rather than lost work, and
what lets an investigation span days. It is deliberately not a resumed
conversation: a daily task running for a year would carry a year of context
into every run and pay for all of it each time. A handoff the agent writes on
purpose is smaller, readable by you, and still useful when the model or the
prompt changes underneath it.

---

## Tools the agent can call

Each run gets its own MCP server, and the tools already know which task and
which run they belong to, so none of them takes a task id for the caller's own
task. A run cannot claim to be a different one.

| Tool | Purpose |
|------|---------|
| `ai_crontab_list_tasks` | Every scheduled task, including its own |
| `ai_crontab_add_task` | Schedule a new task |
| `ai_crontab_modify_task` | Change a task, including its own |
| `ai_crontab_remove_task` | Delete a task and its files |
| `ai_crontab_notify` | File one notification: `title`, `body`, `priority` |
| `ai_crontab_write_handoff` | Leave notes for the next run |
| `ai_crontab_read_handoff` | Read a handoff, its own by default |
| `ai_crontab_time_left` | Seconds remaining in this run |

Every tool result also carries `time_left_sec`, so an agent doing a long job
learns how much of its limit is gone without having to ask.

The warning at nine minutes is a real message pushed into the running
conversation over stdin, not a hint attached to a tool result, because an agent
busy elsewhere never sees the latter. It arrives at the next turn boundary, so
a run inside a long tool call will not see it until that call returns. The hard
kill at ten minutes is the only guaranteed ending.

---

## Configuration

`~/.ai-crontab/config.toml`. Every duration is in seconds.

| Key | Default | Meaning |
|-----|---------|---------|
| `claude_bin` | `"claude"` | The agent binary. Set this absolute |
| `model` | unset | Passed as `--model`. Unset means `claude` chooses |
| `time_limit_sec` | `600` | How long a run gets, unless the task sets its own |
| `warn_before_sec` | `60` | How long before the limit the wrap-up warning is sent |
| `grace_sec` | `5` | How long after the limit before the process group is killed |
| `max_concurrent_runs` | `4` | Ceiling on runs in flight across all tasks |
| `retention_days` | `90` | How long notifications and run history are kept |
| `prune_every_sec` | `86400` | How often a tick prunes |
| `startup_grace_sec` | `60` | How long a run with no supervisor pid yet is left alone |
| `timezone` | machine local | The zone cron expressions are read in |

`model` is unset on purpose. A run then takes whatever `claude` picks, so the
choice follows the CLI instead of being frozen in a config file. Set it only to
hold runs to one model deliberately.

A task over the concurrency ceiling stays due rather than being dropped, so the
next tick starts it.

---

## Where state lives

Everything is under `~/.ai-crontab`, or wherever `AI_CRONTAB_ROOT` points:

```
~/.ai-crontab/
  config.toml
  state.db                    SQLite: tasks, runs, notifications
  tick.lock                   so two ticks cannot overlap
  last-tick                   heartbeat, written by the cron line
  tick.log                    tick output, written by the cron line
  tasks/<task_id>/
    handoff.md                what the last run left for the next
    scratch/                  the task's working files
    runs/<run_id>.jsonl       the full transcript of one run
    runs/<run_id>.stderr
```

Transcripts are the record of what an agent actually did, and are the first
place to look when a task did something surprising.

---

## Checking a running installation

Three commands answer three different questions:

```bash
ai-crontab history gtk-triage   # did it run, and how did it end
ai-crontab notify list          # did it speak, and only when it should have
cat ~/.ai-crontab/last-tick     # is the scheduler alive
```

**An empty inbox is the expected result.** A task that files nothing for three
weeks is working. What is wrong is a gap in the history: a due time with no run
against it means the tick did not fire, which is a scheduler problem rather
than a task one.

| Symptom | Reading |
|---------|---------|
| Inbox empty, history has a run per due time | Healthy and quiet |
| Inbox empty, history missing a due time | The tick did not fire |
| `last-tick` older than a couple of intervals | Cron is not running the line, or the tick is failing |
| `tick.log` has grown | A tick wrote something, which means an error |
| History shows `timeout` repeatedly | The task needs more than its limit, or hangs |
| History shows `lost` | A supervisor died without recording an ending |

---

## Troubleshooting

### A task works by hand and fails on cron

**Symptom:** `run-now` gives `ok`, but every scheduled run is `failed` with an
empty transcript.
**Cause:** `claude_bin` is a bare `claude`, which cron's `PATH` cannot resolve.
**Fix:** set it absolute in `config.toml`, then run the stripped-environment
test in [Setting up](#claude_bin).

### A task never starts and nothing is recorded

**Symptom:** the due time passes and the run history is unchanged.
**Cause:** usually the task is disabled, or an earlier run still holds the task
lock. A run in state `active` blocks every later run of the same task by
design, so that a slow task cannot accumulate copies of itself.
**Fix:** `ai-crontab show <task>` reports whether it is enabled and when it is
next due. A stuck lock resolves itself: the next tick reaps a supervisor whose
process is gone and records that run as `lost`.

### A run did its work but is recorded as a timeout

**Symptom:** the transcript shows the task finishing in seconds, the handoff is
on disk, and the state is `timeout` at the full limit.
**Cause:** the supervisor waited for `claude` to exit, and it did not.
`--input-format stream-json` leaves it reading stdin for another message after
its turn ends.
**Fix:** already handled. The supervisor closes the agent's stdin on the
`result` event, which is the agent saying its turn is over. If this reappears,
the agent's behavior has changed and
`tests/test_supervisor.py::test_an_agent_that_waits_for_more_input_is_still_let_go`
is the regression to look at.

### A run behaved unlike anything you expected

Find out what it actually ran with. The first line of any transcript reports
the model and the MCP servers the run really had:

```bash
grep -m1 '"subtype":"init"' ~/.ai-crontab/tasks/<task_id>/runs/<run_id>.jsonl \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["model"], [s["name"] for s in d["mcp_servers"]])'
```

Two things surprise people here. The model can change between runs, because
`model` is unset by default. And the server list is usually longer than the one
ai-crontab provides, because `--mcp-config` adds to your existing `claude`
configuration rather than replacing it. That is what lets a task use MCP
servers you have already set up, and it also means a task pays for the tool
schemas of servers it never uses.
