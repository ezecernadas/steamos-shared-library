"""Tests for release/build_assets.py, which builds the files a release attaches.

The pure functions get strings; `build_release_assets` reads the real
repository files and writes to a temporary folder, which is fast and needs
nothing else, so it runs with the unit tests.
"""

import subprocess
from pathlib import Path

import pytest
from build_assets import (
    MODULE_IMPORT_LINE,
    VERSION_PLACEHOLDER,
    BuildError,
    build_release_assets,
    build_single_file_setup,
    changelog_section,
    main,
    project_version,
    stamp_installer,
    version_from_tag,
)

REPOSITORY = Path(__file__).resolve().parents[3]

SETUP_SCRIPT = f"""<#
.SYNOPSIS
Help.
#>
[CmdletBinding(SupportsShouldProcess)]
param([string] $Library = '')

Set-StrictMode -Version Latest

{MODULE_IMPORT_LINE}

Invoke-SteamShareSetup
"""

MODULE = """Set-StrictMode -Version Latest

function Invoke-SteamShareSetup { 'ran' }
"""

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something new.

## [0.2.0] - 2026-11-01

### Added

- One-line installs.

## [0.1.0] - 2026-10-26

- First.

[Unreleased]: https://github.com/ezecernadas/steamshare/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ezecernadas/steamshare/releases/tag/v0.2.0
"""


# --- Versions ------------------------------------------------------------------


def test_the_project_version_comes_from_pyproject() -> None:
    assert project_version('[project]\nname = "steamshare"\nversion = "0.2.0"\n') == "0.2.0"


def test_a_tag_gives_its_version() -> None:
    assert version_from_tag("v0.2.0") == "0.2.0"


@pytest.mark.parametrize("tag", ["0.2.0", "release-0.2.0", "v", "v0.2"])
def test_a_tag_that_is_not_v_and_a_version_is_refused(tag: str) -> None:
    with pytest.raises(BuildError, match="vX.Y.Z"):
        version_from_tag(tag)


# --- setup.ps1 -----------------------------------------------------------------


def test_the_single_file_setup_embeds_the_module_in_place_of_its_import() -> None:
    built = build_single_file_setup(SETUP_SCRIPT, MODULE)

    assert MODULE_IMPORT_LINE not in built
    assert "New-Module -Name SteamShare -ScriptBlock {\n" + MODULE in built
    assert "} | Import-Module -Force\n" in built


def test_the_single_file_setup_runs_the_whole_script_in_its_own_scope() -> None:
    # Invoke-Expression runs a string in the caller's scope, so without the
    # script block Set-StrictMode and the variables would stay in the user's
    # session. @args keeps parameters working when the file is run directly.
    built = build_single_file_setup(SETUP_SCRIPT, MODULE)

    body = built[built.index("& {\n") :]
    assert body.endswith("\n} @args\n")
    assert "[CmdletBinding(SupportsShouldProcess)]" in body
    assert body.index("& {\n") < body.index("<#\n.SYNOPSIS")


def test_the_single_file_setup_keeps_everything_else_identical() -> None:
    built = build_single_file_setup(SETUP_SCRIPT, MODULE)

    before, after = SETUP_SCRIPT.split(MODULE_IMPORT_LINE)
    assert before in built
    assert after in built


def test_a_setup_script_without_the_import_line_is_refused() -> None:
    with pytest.raises(BuildError, match="Import-Module"):
        build_single_file_setup("Invoke-SteamShareSetup\n", MODULE)


def test_a_setup_script_with_the_import_line_twice_is_refused() -> None:
    with pytest.raises(BuildError, match="once"):
        build_single_file_setup(SETUP_SCRIPT + MODULE_IMPORT_LINE + "\n", MODULE)


def test_non_ascii_text_is_refused() -> None:
    # Windows PowerShell 5.1 reads a file without a BOM as the ANSI code page.
    with pytest.raises(BuildError, match="ASCII"):
        build_single_file_setup(SETUP_SCRIPT, MODULE + "# café\n")


# --- install.sh ----------------------------------------------------------------


def test_the_installer_gets_the_version_in_place_of_the_placeholder() -> None:
    template = f'version="{VERSION_PLACEHOLDER}"\n'

    assert stamp_installer(template, "0.2.0") == 'version="0.2.0"\n'


def test_an_installer_without_the_placeholder_is_refused() -> None:
    with pytest.raises(BuildError, match=VERSION_PLACEHOLDER):
        stamp_installer('version="0.2.0"\n', "0.2.0")


def test_the_repository_installer_has_the_placeholder_once() -> None:
    template = (REPOSITORY / "release" / "install.sh").read_text()

    # Once in the assignment; the guard compares against it by pattern.
    assert template.count(VERSION_PLACEHOLDER) == 1


# --- Release notes -------------------------------------------------------------


def test_the_release_notes_are_the_changelog_section_of_the_version() -> None:
    assert changelog_section(CHANGELOG, "0.2.0") == "### Added\n\n- One-line installs."


def test_the_last_section_stops_before_the_link_references() -> None:
    assert changelog_section(CHANGELOG, "0.1.0") == "- First."


def test_a_version_missing_from_the_changelog_is_refused() -> None:
    with pytest.raises(BuildError, match=r"CHANGELOG.md has no \[0.3.0\] section"):
        changelog_section(CHANGELOG, "0.3.0")


# --- The whole build, on the real files ----------------------------------------


def test_the_build_writes_both_assets(tmp_path: Path) -> None:
    version = project_version((REPOSITORY / "pyproject.toml").read_text())

    build_release_assets(REPOSITORY, tmp_path, tag=None)

    setup = (tmp_path / "setup.ps1").read_text()
    installer = (tmp_path / "install.sh").read_text()
    assert "function Invoke-SteamShareSetup" in setup
    assert VERSION_PLACEHOLDER not in installer
    assert f'"{version}"' in installer
    assert not (tmp_path / "release-notes.md").exists()


def test_the_built_installer_is_valid_bash(tmp_path: Path) -> None:
    build_release_assets(REPOSITORY, tmp_path, tag=None)

    result = subprocess.run(
        ["bash", "-n", str(tmp_path / "install.sh")], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_the_built_installer_keeps_the_template_mode(tmp_path: Path) -> None:
    build_release_assets(REPOSITORY, tmp_path, tag=None)

    assert (tmp_path / "install.sh").stat().st_mode & 0o111


def test_a_tag_that_does_not_match_pyproject_fails_the_build(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="pyproject.toml"):
        build_release_assets(REPOSITORY, tmp_path, tag="v99.0.0")

    assert not (tmp_path / "setup.ps1").exists()


def test_main_reports_a_failure_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--tag", "v99.0.0", "--output", str(tmp_path)])

    assert code == 1
    error = capsys.readouterr().err
    assert error.startswith("Error: ")
    assert "Traceback" not in error


def test_a_tag_build_also_writes_the_release_notes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    for name in ("windows/setup.ps1", "windows/SteamShare.psm1", "release/install.sh"):
        (repository / name).parent.mkdir(parents=True, exist_ok=True)
        (repository / name).write_text((REPOSITORY / name).read_text())
    (repository / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    (repository / "CHANGELOG.md").write_text(CHANGELOG)
    output = tmp_path / "dist"

    build_release_assets(repository, output, tag="v0.2.0")

    assert (output / "release-notes.md").read_text() == "### Added\n\n- One-line installs.\n"
    assert '"0.2.0"' in (output / "install.sh").read_text()
