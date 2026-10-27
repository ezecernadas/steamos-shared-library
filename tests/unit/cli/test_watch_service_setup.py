"""Tests for the watch part of `install`, with every system effect faked.

The user's home, Steam root and unit folder are temporary folders, and chown
and systemctl are recorded instead of run, so the tests change nothing on the
machine. They carry over what the removed `install-watch` command was tested
for, now done as root on the desktop user's behalf.
"""

import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.invoking_user import InvokingUser
from steamshare.adapters.launcher_location import LAUNCHER_PATH
from steamshare.cli.install_choices import InstallError
from steamshare.cli.run_report import UNICODE_SYMBOLS, ClosingNotes, RunReport
from steamshare.cli.watch_service_setup import (
    WatchServicePlan,
    WatchServiceSkipped,
    apply_watch_service,
    plan_watch_service,
    preview_watch_service,
    watch_summary_field,
)

UNIT_NAME = "steamshare-watch.service"
LIBRARY = PurePosixPath("/mnt/steamlib/SteamLibrary")
USER_SYSTEMCTL = ["systemctl", "--user", "--machine=player@"]


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.steam_root = self.home / ".steam" / "steam"
        self.steam_root.mkdir(parents=True)
        self.launcher = tmp_path / "repository" / "steamshare"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/usr/bin/env bash\n")
        self.user = InvokingUser(
            user_id=1000, group_id=1000, name="player", home=PurePosixPath(self.home)
        )
        self.commands: list[list[str]] = []
        self.owned: list[Path] = []
        self.command_error: Exception | None = None

    @property
    def flag_file(self) -> Path:
        return self.steam_root / ".cef-enable-remote-debugging"

    @property
    def unit_file(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / UNIT_NAME

    def plan(
        self, *, skip: bool = False, tool: str = "proton_experimental"
    ) -> WatchServicePlan | WatchServiceSkipped:
        return plan_watch_service(
            skip=skip,
            user=self.user,
            library=LIBRARY,
            tool=tool,
            steam_root=None,
            launcher=self.launcher,
        )

    def report(self, *, dry_run: bool) -> RunReport:
        return RunReport(symbols=UNICODE_SYMBOLS, home=self.user.home, dry_run=dry_run)

    def apply(self, *, tool: str = "proton_experimental") -> ClosingNotes:
        return apply_watch_service(
            self.plan(tool=tool),
            change_owner=self._change_owner,
            run_command=self._run,
            report=self.report(dry_run=False),
            now=datetime(2026, 10, 26, 18, 31, 31),
        )

    def preview(self, *, show_units: bool = False) -> ClosingNotes:
        return preview_watch_service(self.plan(), self.report(dry_run=True), show_units=show_units)

    def _change_owner(self, path: Path, user_id: int, group_id: int) -> None:
        assert (user_id, group_id) == (1000, 1000)
        self.owned.append(Path(path))

    def _run(self, command: Sequence[str]) -> None:
        if self.command_error is not None:
            raise self.command_error
        self.commands.append(list(command))


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


# --- Planning ----------------------------------------------------------------


def test_the_plan_finds_steam_and_the_unit_folder_in_the_users_home(setup: Setup) -> None:
    plan = setup.plan()

    assert isinstance(plan, WatchServicePlan)
    assert plan.steam_root == setup.steam_root
    assert plan.unit_directory == setup.unit_file.parent
    assert (
        f"ExecStart={setup.launcher} watch --library {LIBRARY} "
        f"--tool proton_experimental --steam-root {setup.steam_root}"
    ) in plan.unit.content


def test_a_given_steam_root_wins_over_the_default(setup: Setup, tmp_path: Path) -> None:
    flatpak_root = tmp_path / "flatpak-steam"
    flatpak_root.mkdir()

    plan = plan_watch_service(
        skip=False,
        user=setup.user,
        library=LIBRARY,
        tool="proton_experimental",
        steam_root=flatpak_root,
        launcher=setup.launcher,
    )

    assert isinstance(plan, WatchServicePlan)
    assert plan.steam_root == flatpak_root


def test_no_watch_skips_the_watch_part(setup: Setup) -> None:
    plan = setup.plan(skip=True)

    assert isinstance(plan, WatchServiceSkipped)
    assert "--no-watch" in plan.reason


def test_without_a_steam_root_the_watch_part_is_skipped_with_the_next_step(setup: Setup) -> None:
    setup.steam_root.rmdir()

    plan = setup.plan()

    assert isinstance(plan, WatchServiceSkipped)
    assert str(setup.steam_root) in plan.reason
    assert plan.next_step is not None
    assert f"sudo {LAUNCHER_PATH} install" in plan.next_step


def test_a_missing_launcher_is_an_error_that_names_the_way_out(setup: Setup) -> None:
    setup.launcher.unlink()

    with pytest.raises(InstallError, match="--no-watch"):
        setup.plan()


# --- Summary and dry run -----------------------------------------------------


def test_the_summary_names_the_user_the_service_runs_for(setup: Setup) -> None:
    assert watch_summary_field(setup.plan()) == ("Watch service", "for user player")


def test_the_summary_says_when_the_watch_part_is_skipped(setup: Setup) -> None:
    assert watch_summary_field(setup.plan(skip=True)) == (
        "Watch service",
        "skipped (--no-watch was given)",
    )


def test_a_dry_run_prints_the_plan_and_changes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.preview()

    assert not setup.flag_file.exists()
    assert not setup.unit_file.parent.exists()
    output = capsys.readouterr().out
    assert "Restart=on-failure" not in output
    section = output[output.index("Watch service  (~/.config/systemd/user)") :].splitlines()
    assert section == [
        "Watch service  (~/.config/systemd/user)",
        "  · would create  Steam debugger flag (~/.steam/steam/.cef-enable-remote-debugging)",
        "  · would create  steamshare-watch.service",
        "  · would run     systemctl --user --machine=player@ daemon-reload",
        "  · would run     systemctl --user --machine=player@ enable --now "
        "steamshare-watch.service",
        "",
    ]


def test_a_dry_run_over_an_older_service_plans_a_restart(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.unit_file.parent.mkdir(parents=True)
    setup.unit_file.write_text("ExecStart=/old/.venv/bin/python -m steamshare watch\n")

    notes = setup.preview()

    output = capsys.readouterr().out
    assert "  · would update  steamshare-watch.service" in output
    assert "would run     systemctl --user --machine=player@ restart steamshare-watch.service" in (
        output
    )
    assert [outcome.path for outcome in notes.unit_outcomes] == [setup.unit_file]


def test_a_dry_run_leaves_the_same_next_steps_as_a_real_run(setup: Setup) -> None:
    planned = setup.preview()

    done = setup.apply()

    assert planned.next_steps == done.next_steps
    assert planned.tips == done.tips


def test_a_dry_run_prints_the_unit_file_only_when_asked(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.preview(show_units=True)

    output = capsys.readouterr().out
    assert output.startswith("--- ~/.config/systemd/user/steamshare-watch.service\n")
    assert "Restart=on-failure" in output


# --- Applying ----------------------------------------------------------------


def test_turns_on_the_debugger_writes_the_service_and_starts_it_for_the_user(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = setup.apply()

    assert setup.flag_file.read_text() == ""
    assert "--library /mnt/steamlib/SteamLibrary" in setup.unit_file.read_text()
    assert setup.flag_file in setup.owned
    assert setup.unit_file in setup.owned
    assert setup.commands == [
        [*USER_SYSTEMCTL, "daemon-reload"],
        [*USER_SYSTEMCTL, "enable", "--now", UNIT_NAME],
    ]
    assert capsys.readouterr().out.splitlines() == [
        "Watch service  (~/.config/systemd/user)",
        "  ✓ created    Steam debugger flag (~/.steam/steam/.cef-enable-remote-debugging)",
        "  ✓ created    steamshare-watch.service",
        "  ✓ running    for player",
        "",
    ]
    assert notes.next_steps[0].startswith("Restart Steam once (Steam → Exit")
    assert "Make default" in notes.next_steps[1]
    assert notes.tips == (("Follow watch", "journalctl --user -u steamshare-watch.service -f"),)


def test_a_second_run_changes_nothing_restarts_nothing_and_needs_no_steam_restart(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.apply()
    setup.commands.clear()
    capsys.readouterr()

    notes = setup.apply()

    assert capsys.readouterr().out.count("unchanged") == 2
    assert not any("Restart Steam" in step for step in notes.next_steps)
    assert [*USER_SYSTEMCTL, "restart", UNIT_NAME] not in setup.commands


def test_a_changed_service_is_backed_up_rewritten_and_restarted(setup: Setup) -> None:
    setup.apply()
    setup.commands.clear()

    notes = setup.apply(tool="proton_9")

    backups = [path.name for path in setup.unit_file.parent.iterdir() if ".bak-" in path.name]
    assert backups == [f"{UNIT_NAME}.bak-20261026-183131"]
    assert [outcome.backup for outcome in notes.unit_outcomes] == [
        setup.unit_file.parent / backups[0]
    ]
    assert "--tool proton_9" in setup.unit_file.read_text()
    # enable --now leaves a running service on its old command line.
    assert setup.commands[-1] == [*USER_SYSTEMCTL, "restart", UNIT_NAME]


def test_a_skipped_watch_part_says_why_and_what_to_do(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.steam_root.rmdir()

    notes = setup.apply()

    assert setup.commands == []
    assert not setup.unit_file.exists()
    output = capsys.readouterr().out
    assert "  ! skipped    Steam has never run for player" in output
    assert "run Steam once as player" in notes.next_steps[0]


def test_a_failed_systemctl_says_how_to_see_why(setup: Setup) -> None:
    setup.command_error = subprocess.CalledProcessError(1, ["systemctl"])

    with pytest.raises(InstallError) as raised:
        setup.apply()

    message = str(raised.value)
    assert "systemctl --user status steamshare-watch.service" in message
    assert "logged in" in message
    assert "--no-watch" in message
