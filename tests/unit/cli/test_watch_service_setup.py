"""Tests for the watch part of `install`, with every system effect faked.

The user's home, Steam root and unit folder are temporary folders, and chown
and systemctl are recorded instead of run, so the tests change nothing on the
machine. They carry over what the removed `install-watch` command was tested
for, now done as root on the desktop user's behalf.
"""

import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.invoking_user import InvokingUser
from steamshare.cli.install_choices import InstallError
from steamshare.cli.watch_service_setup import (
    WatchServicePlan,
    WatchServiceSkipped,
    apply_watch_service,
    format_watch_summary,
    plan_watch_service,
    preview_watch_service,
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

    def apply(self, *, tool: str = "proton_experimental") -> None:
        apply_watch_service(
            self.plan(tool=tool),
            change_owner=self._change_owner,
            run_command=self._run,
        )

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
    assert "sudo ./steamshare install" in plan.next_step


def test_a_missing_launcher_is_an_error_that_names_the_way_out(setup: Setup) -> None:
    setup.launcher.unlink()

    with pytest.raises(InstallError, match="--no-watch"):
        setup.plan()


# --- Summary and dry run -----------------------------------------------------


def test_the_summary_names_the_service_and_the_debugger_flag(setup: Setup) -> None:
    lines = format_watch_summary(setup.plan())

    text = "\n".join(lines)
    assert str(setup.unit_file) in text
    assert "player" in text
    assert str(setup.flag_file) in text


def test_the_summary_says_when_the_watch_part_is_skipped(setup: Setup) -> None:
    lines = format_watch_summary(setup.plan(skip=True))

    assert any("skipped" in line for line in lines)


def test_a_dry_run_prints_the_plan_and_changes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    preview_watch_service(setup.plan())

    assert not setup.flag_file.exists()
    assert not setup.unit_file.parent.exists()
    output = capsys.readouterr().out
    assert f"would create: {setup.flag_file}" in output
    assert f"would create: {setup.unit_file}" in output
    assert "Restart=on-failure" in output
    assert (
        "would run: systemctl --user --machine=player@ enable --now steamshare-watch.service"
    ) in output
    assert "would run: systemctl --user --machine=player@ restart" not in output


def test_a_dry_run_over_an_older_service_plans_a_restart(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.unit_file.parent.mkdir(parents=True)
    setup.unit_file.write_text("ExecStart=/old/.venv/bin/python -m steamshare watch\n")

    preview_watch_service(setup.plan())

    output = capsys.readouterr().out
    assert f"would update (old file backed up first): {setup.unit_file}" in output
    assert "would run: systemctl --user --machine=player@ restart steamshare-watch.service" in (
        output
    )


# --- Applying ----------------------------------------------------------------


def test_turns_on_the_debugger_writes_the_service_and_starts_it_for_the_user(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.apply()

    assert setup.flag_file.read_text() == ""
    assert "--library /mnt/steamlib/SteamLibrary" in setup.unit_file.read_text()
    assert setup.flag_file in setup.owned
    assert setup.unit_file in setup.owned
    assert setup.commands == [
        [*USER_SYSTEMCTL, "daemon-reload"],
        [*USER_SYSTEMCTL, "enable", "--now", UNIT_NAME],
    ]
    output = capsys.readouterr().out
    assert "Restart Steam once" in output
    assert "Make default" in output
    assert "journalctl --user -u steamshare-watch.service" in output


def test_a_second_run_changes_nothing_restarts_nothing_and_needs_no_steam_restart(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.apply()
    setup.commands.clear()
    capsys.readouterr()

    setup.apply()

    output = capsys.readouterr().out
    assert output.count("unchanged") == 2
    assert "Restart Steam" not in output
    assert [*USER_SYSTEMCTL, "restart", UNIT_NAME] not in setup.commands


def test_a_changed_service_is_backed_up_rewritten_and_restarted(setup: Setup) -> None:
    setup.apply()
    setup.commands.clear()

    setup.apply(tool="proton_9")

    backups = [path.name for path in setup.unit_file.parent.iterdir() if ".bak-" in path.name]
    assert len(backups) == 1
    assert "--tool proton_9" in setup.unit_file.read_text()
    # enable --now leaves a running service on its old command line.
    assert setup.commands[-1] == [*USER_SYSTEMCTL, "restart", UNIT_NAME]


def test_a_skipped_watch_part_says_why_and_what_to_do(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.steam_root.rmdir()

    setup.apply()

    assert setup.commands == []
    assert not setup.unit_file.exists()
    output = capsys.readouterr().out
    assert "skipped" in output
    assert "run Steam once" in output


def test_a_failed_systemctl_says_how_to_see_why(setup: Setup) -> None:
    setup.command_error = subprocess.CalledProcessError(1, ["systemctl"])

    with pytest.raises(InstallError) as raised:
        setup.apply()

    message = str(raised.value)
    assert "systemctl --user status steamshare-watch.service" in message
    assert "logged in" in message
    assert "--no-watch" in message
