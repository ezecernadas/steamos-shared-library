"""Tests for the `uninstall` command's wiring, with every system effect faked.

The unit folder, the user's home and the mount point are temporary folders
holding what `install` writes, and root, systemctl and the console are
replaced, so the tests run without root and change nothing on the machine.
The last test installs with the real `install` command into such a fake
system and uninstalls it again, to show the two are symmetric.
"""

import argparse
import json
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from steamshare.__main__ import build_parser
from steamshare.adapters.console import Console
from steamshare.adapters.invoking_user import InvokingUser
from steamshare.adapters.steam_cdp import SteamDebuggerUnavailableError
from steamshare.adapters.steam_client_api import OwnedGames
from steamshare.cli import install as install_command
from steamshare.cli.install_choices import Discovery
from steamshare.cli.run_report import UNICODE_SYMBOLS
from steamshare.cli.uninstall import (
    EXIT_FAILURE,
    EXIT_OK,
    NOTHING_TO_UNINSTALL,
    System,
    uninstall,
)
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.systemd_units import (
    InstallConfig,
    WatchServiceConfig,
    build_units,
    mount_unit_name,
    render_watch_service,
)

BACKUP_SUFFIX = ".bak-20261026-183131"
STEAMSHARE_MAPPING = CompatToolSetting("proton_experimental", 250)
# What Steam reports for a native game once its mapping is cleared.
STEAM_DEFAULT = CompatToolSetting("steamlinuxruntime_sniper", 75)
WATCH_SERVICE = "steamshare-watch.service"
USER_SYSTEMCTL = ["systemctl", "--user", "--machine=player@"]


