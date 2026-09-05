#!/usr/bin/env python3
"""Check that a release is consistent, and print the notes for it.

The release workflow runs this before it builds anything, so running it here
first turns a bad release into a commit rather than a tag that has to be
deleted from GitHub.

    scripts/release-check.py            # what a release from this tree would say
    scripts/release-check.py v0.1.0     # and that this tag is the right name for it
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "ai_crontab" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def package_version(init=None):
    """The version ai-crontab reports.

    Read out of __init__.py rather than imported, so this works in a checkout
    nobody has installed. pyproject.toml takes the same string through
    setuptools' dynamic version, so there is nothing else to agree with.
    """
    init = init or INIT
    for node in ast.parse(init.read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__version__" in names:
            return node.value.value
    fail(f"{init} declares no __version__")


def changelog_notes(version, changelog=None):
    """The body of one version's CHANGELOG section, heading and blank lines trimmed.

    A version with no section is an error rather than an empty string: the
    notes are what the release page shows, and the failure this catches is
    bumping the version and forgetting to write down what changed.

    A heading matches on its version alone, so the date beside it is free to be
    whatever the day was.
    """
    changelog = changelog or CHANGELOG
    body = []
    inside = False
    for line in changelog.read_text().splitlines():
        if line.startswith("## "):
            if inside:
                break
            heading = line[3:].split()
            inside = bool(heading) and heading[0] == version
            continue
        if inside:
            body.append(line)
    if not inside:
        fail(f"{changelog} has no section for {version}")
    notes = "\n".join(body).strip()
    if not notes:
        fail(f"{changelog}'s section for {version} is empty")
    return notes


def fail(message):
    """Stop with a message on stderr and a non-zero status, for the workflow to catch."""
    sys.exit(f"release-check: {message}")


def main(argv):
    """Print this tree's release notes, or exit non-zero saying what is wrong.

    Given a tag (`v0.1.0`, or `0.1.0`) it also requires the tag to name the
    version the package reports, which is what stops a release page from
    disagreeing with the wheel attached to it.
    """
    version = package_version()
    if argv:
        tag = argv[0].removeprefix("v")
        if tag != version:
            fail(f"tag {argv[0]} does not match version {version}")
    print(changelog_notes(version))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
