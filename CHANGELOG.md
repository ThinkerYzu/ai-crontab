# Changelog

Notable changes per release. The version at the top of a section is the one
`ai-crontab --version` reports and the one its tag carries.

## Unreleased

- **Changed:** the Schedule tab hides completed one-time tasks by default.
  “Show completed” includes them again; running and unsuccessful tasks remain
  visible, and history and notifications are retained.

- **Fixed:** a write in the web interface left the page showing what it showed
  before. Most writes send the browser back to the page the form was on — an
  edit, an enable, anything on the inbox — and a URL that differs only in its
  fragment is a same-document navigation, so the page never reloaded and never
  said "saved". The change landed in the store either way, which is what made
  it look like the save had been ignored.
- **Fixed:** the inbox's per-notification `Delete` and `Mark read` buttons
  answered "Not Found" and did nothing. Their forms carry a field named
  `action`, which in a browser shadows the form's own `action` property, so
  the request went to a path nothing serves. `Mark all read` was unaffected.

## 0.1.0 - 2026-09-04

First release: a scheduler for AI agent tasks, with an inbox you pull from.

- **The tick.** System cron runs `ai-crontab tick`; it launches whatever became
  due since the last one and exits. There is no resident process, and any
  sequence of ticks at any spacing leaves the store consistent, so a task that
  missed five due times over a weekend runs once rather than five times.
- **A run gets ten minutes.** A supervisor process owns each agent, pushes a
  wrap-up message into the running conversation at nine minutes, kills the
  process group at ten, and records how the run ended. Every run has a recorded
  ending, so a broken one is never mistaken for a quiet one.
- **The handoff.** Each task keeps a document its next run starts from, which
  the scheduler puts into the prompt. The time limit becomes a checkpoint
  rather than lost work.
- **The inbox.** A run that finds something worth saying files a notification;
  a run that turns up nothing files nothing. Nothing is pushed at you, and a
  failing task is never disabled, backed off, or notified about.
- **Four front ends over one store:** a CLI of sixteen commands, a Python
  library, an MCP server that lets a task's agent change the schedule
  (including its own), and a local web interface at `ai-crontab web` that
  renders a run's transcript as turns and shows a run while it happens.
- **Schedules** are cron expressions, croniter shorthand such as `@daily`, or
  an ISO timestamp for a one-time task. Cron has no year field, so a specific
  date in a specific year is a timestamp.

Two things to know before scheduling anything, both in
[the README](README.md): a run has every tool granted in advance, so the task
prompt is the security boundary; and `claude_bin` has to be an absolute path,
because a bare `claude` resolves from a login shell and not from cron.