class FakeSystem:
    """A machine where steamshare is installed, in temporary folders."""

    def __init__(self, tmp_path: Path, *, is_root: bool = True) -> None:
        self.tmp_path = tmp_path
        self.unit_directory = tmp_path / "units"
        self.unit_directory.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.mount_point = tmp_path / "mnt" / "steamlib"
        # Stands in for /usr/local/bin, where install links `steamshare`.
        self.command_directory = tmp_path / "usr-local-bin"
        self.command_directory.mkdir()
        self.is_root = is_root
        self.user = InvokingUser(1000, 1000, "player", PurePosixPath(self.home))
        self.commands: list[list[str]] = []
        self.failing_commands: dict[str, Exception] = {}
        self.answers: list[str] = []
        self.prompts: list[str] = []
        self.interactive = False
        # The running Steam client a purge talks to.
        self.steam_running = True
        self.steam_games: list[OwnedGame] = []
        self.steam_changes: list[CompatToolChange] = []
        # What the machine looked like when Steam was asked to clear, to
        # check the order of a purge.
        self.when_cleared: dict[str, object] = {}

    def build(self) -> System:
        return System(
            is_root=self.is_root,
            invoking_user=self.user,
            unit_directory=self.unit_directory,
            environment={},
            run_command=self._run,
            console=Console(ask=self._ask, show=print, is_interactive=self.interactive),
            symbols=UNICODE_SYMBOLS,
            connect_to_steam=self._connect_to_steam,
            command_directory=self.command_directory,
        )

    # --- What install leaves behind ------------------------------------------

    def install_config(self) -> InstallConfig:
        return InstallConfig(
            partition_uuid="1111AAAA2222BBBB",
            mount_point=PurePosixPath(self.mount_point),
            library_folder="SteamLibrary",
            user_id=1000,
            group_id=1000,
            home=PurePosixPath(self.home),
        )

    def install_system_units(self, *, with_backups: bool = True) -> None:
        for unit in build_units(self.install_config()):
            (self.unit_directory / unit.name).write_text(unit.content)
            if with_backups:
                (self.unit_directory / f"{unit.name}{BACKUP_SUFFIX}").write_text(unit.content)
        self.mount_point.mkdir(parents=True)
        for data_folder in ("compatdata", "shadercache"):
            (self.home / ".local/share/steam-shared" / data_folder).mkdir(parents=True)

    def install_watch_service(self) -> None:
        unit = render_watch_service(
            WatchServiceConfig(
                launcher=PurePosixPath("/repository/steamshare"),
                library=PurePosixPath(self.mount_point / "SteamLibrary"),
                tool="proton_experimental",
                steam_root=PurePosixPath(self.steam_root),
            )
        )
        self.user_unit_directory.mkdir(parents=True)
        (self.user_unit_directory / unit.name).write_text(unit.content)
        self.debugger_flag.parent.mkdir(parents=True)
        self.debugger_flag.touch()
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text("{}\n")

    @property
    def user_unit_directory(self) -> Path:
        return self.home / ".config/systemd/user"

    @property
    def steam_root(self) -> Path:
        return self.home / ".steam/steam"

    @property
    def debugger_flag(self) -> Path:
        return self.steam_root / ".cef-enable-remote-debugging"

    @property
    def state_file(self) -> Path:
        return self.home / ".config/steamshare/state.json"

    def system_unit_names(self) -> list[str]:
        """Return the system units' names, in the order uninstall stops them."""
        library = self.mount_point / "SteamLibrary" / "steamapps"
        return [
            mount_unit_name(PurePosixPath(library / "compatdata")),
            mount_unit_name(PurePosixPath(library / "shadercache")),
            mount_unit_name(PurePosixPath(self.mount_point)),
            "steamshare-ntfs3.service",
        ]

    def remaining_unit_files(self) -> list[str]:
        return sorted(path.name for path in self.unit_directory.iterdir())

    # --- Fakes ---------------------------------------------------------------

    def _run(self, command: Sequence[str]) -> None:
        self.commands.append(list(command))
        for fragment, error in self.failing_commands.items():
            if fragment in command:
                raise error

    @contextmanager
    def _connect_to_steam(self, steam_root: Path) -> Iterator["FakeSystem"]:
        if not self.steam_running:
            raise SteamDebuggerUnavailableError("Steam's debugger is not answering")
        yield self

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames:
        games = tuple(game for game in self.steam_games if appids is None or game.appid in appids)
        return OwnedGames(games=games, without_details=())

    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]:
        self.steam_changes += changes
        self.when_cleared = {
            "commands": list(self.commands),
            "state file exists": self.state_file.exists(),
        }
        return {change.appid: STEAM_DEFAULT for change in changes}

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def make_arguments(
    *,
    dry_run: bool = False,
    yes: bool = True,
    remove_debugger_flag: bool = False,
    purge: bool = False,
    keep_steam_settings: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        dry_run=dry_run,
        yes=yes,
        remove_debugger_flag=remove_debugger_flag,
        purge=purge,
        keep_steam_settings=keep_steam_settings,
        steam_root=None,
    )


def installed_system(tmp_path: Path, *, is_root: bool = True) -> FakeSystem:
    system = FakeSystem(tmp_path, is_root=is_root)
    system.install_system_units()
    system.install_watch_service()
    return system


# --- Nothing installed -------------------------------------------------------


def test_with_nothing_installed_it_says_so_and_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    # Named like ours, but written by the user: not an installation.
    (system.unit_directory / "mnt-steamlib.mount").write_text("[Mount]\nWhere=/mnt/steamlib\n")

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == f"{NOTHING_TO_UNINSTALL}\n"
    assert system.remaining_unit_files() == ["mnt-steamlib.mount"]
    assert system.commands == []


def test_running_it_twice_finds_nothing_the_second_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    uninstall(make_arguments(), system.build())
    capsys.readouterr()

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == f"{NOTHING_TO_UNINSTALL}\n"


# --- Dry run -----------------------------------------------------------------


