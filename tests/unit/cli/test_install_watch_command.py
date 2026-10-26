"""Tests for the `install-watch` command's wiring, with every system effect faked.

The Steam root, the library, and the user's unit folder are temporary
folders, and root detection and systemctl are replaced, so the tests change
nothing on the machine.
"""

import argparse
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from steamshare.cli.install_watch import EXIT_FAILURE, EXIT_OK, System, install_watch

PYTHON = PurePosixPath("/home/u/steamshare/.venv/bin/python")
UNIT_NAME = "steamshare-watch.service"


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.library = tmp_path / "SteamLibrary"
        (self.library / "steamapps").mkdir(parents=True)
        self.steam_root = tmp_path / "steam"
        self.steam_root.mkdir()
        self.unit_directory = tmp_path / "config" / "systemd" / "user"
        self.is_root = False
        self.enabled: list[list[str]] = []
        self.enable_error: Exception | None = None

    @property
    def flag_file(self) -> Path:
        return self.steam_root / ".cef-enable-remote-debugging"

    @property
    def unit_file(self) -> Path:
        return self.unit_directory / UNIT_NAME

    def _enable(self, unit_names: list[str]) -> None:
        if self.enable_error is not None:
            raise self.enable_error
        self.enabled.append(unit_names)

    def run(self, **overrides: object) -> int:
        values: dict[str, object] = {
            "library": PurePosixPath(self.library),
            "tool": "proton_experimental",
            "steam_root": self.steam_root,
            "dry_run": False,
        }
        values.update(overrides)
        system = System(
            is_root=self.is_root,
            unit_directory=self.unit_directory,
            python=PYTHON,
            enable_units=self._enable,
        )
        return install_watch(argparse.Namespace(**values), system)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def test_turns_on_the_debugger_writes_the_service_and_starts_it(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run()

    assert exit_code == EXIT_OK
    assert setup.flag_file.read_text() == ""
    assert (
        f"ExecStart={PYTHON} -m steamshare watch --library {setup.library} "
        f"--tool proton_experimental --steam-root {setup.steam_root}"
    ) in setup.unit_file.read_text()
    assert setup.enabled == [[UNIT_NAME]]
    output = capsys.readouterr().out
    assert "Restart Steam once" in output
    assert "journalctl --user -u steamshare-watch.service" in output


def test_a_second_run_changes_nothing_and_needs_no_restart(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.run()
    capsys.readouterr()

    setup.run()

    output = capsys.readouterr().out
    assert output.count("unchanged") == 2
    assert "Restart Steam" not in output


def test_a_changed_service_is_backed_up_before_it_is_rewritten(setup: Setup) -> None:
    setup.run()

    setup.run(tool="proton_9")

    backups = [path.name for path in setup.unit_directory.iterdir() if ".bak-" in path.name]
    assert len(backups) == 1
    assert "--tool proton_9" in setup.unit_file.read_text()


def test_a_dry_run_prints_the_plan_and_changes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run(dry_run=True)

    assert exit_code == EXIT_OK
    assert not setup.flag_file.exists()
    assert not setup.unit_directory.exists()
    assert setup.enabled == []
    output = capsys.readouterr().out
    assert f"would create: {setup.flag_file}" in output
    assert "Restart=on-failure" in output
    assert "would run: systemctl --user enable --now steamshare-watch.service" in output


def test_refuses_to_run_as_root(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    setup.is_root = True

    assert setup.run() == EXIT_FAILURE
    assert not setup.flag_file.exists()
    assert "without sudo" in capsys.readouterr().err


def test_refuses_a_folder_that_is_not_a_library(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert setup.run(library=PurePosixPath("/nonexistent/SteamLibrary")) == EXIT_FAILURE
    assert "--library" in capsys.readouterr().err


def test_refuses_a_missing_steam_root(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert setup.run(steam_root=setup.steam_root / "missing") == EXIT_FAILURE
    assert "--steam-root" in capsys.readouterr().err


def test_a_failed_enable_says_how_to_see_why(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.enable_error = subprocess.CalledProcessError(1, ["systemctl"])

    assert setup.run() == EXIT_FAILURE
    assert "systemctl --user status steamshare-watch.service" in capsys.readouterr().err
