"""Tests for the `force-proton` command's wiring.

The library, the Steam root, and the state file live in a temporary folder,
and the Steam process check and the clock are replaced, so the tests run the
whole command without touching the real Steam install.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import pytest

from steamshare.cli.force_proton import (
    EXIT_FAILURE,
    EXIT_OK,
    System,
    appid,
    force_proton,
)
from steamshare.core.vdf import parse_vdf

NOW = datetime(2026, 10, 26, 18, 30, 5)
BACKUP_NAME = "config.vdf.bak-20261026-183005"

CONFIG_WITHOUT_MAPPINGS = (
    '"InstallConfigStore"\n'
    "{\n"
    '\t"Software"\n'
    "\t{\n"
    '\t\t"Valve"\n'
    "\t\t{\n"
    '\t\t\t"Steam"\n'
    "\t\t\t{\n"
    '\t\t\t\t"AutoUpdateWindowEnabled"\t\t"0"\n'
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)


class Setup:
    """A fake library, Steam root, and state file, plus the injected effects."""

    def __init__(self, tmp_path: Path) -> None:
        self.library = tmp_path / "SteamLibrary"
        (self.library / "steamapps").mkdir(parents=True)
        self.steam_root = tmp_path / "steam"
        self.config = self.steam_root / "config" / "config.vdf"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(CONFIG_WITHOUT_MAPPINGS, encoding="utf-8")
        self.state_path = tmp_path / "xdg" / "steamshare" / "state.json"
        self.steam_running = False

    def add_app(self, appid: str, name: str, installdir: str | None = None) -> None:
        manifest = self.library / "steamapps" / f"appmanifest_{appid}.acf"
        manifest.write_text(
            '"AppState"\n{\n'
            f'\t"appid"\t\t"{appid}"\n'
            f'\t"name"\t\t"{name}"\n'
            f'\t"installdir"\t\t"{installdir or name}"\n'
            "}\n",
            encoding="utf-8",
        )

    def system(self) -> System:
        return System(
            state_path=self.state_path,
            is_steam_running=lambda steam_root: self.steam_running,
            now=lambda: NOW,
        )

    def arguments(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "library": self.library,
            "tool": "proton_experimental",
            "keep_native": [],
            "allow_proton": [],
            "steam_root": self.steam_root,
            "dry_run": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def run(self, **overrides: object) -> int:
        return force_proton(self.arguments(**overrides), self.system())

    def mappings(self) -> dict:
        config = parse_vdf(self.config.read_text(encoding="utf-8"))
        return config["InstallConfigStore"]["Software"]["Valve"]["Steam"].get(
            "CompatToolMapping", {}
        )

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def files_next_to_config(self) -> list[str]:
        return sorted(path.name for path in self.config.parent.iterdir())


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    result = Setup(tmp_path)
    result.add_app("1005870", "HYPERFIGHT")
    result.add_app("1493710", "Proton Experimental", "Proton - Experimental")
    return result


def test_maps_the_games_backs_up_the_config_and_records_the_mappings(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run()

    assert exit_code == EXIT_OK
    assert setup.mappings() == {
        "1005870": {"name": "proton_experimental", "config": "", "priority": "250"}
    }
    assert setup.files_next_to_config() == ["config.vdf", BACKUP_NAME]
    assert (setup.config.parent / BACKUP_NAME).read_text() == CONFIG_WITHOUT_MAPPINGS
    assert setup.state() == {"keep_native": [], "managed_mappings": ["1005870"]}
    output = capsys.readouterr().out
    assert "map" in output and "1005870" in output and "HYPERFIGHT" in output
    assert "skipped (tool)" in output and "Proton Experimental" in output
    assert "before starting Steam on Linux" in output


def test_a_second_run_changes_and_writes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.run()
    config_after_first_run = setup.config.read_bytes()
    state_written_at = setup.state_path.stat().st_mtime_ns

    exit_code = setup.run()

    assert exit_code == EXIT_OK
    assert setup.config.read_bytes() == config_after_first_run
    assert setup.files_next_to_config() == ["config.vdf", BACKUP_NAME]
    assert setup.state_path.stat().st_mtime_ns == state_written_at
    assert "already mapped (managed)" in capsys.readouterr().out


def test_dry_run_prints_the_plan_and_writes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run(dry_run=True, keep_native=["1005870"])

    assert exit_code == EXIT_OK
    assert setup.config.read_text() == CONFIG_WITHOUT_MAPPINGS
    assert setup.files_next_to_config() == ["config.vdf"]
    assert not setup.state_path.exists()
    output = capsys.readouterr().out
    assert "Dry run" in output
    assert "skipped (keep native)" in output


def test_dry_run_mentions_that_steam_is_running_without_failing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.steam_running = True

    exit_code = setup.run(dry_run=True)

    assert exit_code == EXIT_OK
    assert "Steam is running" in capsys.readouterr().out


def test_refuses_to_write_while_steam_is_running(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.steam_running = True

    exit_code = setup.run()

    assert exit_code == EXIT_FAILURE
    assert setup.config.read_text() == CONFIG_WITHOUT_MAPPINGS
    assert not setup.state_path.exists()
    assert "close Steam (Steam → Exit) and run again" in capsys.readouterr().err


def test_keep_native_removes_the_managed_mapping_and_warns(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.run()

    exit_code = setup.run(keep_native=["1005870"])

    assert exit_code == EXIT_OK
    assert setup.mappings() == {}
    assert setup.state() == {"keep_native": ["1005870"], "managed_mappings": []}
    output = capsys.readouterr().out
    assert "unmap (keep native)" in output
    assert "Windows can no longer run" in output
    assert "ext4 library" in output


def test_allow_proton_maps_a_kept_native_app_again_in_the_same_run(setup: Setup) -> None:
    setup.run(keep_native=["1005870"])

    exit_code = setup.run(allow_proton=["1005870"])

    assert exit_code == EXIT_OK
    assert "1005870" in setup.mappings()
    assert setup.state() == {"keep_native": [], "managed_mappings": ["1005870"]}


def test_never_touches_a_mapping_the_user_made(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.config.write_text(
        CONFIG_WITHOUT_MAPPINGS.replace(
            '\t\t\t\t"AutoUpdateWindowEnabled"',
            '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n'
            '\t\t\t\t\t"1005870"\n\t\t\t\t\t{\n'
            '\t\t\t\t\t\t"name"\t\t"proton_8"\n'
            '\t\t\t\t\t\t"config"\t\t""\n'
            '\t\t\t\t\t\t"priority"\t\t"250"\n'
            "\t\t\t\t\t}\n\t\t\t\t}\n"
            '\t\t\t\t"AutoUpdateWindowEnabled"',
        )
    )
    config_before = setup.config.read_bytes()

    exit_code = setup.run(keep_native=["1005870"])

    assert exit_code == EXIT_OK
    assert setup.config.read_bytes() == config_before
    assert setup.files_next_to_config() == ["config.vdf"]
    assert "skipped (keep native)" in capsys.readouterr().out


def test_rejects_an_app_both_kept_native_and_allowed(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run(keep_native=["400"], allow_proton=["400"])

    assert exit_code == EXIT_FAILURE
    assert "400" in capsys.readouterr().err


def test_a_missing_config_says_steam_never_ran_or_the_root_is_wrong(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.config.unlink()

    exit_code = setup.run()

    assert exit_code == EXIT_FAILURE
    error = capsys.readouterr().err
    assert "--steam-root" in error
    assert "Start Steam once" in error


def test_a_missing_library_says_to_mount_it(
    setup: Setup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = setup.run(library=tmp_path / "not-mounted")

    assert exit_code == EXIT_FAILURE
    assert "Mount the NTFS partition" in capsys.readouterr().err


def test_a_broken_state_file_says_how_to_recover(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.state_path.parent.mkdir(parents=True)
    setup.state_path.write_text("not json")

    exit_code = setup.run()

    assert exit_code == EXIT_FAILURE
    assert str(setup.state_path) in capsys.readouterr().err


@pytest.mark.parametrize("value", ["1005870", "400"])
def test_appid_accepts_numbers(value: str) -> None:
    assert appid(value) == value


@pytest.mark.parametrize("value", ["", "HYPERFIGHT", "-1", "12a"])
def test_appid_rejects_anything_else(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        appid(value)