def test_a_dry_run_lists_everything_in_order_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path, is_root=False)
    names = system.system_unit_names()
    units = system.unit_directory
    mount_point = system.mount_point

    exit_code = uninstall(make_arguments(dry_run=True, yes=False), system.build())

    assert exit_code == EXIT_OK
    assert system.commands == []
    assert system.prompts == []
    assert len(system.remaining_unit_files()) == 8
    assert (system.user_unit_directory / WATCH_SERVICE).exists()
    output = capsys.readouterr().out.split("\nNothing was changed.")[0]
    assert output.splitlines() == [
        "Dry run: nothing is removed and systemctl is not run.",
        "",
        "Watch service  (~/.config/systemd/user)",
        "  · would disable  steamshare-watch.service",
        "  · would remove   steamshare-watch.service",
        "  · would reload   systemd for player",
        "  · would remove   ~/.config/steamshare/state.json",
        "  · would remove   ~/.config/steamshare  (if empty)",
        "",
        f"Mounts  ({units})",
        f"  · would disable  {names[0]}",
        f"  · would disable  {names[1]}",
        f"  · would disable  {names[2]}",
        f"  · would stop     {names[3]}",
        f"  · would remove   {names[0]}",
        f"  · would remove   {names[0]}{BACKUP_SUFFIX}",
        f"  · would remove   {names[1]}",
        f"  · would remove   {names[1]}{BACKUP_SUFFIX}",
        f"  · would remove   {names[2]}",
        f"  · would remove   {names[2]}{BACKUP_SUFFIX}",
        f"  · would remove   {names[3]}",
        f"  · would remove   {names[3]}{BACKUP_SUFFIX}",
        "  · would reload   systemd",
        f"  · would remove   {mount_point}  (if empty)",
        "",
        "Kept",
        "  ✓ kept           ~/.local/share/steam-shared/compatdata",
        "  ✓ kept           ~/.local/share/steam-shared/shadercache",
        "  ✓ kept           the Proton choices watch made in Steam",
        "  ✓ kept           Steam debugger flag (~/.steam/steam/.cef-enable-remote-debugging)",
        "  ✓ kept           this repository and the files on the NTFS partition",
    ]


def test_a_dry_run_ends_with_what_is_kept_and_how_to_remove_it_by_hand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path, is_root=False)

    uninstall(make_arguments(dry_run=True), system.build())

    closing = capsys.readouterr().out.split("Nothing was changed. After a real uninstall:\n")[1]
    data = system.home / ".local/share/steam-shared"
    assert f"rm -rf {data / 'compatdata'} {data / 'shadercache'}" in closing
    assert "re-download" in closing
    assert "Properties → Compatibility" in closing
    assert "Decky Loader" in closing
    assert "--remove-debugger-flag" in closing


# --- Running it --------------------------------------------------------------


def test_removes_every_unit_backup_and_state_and_keeps_the_users_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    names = system.system_unit_names()

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert system.remaining_unit_files() == []
    assert list(system.user_unit_directory.iterdir()) == []
    assert not system.state_file.parent.exists()
    assert not system.mount_point.exists()
    assert (system.home / ".local/share/steam-shared/compatdata").is_dir()
    assert system.debugger_flag.exists()
    output = capsys.readouterr().out
    assert "  ✓ disabled  steamshare-watch.service\n" in output
    assert "  ✓ stopped   steamshare-ntfs3.service\n" in output
    assert f"  ✓ removed   {names[2]}{BACKUP_SUFFIX}\n" in output
    assert "Done. What was kept, and what you can do with it:" in output


def test_a_confirmed_run_shows_a_short_summary_then_every_file_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    names = system.system_unit_names()
    system.interactive = True
    system.answers = ["y"]

    uninstall(make_arguments(yes=False), system.build())

    output = capsys.readouterr().out.split("Done. What was kept")[0]
    assert "would" not in output
    assert output.splitlines() == [
        "Uninstall summary",
        "  Remove  watch service, 4 systemd units, 4 backups, steamshare state, "
        "the mount point folder (if empty)",
        "  Keep    Proton prefixes and shader caches (~/.local/share/steam-shared)",
        "          the Proton choices watch made in Steam",
        "          Steam debugger flag",
        "          this repository and the files on the NTFS partition",
        "Add --dry-run to see every file.",
        "",
        # Shown after the answer, which the fake console does not echo.
        "",
        "Watch service  (~/.config/systemd/user)",
        "  ✓ disabled  steamshare-watch.service",
        "  ✓ removed   steamshare-watch.service",
        "  ✓ reloaded  systemd for player",
        "  ✓ removed   ~/.config/steamshare/state.json",
        "  ✓ removed   ~/.config/steamshare",
        "",
        f"Mounts  ({system.unit_directory})",
        f"  ✓ disabled  {names[0]}",
        f"  ✓ disabled  {names[1]}",
        f"  ✓ disabled  {names[2]}",
        f"  ✓ stopped   {names[3]}",
        *(
            line
            for name in names
            for line in (f"  ✓ removed   {name}", f"  ✓ removed   {name}{BACKUP_SUFFIX}")
        ),
        "  ✓ reloaded  systemd",
        f"  ✓ removed   {system.mount_point}",
        "",
    ]
    assert system.prompts == ["Proceed? [y/N]: "]


