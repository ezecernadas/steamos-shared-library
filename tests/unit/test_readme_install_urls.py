"""The README's one-line install commands name the version in pyproject.toml.

They are pinned to a release tag, because GitHub's /releases/latest/ skips
pre-releases and every release so far is one. A version bump that forgets
the README fails here instead of sending users to the previous release.
See "Releasing" in CONTRIBUTING.md.
"""

import re
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]

RELEASE_ASSET_URL = re.compile(
    r"https://github\.com/ezecernadas/steamshare/releases/download/(?P<tag>[^/\s]+)/"
    r"(?P<asset>[^\s)`]+)"
)


def test_the_readme_install_commands_name_the_project_version() -> None:
    version = tomllib.loads((REPOSITORY / "pyproject.toml").read_text())["project"]["version"]
    readme = (REPOSITORY / "README.md").read_text()

    urls = list(RELEASE_ASSET_URL.finditer(readme))

    assert {url["asset"] for url in urls} >= {"setup.ps1", "install.sh"}
    assert {url["tag"] for url in urls} == {f"v{version}"}
