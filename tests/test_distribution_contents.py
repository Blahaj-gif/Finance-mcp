"""
What may and may not leave this machine inside a distribution.

Four published versions carried the author's real Webull account id, a
portfolio's positions and value day by day, and a live order with the broker's
own order id. Nothing in the repository showed it: those files are gitignored,
and setuptools globs the filesystem rather than the git index. The build ran
from a working tree where the tool had been used.

It was a correctness bug as well as a privacy one -- a fresh install started
with somebody else's drafts already sitting in the approval queue.

These tests read the packaging configuration rather than a built artifact, so
they run offline and fail before a build rather than after a publish. The CI
packaging job checks the built wheel itself.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Files the dashboard writes beside its own code. Every one of these is
#: personal: what you hold, what you drafted, what you were alerted about.
RUNTIME_STATE = (
    "order_drafts.json", "portfolio_history.json", "live_consent.json",
    "iv_history.json", "alerts.json", "trading_journal.json",
)


def _pyproject():
    return open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()


def test_setuptools_does_not_sweep_files_it_finds_beside_the_code():
    """
    `include-package-data` defaults to **true** for a pyproject config. That is
    the setting that shipped the leak, and removing the package-data glob alone
    did not stop it -- a stale egg-info SOURCES.txt remembered the files from an
    earlier build.
    """
    text = _pyproject()
    match = re.search(r"^include-package-data\s*=\s*(\w+)", text, re.M)
    assert match, ("include-package-data must be set explicitly; its default "
                   "is true and that default published private data")
    assert match.group(1).lower() == "false", match.group(1)


def test_no_glob_ships_a_directory_the_program_writes_to():
    """
    A glob over `dashboard/` cannot be safe: the dashboard writes its state
    there. If an asset genuinely needs shipping, name the file.
    """
    text = _pyproject()
    section = re.search(r"\[tool\.setuptools\.package-data\](.*?)(?=\n\[|\Z)",
                        text, re.S)
    if not section:
        return                      # no package data at all is the safe case
    for line in section.group(1).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        assert "*" not in line.split("=", 1)[1], (
            f"a wildcard in package-data over a written-to directory: {line}")


@pytest.mark.parametrize("name", RUNTIME_STATE)
def test_runtime_state_is_never_tracked_by_git(name):
    """
    The second line of defence. These are gitignored, which is why the leak was
    invisible in review -- but if one were ever committed it would ship no
    matter how the packaging is configured.
    """
    import subprocess
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", f"dashboard/{name}"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0, (
        f"dashboard/{name} is tracked by git and will ship in every "
        "distribution; it holds account state")


@pytest.mark.parametrize("name", RUNTIME_STATE)
def test_the_gitignore_still_covers_the_dashboard_state(name):
    """
    Asked of git rather than by grepping .gitignore for a pattern. The file
    listed these one by one and missed iv_history.json -- untracked, but not
    ignored, and so one `git add -A` from being committed.
    """
    import subprocess
    result = subprocess.run(["git", "check-ignore", "-q", f"dashboard/{name}"],
                            cwd=ROOT)
    assert result.returncode == 0, f"dashboard/{name} is not gitignored"