def test_systemctl_runs_the_watch_service_first_then_the_mounts_nested_first(
    tmp_path: Path,
) -> None:
    system = installed_system(tmp_path)
    names = system.system_unit_names()

    uninstall(make_arguments(), system.build())

    assert system.commands == [
        [*USER_SYSTEMCTL, "disable", "--now", WATCH_SERVICE],
        [*USER_SYSTEMCTL, "daemon-reload"],
        ["systemctl", "disable", "--now", names[0]],
        ["systemctl", "disable", "--now", names[1]],
        ["systemctl", "disable", "--now", names[2]],
        ["systemctl", "stop", names[3]],
        ["systemctl", "daemon-reload"],
    ]


def test_remove_debugger_flag_deletes_it_and_says_to_restart_steam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)

    uninstall(make_arguments(remove_debugger_flag=True), system.build())

    assert not system.debugger_flag.exists()
    output = capsys.readouterr().out
    assert "  ✓ removed   ~/.steam/steam/.cef-enable-remote-debugging\n" in output
    assert "restart Steam" in output


def test_a_mount_point_that_is_not_empty_is_left_with_its_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    (system.mount_point / "someone's file").write_text("keep me\n")

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert (system.mount_point / "someone's file").read_text() == "keep me\n"
    assert f"  ! skipped   {system.mount_point}: not empty, so it stays\n" in (
        capsys.readouterr().out
    )


# --- Partial installations ---------------------------------------------------


def test_only_the_watch_service_is_removed_when_only_it_is_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.install_watch_service()

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert not (system.user_unit_directory / WATCH_SERVICE).exists()
    assert all(command[:2] == ["systemctl", "--user"] for command in system.commands)
    assert "Mounts" not in capsys.readouterr().out


def test_only_the_system_units_are_removed_when_only_they_are_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.install_system_units(with_backups=False)

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert system.remaining_unit_files() == []
    assert not any("--user" in command for command in system.commands)
    assert "Watch service" not in capsys.readouterr().out


def test_a_unit_removed_by_hand_is_skipped_and_the_rest_removed(tmp_path: Path) -> None:
    system = installed_system(tmp_path)
    names = system.system_unit_names()
    (system.unit_directory / names[1]).unlink()

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert ["systemctl", "disable", "--now", names[1]] not in system.commands
    # Its backup is only found through the unit, so it stays, unnamed.
    assert system.remaining_unit_files() == [f"{names[1]}{BACKUP_SUFFIX}"]


# --- Failures ----------------------------------------------------------------


def test_an_unreachable_user_manager_still_removes_the_rest_and_says_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    system.failing_commands = {"--machine=player@": subprocess.CalledProcessError(1, "x")}

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert not (system.user_unit_directory / WATCH_SERVICE).exists()
    assert system.remaining_unit_files() == []
    # The reload is not tried once the manager was found unreachable.
    assert [*USER_SYSTEMCTL, "daemon-reload"] not in system.commands
    output = capsys.readouterr().out
    assert "  ! skipped   steamshare-watch.service: player's service manager is not" in output
    link = system.user_unit_directory / "default.target.wants" / WATCH_SERVICE
    assert f"rm {link}" in output
    assert f"systemctl --user stop {WATCH_SERVICE}" in output


