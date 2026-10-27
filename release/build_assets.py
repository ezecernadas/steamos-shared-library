"""Build the files each GitHub release attaches, for the one-line installs.

    python3 release/build_assets.py [--tag vX.Y.Z] [--output dist]

Users install with one command per system, served from the release of a
fixed version:

- Windows: `irm <release>/setup.ps1 | iex`. Invoke-Expression runs one
  string, so there is no SteamShare.psm1 next to the script to import. The
  built setup.ps1 is windows/setup.ps1 with the module embedded in place of
  its Import-Module line, as a dynamic module (`New-Module`). Everything else
  stays identical; the repository keeps the two files apart, as the Pester
  tests load them.
- Linux: `curl -fsSL <release>/install.sh | sudo bash`. The built install.sh
  is release/install.sh with the version stamped in, so it downloads the
  source archive of its own release.

With `--tag`, as the release workflow runs it, the tag must name the version
in pyproject.toml, and the CHANGELOG section of that version is written as
the release notes. Development tooling only: nothing here runs on a user's
machine (CONTRIBUTING.md, rule 1).
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]

SETUP_SCRIPT = Path("windows") / "setup.ps1"
SETUP_MODULE = Path("windows") / "SteamShare.psm1"
INSTALLER_TEMPLATE = Path("release") / "install.sh"
PYPROJECT = Path("pyproject.toml")
CHANGELOG = Path("CHANGELOG.md")

SETUP_ASSET = "setup.ps1"
INSTALLER_ASSET = "install.sh"
# Passed to `gh release create --notes-file`; not attached to the release.
RELEASE_NOTES = "release-notes.md"

# The line of windows/setup.ps1 the embedded module replaces.
MODULE_IMPORT_LINE = "Import-Module (Join-Path $PSScriptRoot 'SteamShare.psm1') -Force"
# What release/install.sh holds instead of a version until it is built.
VERSION_PLACEHOLDER = "@STEAMSHARE_VERSION@"

# Release tags are "v" and a semantic version, such as v0.2.0.
TAG_PATTERN = re.compile(r"v(\d+\.\d+\.\d+)")

SINGLE_FILE_HEADER = """\
# steamshare Windows setup, as one file for `irm <url> | iex`.
# Built from windows/setup.ps1 and windows/SteamShare.psm1 by
# release/build_assets.py; change those, not this file.
#
# Invoke-Expression runs a string in the caller's scope, so the whole script
# runs inside a script block: its Set-StrictMode, preferences and variables
# end with it instead of staying in the user's session. @args passes the
# parameters on when this file is run directly, with -File.
"""


class BuildError(Exception):
    """A build that cannot go on, with a message that says what to fix."""


def project_version(pyproject_text: str) -> str:
    return tomllib.loads(pyproject_text)["project"]["version"]


def version_from_tag(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise BuildError(f"The tag {tag!r} is not a release tag: release tags are vX.Y.Z.")
    return match.group(1)


def build_single_file_setup(setup_script: str, module: str) -> str:
    """Return setup.ps1 with the module embedded, runnable through `irm | iex`."""
    _require_ascii(setup_script, SETUP_SCRIPT)
    _require_ascii(module, SETUP_MODULE)
    count = setup_script.count(MODULE_IMPORT_LINE)
    if count == 0:
        raise BuildError(f"{SETUP_SCRIPT} no longer has the line `{MODULE_IMPORT_LINE}`.")
    if count > 1:
        raise BuildError(f"{SETUP_SCRIPT} must have `{MODULE_IMPORT_LINE}` once, not {count}.")
    embedded_module = (
        "# The module, embedded: there is no SteamShare.psm1 to import under iex.\n"
        "New-Module -Name SteamShare -ScriptBlock {\n"
        f"{_with_final_newline(module)}"
        "} | Import-Module -Force"
    )
    script = setup_script.replace(MODULE_IMPORT_LINE, embedded_module)
    return f"{SINGLE_FILE_HEADER}\n& {{\n{_with_final_newline(script)}}} @args\n"


def stamp_installer(template: str, version: str) -> str:
    """Return install.sh with its release version in place of the placeholder."""
    if VERSION_PLACEHOLDER not in template:
        raise BuildError(
            f"{INSTALLER_TEMPLATE} no longer has the {VERSION_PLACEHOLDER} placeholder."
        )
    return template.replace(VERSION_PLACEHOLDER, version)


def changelog_section(changelog: str, version: str) -> str:
    """Return the body of the version's CHANGELOG section, without its heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\].*$", re.MULTILINE)
    found = heading.search(changelog)
    if found is None:
        raise BuildError(
            f"CHANGELOG.md has no [{version}] section. Move the [Unreleased] entries under "
            f"a `## [{version}] - <date>` heading, then tag again."
        )
    # It ends at the next section or at the link references at the bottom.
    end = re.compile(r"^(## |\[[^\]]+\]: )", re.MULTILINE).search(changelog, found.end())
    body = changelog[found.end() : None if end is None else end.start()]
    return body.strip()


def build_release_assets(repository: Path, output: Path, *, tag: str | None) -> None:
    """Write setup.ps1 and install.sh to `output`, and with a tag, the release notes."""
    version = project_version(_read(repository / PYPROJECT))
    if tag is not None and version_from_tag(tag) != version:
        raise BuildError(
            f"The tag {tag} does not match the version {version} in pyproject.toml. "
            "Bump the version there (and the README install links), or tag "
            f"v{version} instead."
        )
    setup = build_single_file_setup(
        _read(repository / SETUP_SCRIPT), _read(repository / SETUP_MODULE)
    )
    template_path = repository / INSTALLER_TEMPLATE
    installer = stamp_installer(_read(template_path), version)
    notes = None
    if tag is not None:
        notes = changelog_section(_read(repository / CHANGELOG), version)

    output.mkdir(parents=True, exist_ok=True)
    _write(output / SETUP_ASSET, setup)
    installer_path = output / INSTALLER_ASSET
    _write(installer_path, installer)
    # Executable like the template, for users who download it and run it.
    installer_path.chmod(template_path.stat().st_mode)
    if notes is not None:
        _write(output / RELEASE_NOTES, f"{notes}\n")


def _read(path: Path) -> str:
    # Explicit, since Windows would otherwise read with its ANSI code page;
    # CI builds setup.ps1 there too.
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    # Always LF, even on Windows: bash fails on a CRLF script.
    path.write_text(text, encoding="utf-8", newline="\n")


def _require_ascii(text: str, source: Path) -> None:
    # Windows PowerShell 5.1 reads UTF-8 without a BOM as the ANSI code page
    # (CONTRIBUTING.md, "PowerShell").
    if not text.isascii():
        raise BuildError(f"{source} must be ASCII, for Windows PowerShell 5.1.")


def _with_final_newline(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the files a steamshare release attaches.")
    parser.add_argument(
        "--tag", help="the release tag, vX.Y.Z; must match pyproject.toml, adds the notes"
    )
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY / "dist", help="where to write (default: dist)"
    )
    arguments = parser.parse_args(argv)
    try:
        build_release_assets(REPOSITORY, arguments.output, tag=arguments.tag)
    except BuildError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Built {SETUP_ASSET} and {INSTALLER_ASSET} in {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
