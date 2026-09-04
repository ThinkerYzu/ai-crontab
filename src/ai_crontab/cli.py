"""Command line entry point for ai-crontab."""

import sys

DOCS = "~/progm/claudebugzilla/proj_docs/ai-crontab/"


def main(argv=None):
    """Report that nothing is built yet and say where the plan lives.

    Stands in for the real command parser so that the `ai-crontab` script
    declared in pyproject.toml resolves. Returns the process exit status, which
    is 1 for every invocation: no subcommand can do what it claims yet, and a
    cron line installed against this build must fail rather than look like it
    ran.
    """
    print("ai-crontab is not implemented yet: this is the package skeleton.")
    print(f"The spec, design and build plan are in {DOCS}")
    print("Start with HANDOFF.md for the current status and next actions.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
