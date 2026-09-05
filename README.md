# ai-crontab

[![tests](https://github.com/ThinkerYzu/ai-crontab/actions/workflows/ci.yml/badge.svg)](https://github.com/ThinkerYzu/ai-crontab/actions/workflows/ci.yml)

Run AI agent tasks on a schedule, unattended, and let them reach you through an
inbox you read when you have time.

System cron can run a script on a schedule, but a script cannot read a bug
report and decide whether the reply matters. An agent can, but only when you
start a session and ask it to. ai-crontab schedules the agent instead of the
script: each entry is a prompt that `claude` carries out, one-time or recurring,
with nobody watching.

```
$ ai-crontab add --name gtk-triage --schedule "30 8 * * *" \
    --prompt "Check the untriaged GTK bug queue. If it grew since the
              watermark in your handoff, file one notification listing the
              new bugs. Otherwise file nothing."

$ ai-crontab notify list
    1  09-03 23:00  normal gtk-triage: 1 new untriaged bug in Core :: Widget: Gtk
```

Nothing is ever pushed at you. A run that finds something worth saying files a
notification; a run that turns up nothing files nothing, so the inbox stays a
list of things that need you rather than a log of everything that ran.

## Read this before you schedule anything

**A scheduled run has every tool granted in advance.** It starts `claude` with
`--permission-mode bypassPermissions` and `--permission-prompts none`, because
there is no terminal attached and nobody to approve anything. A run can
therefore edit files, run shell commands, and reach the network, on a timer,
while you are asleep.

That is the whole point of the tool and it is also its only real risk. The
security boundary is the prompt you wrote and the working directory you pointed
it at. There is no protection from a prompt you wrote carelessly, so give a
task the narrowest job that does what you want, and prefer a scratch directory
over your home directory or a repository you care about.

## Requirements

- Python 3.11 or newer
- [Claude Code](https://claude.com/claude-code) (the `claude` command) on the machine
- A working `cron`

## Install

```bash
git clone https://github.com/ThinkerYzu/ai-crontab
cd ai-crontab
python3 -m venv .venv && . .venv/bin/activate
pip install -e .          # add [web] for the browser interface
```

The wheel attached to the [latest release](https://github.com/ThinkerYzu/ai-crontab/releases/latest) works too, if
you would rather not keep a checkout.

Then point it at your `claude` binary, because cron's `PATH` will not find one
installed under your home directory:

```bash
mkdir -p ~/.ai-crontab
echo 'claude_bin = "'$(command -v claude)'"' > ~/.ai-crontab/config.toml
```

And install the cron line that drives it:

```cron
*/5 * * * * /path/to/ai-crontab/.venv/bin/ai-crontab tick >> ~/.ai-crontab/tick.log 2>&1 && date -Is > ~/.ai-crontab/last-tick
```

Any interval works. See [docs/USAGE.md](docs/USAGE.md) for what each half of
that line is for and how to check the installation.

## Schedules

`--schedule` takes one of two forms, and which one you used decides whether the
task ever runs again.

| Form | Example | Meaning |
|------|---------|---------|
| Cron expression | `30 8 * * *` | Every day at 08:30 |
| Cron expression | `0 9 15 3 *` | 09:00 on 15 March, every year |
| ISO timestamp | `2027-03-15T09:00` | 09:00 on 15 March 2027, once |
| Shorthand | `@daily`, `@hourly`, `@weekly`, `@monthly` | what they say, in your timezone |

**Cron has no year field.** Its five fields are minute, hour, day of month,
month, and day of week, so there is no way to write "15 March 2027" as a cron
expression. `0 9 15 3 *` is the closest, and it means every March for as long
as the task exists. A sixth field does not help: it is read as seconds, so
`0 9 15 3 * 2027` is rejected because 2027 is not a valid number of seconds
rather than quietly taken for a year.

A specific date in a specific year is an ISO timestamp instead, which makes a
one-time task:

```bash
ai-crontab add --name renewal-check --schedule "2027-03-15T09:00" \
  --prompt "Check whether the certificate was renewed. If it was not, file a
            high priority notification."
```

A one-time task retires after it runs. It stays in `list` with no next run so
you can still read its history, until you remove it.

Both forms are read in your local timezone unless the timestamp carries an
offset or a trailing `Z`. A timestamp already in the past creates a retired
task rather than running immediately, because "at 09:00" written yesterday is
usually a mistake rather than an instruction to run now.

What neither form expresses is a repeating schedule limited to particular
years, such as every 15 March in 2027 and 2029 but not 2028. Either add a
one-time task for each date, or let the task arrange its own follow-up: an
agent can call the MCP tools to schedule its next run, which is the same
mechanism behind "check back in three days if nobody has replied".

## How it works

**The tick is the unit of work.** There is no resident process. Cron runs
`ai-crontab tick`; it reads the task store, launches whatever is due, and
exits in milliseconds. That keeps the tool honest about a laptop that sleeps,
reboots, and travels, because a tick only has to be correct the next time it
runs. Any sequence of ticks at any spacing leaves the store consistent, and a
task that missed five due times over a weekend runs once rather than five
times.

**Silence is a valid answer.** Plenty of tasks exist to watch for a condition
that is usually false, so a task that reports nothing for three weeks is
working correctly. The scheduler therefore never reads silence as a fault: a
failing task is not disabled, not backed off, and never notified about. Its
failures go to the run history, where you look when you are debugging a task
rather than every morning.

**A run gets ten minutes, and the limit is a checkpoint.** A minute before the
deadline the agent is sent a real message telling it to stop and write down
where it got to. Each task keeps that **handoff** document, and the scheduler
puts it into the next run's prompt rather than leaving the agent to find it. So
"work through the triage queue" becomes a task that gets as far as it can,
notes where it stopped, and carries on tomorrow.

**Every run has a supervisor.** The tick exits immediately and the next one may
be an hour away, so something else has to be alive to enforce the limit. Each
run gets a small process that owns one agent, pushes the wrap-up warning at
nine minutes, kills the whole process group at ten, and records how it ended.
It is what guarantees a broken run can never be mistaken for a quiet one.

**Agents can change the schedule.** MCP tools let a task's agent add, remove,
and modify tasks, including its own. That is what turns "check back in three
days if nobody has replied" into something the agent arranges for itself.

**There is a web interface, and it stays on loopback.** `ai-crontab web` serves
the schedule, the inbox, and a page per run that renders that run's transcript
as turns, which is the one thing the command line does badly. It does
everything the CLI does to a task as well: add, edit, enable, disable, delete,
and run now. It is optional,
nothing depends on it, and it has no login because it listens on `127.0.0.1`
only and refuses any request that did not come from its own pages. Read
[the section in docs/USAGE.md](docs/USAGE.md#the-web-interface) before changing
how it is served: a task is a prompt that runs with every tool pre-granted, so
exposing that port is not the small thing it looks like.

## Documentation

- **[docs/USAGE.md](docs/USAGE.md)** covers every command, the config file, how
  to write a task prompt that works unattended, the tools an agent can call
  back through, and how to tell a healthy installation from a broken one.
- **[CHANGELOG.md](CHANGELOG.md)** is what changed in each release.
- **[docs/RELEASING.md](docs/RELEASING.md)** is how a release is cut.

## Development

```bash
pip install -e ".[dev,web]"
pytest -q          # 174 tests, about 20 seconds
```

The suite needs no network and starts no agent that costs money. A fake agent
in `tests/fake_agent.py` speaks the same `stream-json` protocol a real run
does, so every timing rule (the warning, the kill, the process-group cleanup)
is tested in seconds rather than minutes.

Every push runs that suite on Python 3.11 through 3.14, then builds the wheel
and installs it into an empty virtualenv to check that what ships actually
runs. A pushed `v*` tag turns into a release page with the wheel attached;
[docs/RELEASING.md](docs/RELEASING.md) has the procedure.

## License

MIT. See [LICENSE](LICENSE).
