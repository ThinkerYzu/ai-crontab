"""What the agent is actually shown at the start of a run, and at the end."""

PREAMBLE = """\
You are ai-crontab task "{name}" (id: {task_id}).
This run started at {started} and will be stopped at {deadline}.
About {warn} before that you will be told to wrap up.
Working directory: {working_dir}
Scratch directory: {scratch}

Nobody is watching this run. There is no one to answer a question, so decide
and act rather than asking. Finish, or leave a handoff saying where you got to.

Call ai_crontab_notify only if something needs the user's attention. A run that
turns up nothing worth saying should file nothing: the inbox is a list of
things that need them, not a record of what ran.

Call ai_crontab_write_handoff to leave notes for your next run.
"""

HANDOFF_BLOCK = """
--- Handoff from your previous run ---
{handoff}
--- End of handoff ---
"""

TASK_BLOCK = """
--- Your task ---
{prompt}
"""

WRAP_UP = """\
Time is nearly up: this run will be stopped in about {seconds} seconds.

Stop what you are doing now. Do not start anything new. If you have something
worth handing to your next run, call ai_crontab_write_handoff with it, then
finish. If something needs the user's attention, call ai_crontab_notify first.
"""


def build(cfg, task, run_started, deadline, handoff=None):
    """Assemble the opening message for a run.

    The preamble comes first, then the previous handoff, then the task's own
    prompt. The handoff is placed here rather than left for the agent to find,
    so continuity does not depend on it remembering to look.

    Times are rendered in the user's timezone, since they exist for the agent
    to read rather than to compute with.
    """
    parts = [PREAMBLE.format(
        name=task.name,
        task_id=task.id,
        started=f"{run_started.astimezone(cfg.tz):%Y-%m-%d %H:%M %Z}",
        deadline=f"{deadline.astimezone(cfg.tz):%H:%M %Z}",
        warn=_duration(cfg.warn_before_sec),
        working_dir=task.working_dir,
        scratch=cfg.scratch_dir(task.id),
    )]
    if handoff and handoff.strip():
        parts.append(HANDOFF_BLOCK.format(handoff=handoff.strip()))
    parts.append(TASK_BLOCK.format(prompt=task.prompt.strip()))
    return "\n".join(parts)


def wrap_up(seconds):
    """The message pushed into a run when its deadline is close."""
    return WRAP_UP.format(seconds=int(seconds))


def _duration(seconds):
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "a minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"
