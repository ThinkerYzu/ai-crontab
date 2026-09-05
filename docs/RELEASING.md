# Releasing

A release is a pushed tag. `git push origin v0.2.0` is the whole action: the
`release` workflow checks that the tag names the right version, runs the tests,
builds the sdist and the wheel, installs that wheel into an empty virtualenv to
prove it works, and creates the GitHub release with the CHANGELOG notes and
both files attached.

## Where the version lives

One string, in `src/ai_crontab/__init__.py`:

```python
__version__ = "0.1.0"
```

`pyproject.toml` declares `dynamic = ["version"]` and reads that attribute, so
the packaging metadata, `ai-crontab --version` and the tag all come from the
same place. A test in `tests/test_release.py` checks the first two still agree,
because a broken dynamic version produces a wheel that says `0.0.0` in its
metadata and something else on the command line.

## Making a release

1. **Move the notes.** In `CHANGELOG.md`, rename `## Unreleased` to the version
   and the date, and put a fresh empty `## Unreleased` above it:

   ```markdown
   ## Unreleased

   Nothing yet.

   ## 0.2.0 - 2026-10-01
   ```

2. **Bump `__version__`** in `src/ai_crontab/__init__.py` to match.

3. **Rehearse it.** This prints exactly what the release page will say, or
   exits with the reason it cannot:

   ```bash
   python scripts/release-check.py v0.2.0
   ```

4. **Commit and push**, and wait for the `tests` workflow to come back green.
   A tag on a red commit builds nothing, but it still has to be deleted.

5. **Tag and push the tag:**

   ```bash
   git tag -a v0.2.0 -m "ai-crontab 0.2.0"
   git push origin v0.2.0
   ```

6. **Watch it land:**

   ```bash
   gh run watch
   gh release view v0.2.0
   ```

## When it goes wrong

The workflow checks the tag before it builds or publishes anything, so the
usual failure costs a deleted tag and nothing else.

| Symptom | What happened | Fix |
|---------|---------------|-----|
| `tag v0.2.0 does not match version 0.1.0` | Tagged without bumping `__version__` | `git tag -d v0.2.0 && git push --delete origin v0.2.0`, bump, retag |
| `CHANGELOG.md has no section for 0.2.0` | Bumped the version, did not write the notes | Same: delete the tag, add the section, retag |
| Tests fail at the tag but passed on `master` | Something outside the repo moved, usually a dependency release | Fix on `master` first, then retag |
| The release exists but is wrong | Already published | `gh release delete v0.2.0`, delete the tag, fix, retag |

A tag is cheap to delete while nobody has pulled it. Once a release page has
been up long enough for someone to download the wheel, release `0.2.1` instead
of rewriting `0.2.0`.

## Prereleases

Any tag with something after the third number, such as `v0.2.0rc1`, is
published as a prerelease and does not become the release people are shown as
Latest. Everything else about the flow is identical.

## PyPI

Nothing is published to PyPI, and `pip install ai-crontab` installs something
else or nothing. Installing means cloning the repository or downloading the
wheel from a release page.

Publishing would be one more job in `release.yml` using PyPI's trusted
publishing, which needs the project registered on PyPI and a `pypi`
environment configured on this repository first. It is worth doing only if the
name is wanted, since the tool is useless without a `claude` binary and a
crontab line, neither of which pip can install.