def test_a_mount_that_cannot_be_stopped_stops_the_run_before_its_files_go(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    names = system.system_unit_names()
    system.failing_commands = {names[2]: subprocess.CalledProcessError(1, "x")}

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert len(system.remaining_unit_files()) == 8
    error = capsys.readouterr().err
    assert shlex.join(["systemctl", "disable", "--now", names[2]]) in error
    assert "close Steam" in error
    assert "sudo ./steamshare uninstall" in error


# --- Asking ------------------------------------------------------------------


def test_refuses_to_run_without_root_and_suggests_a_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path, is_root=False)

    exit_code = uninstall(make_arguments(remove_debugger_flag=True), system.build())

    assert exit_code == EXIT_FAILURE
    assert len(system.remaining_unit_files()) == 8
    error = capsys.readouterr().err
    assert "sudo ./steamshare uninstall --yes --remove-debugger-flag" in error
    assert "--dry-run" in error


def test_refuses_to_run_for_root_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    system = installed_system(tmp_path)
    system.user = InvokingUser(0, 0, "root", PurePosixPath("/root"))

    exit_code = uninstall(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert "desktop user" in capsys.readouterr().err


def test_the_default_answer_is_no_and_removes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    system.interactive = True
    system.answers = [""]

    exit_code = uninstall(make_arguments(yes=False), system.build())

    assert exit_code == EXIT_FAILURE
    assert system.prompts == ["Proceed? [y/N]: "]
    assert len(system.remaining_unit_files()) == 8
    assert system.commands == []
    assert "nothing was changed" in capsys.readouterr().err


def test_yes_at_the_prompt_proceeds(tmp_path: Path) -> None:
    system = installed_system(tmp_path)
    system.interactive = True
    system.answers = ["y"]

    exit_code = uninstall(make_arguments(yes=False), system.build())

    assert exit_code == EXIT_OK
    assert system.remaining_unit_files() == []


def test_without_a_terminal_it_needs_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)

    exit_code = uninstall(make_arguments(yes=False), system.build())

    assert exit_code == EXIT_FAILURE
    assert len(system.remaining_unit_files()) == 8
    assert "--yes" in capsys.readouterr().err


# --- The command line --------------------------------------------------------


def test_uninstall_is_a_command_with_its_flags() -> None:
    arguments = build_parser().parse_args(
        ["uninstall", "--dry-run", "--yes", "--remove-debugger-flag", "--steam-root", "/s"]
    )

    assert arguments.dry_run and arguments.yes and arguments.remove_debugger_flag
    assert arguments.steam_root == Path("/s")
    defaults = build_parser().parse_args(["uninstall"])
    assert not (defaults.dry_run or defaults.yes or defaults.remove_debugger_flag)
    assert defaults.steam_root is None


# --- Purge -------------------------------------------------------------------


def mapped_game(appid: str, *, installed: bool) -> OwnedGame:
    return OwnedGame(appid, f"Game {appid}", installed, None, STEAMSHARE_MAPPING)


def purgeable_system(tmp_path: Path, *, is_root: bool = True) -> FakeSystem:
    """An installed system whose watch mapped games 10 and 11; 11 is installed."""
    system = installed_system(tmp_path, is_root=is_root)
    system.state_file.write_text(json.dumps({"managed_mappings": ["10", "11"]}))
    system.steam_games = [mapped_game("10", installed=False), mapped_game("11", installed=True)]
    prefix = system.home / ".local/share/steam-shared/compatdata/10/pfx"
    prefix.mkdir(parents=True)
    (prefix / "user.reg").write_text("save data\n")
    return system


def test_purge_removes_the_data_the_flag_and_the_choices_of_games_not_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)

    exit_code = uninstall(make_arguments(purge=True), system.build())

    assert exit_code == EXIT_OK
    assert not (system.home / ".local/share/steam-shared").exists()
    assert not system.debugger_flag.exists()
    assert not system.state_file.exists()
    # The installed game keeps its mapping: clearing it would re-download it.
    assert [(change.appid, change.tool) for change in system.steam_changes] == [("10", "")]
    output = capsys.readouterr().out
    assert "  ✓ cleared   Proton choices of 1 game not installed\n" in output
    assert "Proton choices of installed games stay in Steam" in output
    assert "Disk Management" in output


