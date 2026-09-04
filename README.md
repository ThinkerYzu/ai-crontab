# ai-crontab

Run AI agent tasks on a schedule, unattended, and let them reach you through an
inbox you read when you have time.

Each entry is a prompt an agent carries out, one-time or recurring, with nobody
watching. System cron fires `ai-crontab tick`; the tick launches whatever became
due since the last one and exits, so there is no resident process to survive a
sleeping laptop. An agent that finds something worth saying files a notification;
a run that turns up nothing files nothing.

## Status

Skeleton only. No implementation yet.

## Documentation

The design documents live in the workspace, in their own repository:

    ~/progm/claudebugzilla/proj_docs/ai-crontab/

- `SPEC.md` - what this is and why, with the requirements and success criteria
- `DESIGN.md` - architecture, the run lifecycle, the tick algorithm, data model
- `IMPLEMENTATION-GUIDE.md` - dependencies, file structure, build phases
- `HANDOFF.md` - current status and what to do next

## Development

    python3 -m venv .venv && . .venv/bin/activate
    pip install -e ".[dev]"
    pytest -q
