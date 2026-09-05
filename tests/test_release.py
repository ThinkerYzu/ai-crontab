"""The release check: the version, its notes, and the tag that names them."""

import importlib.metadata
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "release-check.py"


@pytest.fixture
def rc():
    """The release-check script, imported from its path.

    It lives in scripts/ rather than the package, because it has to run in a
    checkout nobody has installed, which leaves loading it by hand as the only
    way to test it.
    """
    spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def changelog(tmp_path):
    """A changelog with two versions in it, and a heading that carries a date."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "# Changelog\n\n"
        "## Unreleased\n\nNothing yet.\n\n"
        "## 0.2.0 - 2026-10-01\n\nThe second one.\n\n"
        "## 0.1.0 - 2026-09-04\n\nThe first one.\n"
    )
    return path


def test_version_comes_from_the_package_source(rc, tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text('"""Doc."""\n\n__version__ = "1.2.3"\n')
    assert rc.package_version(init) == "1.2.3"


def test_a_source_with_no_version_is_an_error(rc, tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text('"""Doc."""\n')
    with pytest.raises(SystemExit):
        rc.package_version(init)


def test_notes_stop_at_the_next_version(rc, changelog):
    assert rc.changelog_notes("0.2.0", changelog) == "The second one."


def test_the_last_section_runs_to_the_end(rc, changelog):
    assert rc.changelog_notes("0.1.0", changelog) == "The first one."


def test_a_version_with_no_section_is_an_error(rc, changelog):
    with pytest.raises(SystemExit):
        rc.changelog_notes("0.3.0", changelog)


def test_an_empty_section_is_an_error(rc, tmp_path):
    """An empty section would otherwise become an empty release page."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## 0.1.0\n\n\n## 0.0.9\n\nOld.\n")
    with pytest.raises(SystemExit):
        rc.changelog_notes("0.1.0", path)


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1.0"])
def test_a_tag_naming_the_version_passes(rc, monkeypatch, capsys, tmp_path, changelog, tag):
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "0.1.0"\n')
    monkeypatch.setattr(rc, "INIT", init)
    monkeypatch.setattr(rc, "CHANGELOG", changelog)
    assert rc.main([tag]) == 0
    assert capsys.readouterr().out.strip() == "The first one."


def test_a_tag_naming_another_version_is_an_error(rc, monkeypatch, tmp_path, changelog):
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "0.1.0"\n')
    monkeypatch.setattr(rc, "INIT", init)
    monkeypatch.setattr(rc, "CHANGELOG", changelog)
    with pytest.raises(SystemExit):
        rc.main(["v0.2.0"])


def test_this_checkout_could_be_released(rc):
    """The invariant CI enforces: whatever version is set has notes to ship."""
    assert rc.main([]) == 0


def test_the_packaging_metadata_agrees_with_the_package(rc):
    """pyproject.toml takes its version from __init__.py, and this is the wire.

    Breaking the dynamic version leaves a wheel whose metadata says 0.0.0 while
    `ai-crontab --version` says something else, which nothing else here notices.
    """
    assert importlib.metadata.version("ai-crontab") == rc.package_version()