def test_purge_stops_watch_before_clearing_and_removes_the_state_after(
    tmp_path: Path,
) -> None:
    system = purgeable_system(tmp_path)
    names = system.system_unit_names()

    uninstall(make_arguments(purge=True), system.build())

    # watch would map the games again; the state says which mappings are ours.
    assert system.when_cleared["commands"] == [
        [*USER_SYSTEMCTL, "disable", "--now", WATCH_SERVICE],
        [*USER_SYSTEMCTL, "daemon-reload"],
    ]
    assert system.when_cleared["state file exists"] is True
    assert ["systemctl", "disable", "--now", names[0]] in system.commands


def test_purge_deletes_the_data_only_after_the_bind_mounts_are_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)

    uninstall(make_arguments(purge=True), system.build())

    output = capsys.readouterr().out
    last_disable = output.index(f"disabled  {system.system_unit_names()[2]}")
    data_removed = output.index("removed   ~/.local/share/steam-shared/compatdata")
    assert last_disable < data_removed


def test_purge_asks_for_the_word_typed_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)
    system.interactive = True
    system.answers = ["purge"]

    exit_code = uninstall(make_arguments(purge=True, yes=False), system.build())

    assert exit_code == EXIT_OK
    assert system.prompts == ['Type "purge" to delete these permanently: ']


@pytest.mark.parametrize("answer", ["", "y", "yes", "PURGE"])
def test_anything_but_the_typed_word_cancels_a_purge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    system = purgeable_system(tmp_path)
    system.interactive = True
    system.answers = [answer]

    exit_code = uninstall(make_arguments(purge=True, yes=False), system.build())

    assert exit_code == EXIT_FAILURE
    assert system.commands == []
    assert system.steam_changes == []
    assert (system.home / ".local/share/steam-shared/compatdata/10").is_dir()
    assert "Cancelled; nothing was changed." in capsys.readouterr().err


def test_purge_without_a_terminal_needs_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)

    exit_code = uninstall(make_arguments(purge=True, yes=False), system.build())

    assert exit_code == EXIT_FAILURE
    assert system.commands == []
    assert "--yes" in capsys.readouterr().err


def test_purge_refuses_before_any_change_when_steam_is_not_reachable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)
    system.steam_running = False

    exit_code = uninstall(make_arguments(purge=True), system.build())

    assert exit_code == EXIT_FAILURE
    assert system.commands == []
    assert len(system.remaining_unit_files()) == 8
    assert system.state_file.exists()
    error = capsys.readouterr().err
    assert "Start Steam" in error
    assert "sudo ./steamshare uninstall --purge" in error
    assert "--keep-steam-settings" in error


def test_keep_steam_settings_purges_without_steam_and_keeps_every_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)
    system.steam_running = False

    exit_code = uninstall(make_arguments(purge=True, keep_steam_settings=True), system.build())

    assert exit_code == EXIT_OK
    assert system.steam_changes == []
    assert not (system.home / ".local/share/steam-shared").exists()
    assert "The Proton choices watch made stay in Steam" in capsys.readouterr().out


def test_keep_steam_settings_needs_purge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path)

    exit_code = uninstall(make_arguments(keep_steam_settings=True), system.build())

    assert exit_code == EXIT_FAILURE
    assert "--purge" in capsys.readouterr().err


