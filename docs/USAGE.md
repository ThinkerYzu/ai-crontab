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

Check what you wrote against what the dashboard reports, because the interval
you meant and the interval cron read are easy to part company. Hourly is
`0 * * * *`. It is not `* */1 * * *`: cron fires when every field matches, and
that line leaves the minute field at `*`, so it means every minute of every
hour. The tool cannot warn you, since it never sees your crontab, but the
dashboard shows when the last tick was and that is where the difference
appears.

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

Which of the two you meant is inferred rather than declared: anything that
parses as cron is cron, and everything else is tried as a timestamp. That
leaves more forms accepted than the two examples suggest.

| Form | Example | Means |
|------|---------|-------|
| Five fields | `30 8 * * 1-5` | 08:30 on weekdays |
| Named weekdays | `0 9 * * MON-FRI` | the same thing |
| Shorthand | `@daily`, `@hourly`, `@weekly`, `@monthly`, `@yearly`, `@midnight` | what they say, in your timezone |
| Six fields | `*/30 * * * * *` | the sixth field is **seconds**, not a year |
| Timestamp | `2026-09-10T14:00`, `2026-09-10 14:00`, `2026-09-10` | once; a bare date is midnight |
| Timestamp with offset | `2026-09-10T14:00:00-07:00` | once, at that instant |

`@reboot` is refused: nothing here is resident, so there is no boot to hook.

Two things the seconds field cannot do. A task runs no more often than your
cron line fires `ai-crontab tick`, so `*/30 * * * * *` on a five-minute tick is
a task that runs every five minutes. And the sixth field is why a year cannot
be smuggled in as `0 9 15 3 * 2027` — that is rejected because 2027 is not a
valid number of seconds, not because six fields are.

A schedule with no offset is read in your local timezone. A one-time task
retires after it runs. It stays in `list` with no next run, so you can read its
history, until you remove it.

**A timestamp already in the past is accepted, not refused.** The task is
created retired, on the reasoning that "at 09:00" written yesterday is a
mistake rather than an instruction to run now. `add` says so, and `list` shows
the task as `retired`, but a mistyped year gives you a task that will never
fire until you change its schedule.

The timestamp form is not just a convenience. Cron has no year field, so a
specific date in a specific year cannot be written as a cron expression at all,
and a sixth field naming one is rejected rather than ignored. See
[Schedules](../README.md#schedules) in the README for the full picture,
including what to do about a recurrence limited to particular years.

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

## The web interface

Optional, and installed separately:

```bash
pip install 'ai-crontab[web]'
ai-crontab web            # http://127.0.0.1:8474
```

It shows the schedule, a page per task with its prompt and handoff, the inbox,
and a page per run rendering that run's transcript as turns, with the tool
calls, their results, and what it cost. Reading a transcript is the thing the
command line does badly, and it is most of why the interface exists.

It also writes. Everything the CLI can do to a task, the interface can do:
add one, edit its prompt and schedule, enable and disable it, delete it, and
run it now. The inbox can be marked read, one notification at a time or all of
them, and a notification can be deleted. A run started from a browser is the
run cron would have started, down to the environment it gets, which is what
`run_path` below is for.

Nothing schedules the server and nothing depends on it. Ticks run whether it is
up or not, so leaving it down costs only the view.

**It listens on loopback and nowhere else, and it has no login.** That is safe
because of what it refuses, so it is worth knowing what those refusals are
before you change anything about how it is served:

- A request whose `Host` header is not loopback is refused, which stops a
  request that reached the port under another name.
- A request that would change something must carry an `X-AI-Crontab` header and
  must not declare somebody else's `Origin`.

The second one matters more than it looks. A task is a prompt that runs with
every tool pre-granted, so a form that writes a task prompt is a form that runs
code on your machine, and any page open in any browser can send a request to a
port on `127.0.0.1`. A password would not help, because your browser would
attach the cookie to that request for you. A header outside the CORS safelist
would need a preflight this server never approves, so the request is never
sent.

One consequence you can see: the pages submit their forms with JavaScript
rather than as plain form posts, because a form cannot add a header and a plain
post is refused. The interface needs JavaScript enabled to change anything. It
reads fine without.

Do not put this behind a reverse proxy, and do not change the bind address. It
has no authentication because it does not need any at loopback, and exposing it
would remove the one thing that makes that true.

---

## Configuration

`~/.ai-crontab/config.toml`. Every duration is in seconds.

| Key | Default | Meaning |
|-----|---------|---------|
| `claude_bin` | `"claude"` | The agent binary. Set this absolute |
| `run_path` | `"/usr/local/bin:/usr/bin:/bin"` | The `PATH` every run gets |
| `model` | unset | Passed as `--model`. Unset means `claude` chooses |
| `time_limit_sec` | `600` | How long a run gets, unless the task sets its own |
| `warn_before_sec` | `60` | How long before the limit the wrap-up warning is sent |
| `grace_sec` | `5` | How long after the limit before the process group is killed |
| `max_concurrent_runs` | `4` | Ceiling on runs in flight across all tasks |
| `retention_days` | `90` | How long notifications and run history are kept |
| `prune_every_sec` | `86400` | How often a tick prunes |
| `startup_grace_sec` | `60` | How long a run with no supervisor pid yet is left alone |
| `timezone` | machine local | The zone cron expressions are read in |
| `tick_interval_sec` | `300` | How often your cron line runs a tick. Display only |
| `web_port` | `8474` | The port `ai-crontab web` listens on, always on loopback |

`model` is unset on purpose. A run then takes whatever `claude` picks, so the
choice follows the CLI instead of being frozen in a config file. Set it only to
hold runs to one model deliberately.

A task over the concurrency ceiling stays due rather than being dropped, so the
next tick starts it.

`run_path` is set rather than inherited, and so is the rest of a run's
environment: a run keeps `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`,
`TZ` and the proxy variables from whoever started it, and nothing else. That is
what makes a run the same run whether cron, your terminal or the web interface
started it. Widen `run_path` if your tasks shell out to programs that live
somewhere else; the failure it prevents is a task that works when you click
"Run now" and fails at 08:30.

`tick_interval_sec` is what your cron line does, not what ai-crontab does.
Nothing here can check it, because you own that line. It exists so the web
interface can say the scheduler is overdue rather than only saying when it last
ran, and getting it wrong makes that one indicator wrong and nothing else.

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

Three commands answer three different questions, and a fourth says which
version is answering them:

```bash
ai-crontab history gtk-triage   # did it run, and how did it end
ai-crontab notify list          # did it speak, and only when it should have
cat ~/.ai-crontab/last-tick     # is the scheduler alive
ai-crontab --version            # which build this is
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
