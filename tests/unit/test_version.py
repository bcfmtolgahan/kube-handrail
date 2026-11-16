"""The release version has one source (handrail_common.__version__), which the User-Agent
uses; pyproject.toml and the newest release in CHANGELOG.md must agree with it."""

import re
import tomllib
from pathlib import Path

from handrail_common import __version__

ROOT = Path(__file__).parents[2]
RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pyproject_has_the_same_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__


def test_newest_changelog_release_is_the_version():
    releases = RELEASE.findall((ROOT / "CHANGELOG.md").read_text())
    # Nothing released yet: the version stays at its initial value until the first release.
    assert releases[0] == __version__ if releases else __version__ == "0.0.0"