def test_a_purge_dry_run_lists_the_data_with_sizes_and_the_choices_it_clears(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path, is_root=False)

    exit_code = uninstall(make_arguments(purge=True, dry_run=True, yes=False), system.build())

    assert exit_code == EXIT_OK
    assert system.steam_changes == []
    assert system.commands == []
    output = capsys.readouterr().out
    watch = output[output.index("Watch service") : output.index("Mounts")]
    assert watch.splitlines()[4] == "  · would clear    Proton choices of 1 game not installed"
    purge = output[output.index("\nPurge\n") : output.index("\nKept\n")].splitlines()[2:]
    # Sizes depend on the filesystem's folder sizes, so only their place is checked.
    assert purge[0].startswith("  · would remove   ~/.local/share/steam-shared/compatdata  (")
    assert purge[1].startswith("  · would remove   ~/.local/share/steam-shared/shadercache  (")
    assert purge[2:] == [
        "  · would remove   ~/.local/share/steam-shared  (if empty)",
        "  · would remove   ~/.steam/steam/.cef-enable-remote-debugging",
    ]
    kept = output[output.index("\nKept\n") :].splitlines()[2:4]
    assert kept == [
        "  ✓ kept           Proton choices of 1 installed game (clearing them would "
        "re-download them as Linux builds)",
        "  ✓ kept           this repository and the files on the NTFS partition",
    ]


def test_a_purge_dry_run_without_steam_says_how_many_games_it_would_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = purgeable_system(tmp_path, is_root=False)
    system.steam_running = False

    exit_code = uninstall(make_arguments(purge=True, dry_run=True), system.build())

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "would clear    Proton choices of the games not installed, among 2" in output
    assert "Steam is not running, so the 2 games steamshare mapped were not checked" in output


# --- Install, then uninstall -------------------------------------------------


def test_uninstall_removes_everything_install_wrote_except_what_it_keeps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.steam_root.mkdir(parents=True)
    system.mount_point.mkdir(parents=True)
    launcher = make_checkout(tmp_path / "repository")
    before = snapshot(tmp_path)

    install_exit_code = install_command.install(
        install_arguments(system.mount_point), install_system(system, launcher)
    )
    # watch writes the state file once it runs; install itself does not.
    system.state_file.parent.mkdir(parents=True)
    system.state_file.write_text("{}\n")
    installed = snapshot(tmp_path)
    uninstall_exit_code = uninstall(make_arguments(), system.build())

    assert (install_exit_code, uninstall_exit_code) == (EXIT_OK, EXIT_OK)
    data = system.home / ".local/share/steam-shared"
    kept = {
        # The Proton prefixes and shader caches, and the folders that hold them.
        *(data / name for name in ("compatdata", "shadercache")),
        data,
        data.parent,
        data.parent.parent,
        # Steam's debugger flag, kept unless --remove-debugger-flag is given.
        system.debugger_flag,
        # The user's unit folder, which systemd's own tools use as well.
        system.user_unit_directory,
        system.user_unit_directory.parent,
        system.user_unit_directory.parent.parent,
    }
    assert installed - before > kept
    assert snapshot(tmp_path) - before == kept
    # The mount point existed before install (systemd creates it at the
    # first mount); empty once unmounted, it is removed.
    assert not system.mount_point.exists()
    output = capsys.readouterr().out
    assert "Done. What was kept, and what you can do with it:" in output


def test_install_then_purge_leaves_nothing_steamshare_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    # What a desktop already has before install: the standard folders of the
    # user's home, Steam, and the mount point systemd creates at the first mount.
    for folder in (".config", ".local/share", ".steam/steam"):
        (system.home / folder).mkdir(parents=True)
    system.mount_point.mkdir(parents=True)
    launcher = make_checkout(tmp_path / "repository")
    before = snapshot(tmp_path)

    install_command.install(install_arguments(system.mount_point), install_system(system, launcher))
    # What watch adds once it runs: its state, and a game's Proton prefix.
    system.state_file.parent.mkdir(parents=True)
    system.state_file.write_text(json.dumps({"managed_mappings": ["10"]}))
    (system.home / ".local/share/steam-shared/compatdata/10/pfx").mkdir(parents=True)
    system.steam_games = [mapped_game("10", installed=False)]
    exit_code = uninstall(make_arguments(purge=True), system.build())

    assert exit_code == EXIT_OK
    assert [change.appid for change in system.steam_changes] == ["10"]
    # Only the user's systemd unit folder stays, empty: other user units live
    # there too, and on a real system `systemctl disable` leaves its empty
    # default.target.wants folder in it, so removing it would be noise.
    user_systemd = {system.user_unit_directory, system.user_unit_directory.parent}
    assert snapshot(tmp_path) - before == user_systemd
    assert before - snapshot(tmp_path) == {system.mount_point}
    assert launcher.exists()


def snapshot(root: Path) -> set[Path]:
    return set(root.rglob("*"))


def install_arguments(mount_point: Path) -> argparse.Namespace:
    return argparse.Namespace(
        partition_uuid="1111AAAA2222BBBB",
        mount_point=PurePosixPath(mount_point),
        library_folder="SteamLibrary",
        dry_run=False,
        yes=True,
        no_watch=False,
        show_units=False,
        steam_root=None,
        tool="proton_experimental",
    )


def install_system(system: FakeSystem, launcher: Path) -> install_command.System:
    return install_command.System(
        is_root=True,
        invoking_user=system.user,
        unit_directory=system.unit_directory,
        partition_exists=lambda device: True,
        enable_units=lambda unit_names: None,
        change_owner=lambda path, user_id, group_id: None,
        discovery=Discovery(list_partitions=list, probe_partition=_never_probed),
        console=Console(ask=input, show=print, is_interactive=False),
        launcher=launcher,
        run_command=lambda command: None,
        symbols=UNICODE_SYMBOLS,
        command_directory=system.command_directory,
        is_read_only=lambda directory: False,
        change_link_owner=lambda path, user_id, group_id: None,
    )


def make_checkout(folder: Path) -> Path:
    """Return the launcher of a steamshare checkout in `folder`, as uninstall recognizes it."""
    (folder / "src" / "steamshare").mkdir(parents=True)
    launcher = folder / "steamshare"
    launcher.write_text("#!/usr/bin/env bash\n")
    return launcher


def _never_probed(partition: object) -> object:
    raise AssertionError("both install flags are given, so no partition is probed")


# --- The steamshare command on PATH -------------------------------------------


def test_removes_the_steamshare_link_install_put_on_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path)
    link = system.command_directory / "steamshare"
    link.symlink_to(make_checkout(tmp_path / "repository"))

    assert uninstall(make_arguments(), system.build()) == EXIT_OK

    assert not link.is_symlink()
    assert f"  ✓ removed   {link}\n" in capsys.readouterr().out


def test_removes_the_link_in_the_users_folder_too(tmp_path: Path) -> None:
    system = installed_system(tmp_path)
    link = system.home / ".local/bin/steamshare"
    link.parent.mkdir(parents=True)
    link.symlink_to(make_checkout(tmp_path / "repository"))

    uninstall(make_arguments(), system.build())

    assert not link.is_symlink()
    # The folder holds the user's other commands; it stays.
    assert link.parent.is_dir()


def test_a_dry_run_lists_the_link_and_keeps_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = installed_system(tmp_path, is_root=False)
    link = system.command_directory / "steamshare"
    link.symlink_to(make_checkout(tmp_path / "repository"))

    uninstall(make_arguments(dry_run=True), system.build())

    assert link.is_symlink()
    assert f"  · would remove   {link}\n" in capsys.readouterr().out


def test_a_steamshare_file_that_is_not_our_link_stays(tmp_path: Path) -> None:
    system = installed_system(tmp_path)
    other = system.command_directory / "steamshare"
    other.write_text("#!/bin/sh\n")

    uninstall(make_arguments(), system.build())

    assert other.read_text() == "#!/bin/sh\n"


def test_a_link_left_alone_still_counts_as_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    link = system.command_directory / "steamshare"
    link.symlink_to(make_checkout(tmp_path / "repository"))

    uninstall(make_arguments(), system.build())

    assert not link.is_symlink()
    assert NOTHING_TO_UNINSTALL not in capsys.readouterr().out


def test_purge_and_keep_steam_settings_are_flags_off_by_default() -> None:
    arguments = build_parser().parse_args(["uninstall", "--purge", "--keep-steam-settings"])

    assert arguments.purge and arguments.keep_steam_settings
    defaults = build_parser().parse_args(["uninstall"])
    assert not (defaults.purge or defaults.keep_steam_settings)
