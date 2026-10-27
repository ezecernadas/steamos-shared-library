"""Tests for the `install` command's wiring, with every system effect faked.

The unit directory and the user's home are temporary folders, and root, the
partition lookup, systemctl (system and user), chown, the partition listing
and probing, and the console are replaced, so the tests run without root and
change nothing on the machine. The watch part is tested in detail in
`test_watch_service_setup`; here only its wiring into install is.
"""

import argparse
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest

from steamshare.__main__ import build_parser
from steamshare.adapters.console import Console
from steamshare.adapters.invoking_user import InvokingUser
from steamshare.adapters.launcher_location import LAUNCHER_PATH
from steamshare.cli.install import (
    EXIT_FAILURE,
    EXIT_OK,
    System,
    install,
)
from steamshare.cli.install_choices import Discovery
from steamshare.cli.run_report import UNICODE_SYMBOLS
from steamshare.cli.shared import absolute_path, relative_folder
from steamshare.core.ntfs_partitions import (
    NtfsPartition,
    PartitionContents,
    PartitionFacts,
    not_inspected_without_root,
)
from steamshare.core.systemd_units import DEFAULT_MOUNT_POINT

USER = InvokingUser(user_id=1000, group_id=1000, name="player", home=PurePosixPath("/home/player"))
EXPECTED_UNITS = [
    "steamshare-ntfs3.service",
    "mnt-steamlib.mount",
    "mnt-steamlib-SteamLibrary-steamapps-compatdata.mount",
    "mnt-steamlib-SteamLibrary-steamapps-shadercache.mount",
]


WINDOWS = NtfsPartition(
    PurePosixPath("/dev/nvme0n1p3"), 192277381120, None, "3333CCCC4444DDDD", None
)
LIBRARY = NtfsPartition(
    PurePosixPath("/dev/nvme0n1p4"), 103078166528, "SteamLib", "1111AAAA2222BBBB", None
)
WINDOWS_CONTENTS = PartitionContents(
    has_windows_system=True, has_recovery_folder=True, library_folders=()
)
LIBRARY_CONTENTS = PartitionContents(
    has_windows_system=False, has_recovery_folder=False, library_folders=("SteamLibrary",)
)


class FakeSystem:
    """Builds a `System` whose effects are recorded instead of applied."""

    def __init__(self, tmp_path: Path, *, is_root: bool = True, user: InvokingUser = USER) -> None:
        self.unit_directory = tmp_path / "units"
        self.unit_directory.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.enabled: list[list[str]] = []
        self.owners: list[Path] = []
        self.partition_present = True
        self.enable_error: Exception | None = None
        self.is_root = is_root
        self.user = InvokingUser(user.user_id, user.group_id, user.name, PurePosixPath(self.home))
        self.launcher = tmp_path / "repository" / "steamshare"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/usr/bin/env bash\n")
        # Stands in for /usr/local/bin, where the `steamshare` link goes.
        self.command_directory = tmp_path / "usr-local-bin"
        self.command_directory.mkdir()
        self.command_directory_is_read_only = False
        self.user_commands: list[list[str]] = []
        # Every effect in order, to check that the library folder is created
        # only after the mounts are enabled.
        self.events: list[str] = []
        self.answers: list[str] = []
        self.prompts: list[str] = []
        self.interactive = False
        self.contents = {WINDOWS: WINDOWS_CONTENTS, LIBRARY: LIBRARY_CONTENTS}

    def build(self) -> System:
        return System(
            is_root=self.is_root,
            invoking_user=self.user,
            unit_directory=self.unit_directory,
            partition_exists=lambda device: self.partition_present,
            enable_units=self._enable,
            change_owner=self._change_owner,
            discovery=Discovery(
                list_partitions=lambda: list(self.contents), probe_partition=self._probe
            ),
            console=Console(ask=self._ask, show=print, is_interactive=self.interactive),
            launcher=self.launcher,
            run_command=self._run_user_command,
            symbols=UNICODE_SYMBOLS,
            command_directory=self.command_directory,
            is_read_only=lambda directory: self.command_directory_is_read_only,
            change_link_owner=self._change_owner,
        )

    @property
    def steam_root(self) -> Path:
        return self.home / ".steam" / "steam"

    @property
    def watch_unit(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / "steamshare-watch.service"

    def _run_user_command(self, command: Sequence[str]) -> None:
        self.user_commands.append(list(command))
        self.events.append(f"user {command[-1]}")

    def _enable(self, unit_names: list[str]) -> None:
        if self.enable_error is not None:
            raise self.enable_error
        self.enabled.append(unit_names)
        self.events.append("enable")

    def _change_owner(self, path: Path, user_id: int, group_id: int) -> None:
        self.owners.append(path)
        self.events.append(f"own {path.name}")

    def _probe(self, partition: NtfsPartition) -> PartitionFacts:
        if not self.is_root:
            return not_inspected_without_root(partition)
        return PartitionFacts(partition, self.contents[partition])

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def make_arguments(
    dry_run: bool = False,
    *,
    partition_uuid: str | None = "1111AAAA2222BBBB",
    library_folder: str | None = "SteamLibrary",
    mount_point: PurePosixPath = DEFAULT_MOUNT_POINT,
    yes: bool = False,
    no_watch: bool = False,
    show_units: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        partition_uuid=partition_uuid,
        mount_point=mount_point,
        library_folder=library_folder,
        dry_run=dry_run,
        yes=yes,
        no_watch=no_watch,
        show_units=show_units,
        steam_root=None,
        tool="proton_experimental",
    )


def test_real_run_writes_units_creates_folders_and_enables_the_mounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert sorted(path.name for path in system.unit_directory.iterdir()) == sorted(EXPECTED_UNITS)
    assert (system.home / ".local/share/steam-shared/compatdata").is_dir()
    assert system.enabled == [EXPECTED_UNITS[1:]]
    output = capsys.readouterr().out
    assert "created" in output
    assert "steamshare doctor --library /mnt/steamlib/SteamLibrary" in output


def test_second_run_reports_every_unit_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    install(make_arguments(), system.build())
    capsys.readouterr()

    install(make_arguments(), system.build())

    output = capsys.readouterr().out
    assert output.count("unchanged") == len(EXPECTED_UNITS) + 2  # plus the two folders
    assert "created" not in output


def test_dry_run_lists_every_unit_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    exit_code = install(make_arguments(dry_run=True), system.build())

    assert exit_code == EXIT_OK
    assert list(system.unit_directory.iterdir()) == []
    assert not (system.home / ".local").exists()
    assert system.enabled == []
    output = capsys.readouterr().out
    for name in EXPECTED_UNITS:
        assert name in output
    assert "systemctl enable --now mnt-steamlib.mount" in output


def test_refuses_to_run_without_root_and_explains_how(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert list(system.unit_directory.iterdir()) == []
    error = capsys.readouterr().err
    assert f"sudo {LAUNCHER_PATH} install --partition-uuid 1111AAAA2222BBBB" in error
    assert "--dry-run" in error


def test_refuses_to_install_for_root_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = InvokingUser(user_id=0, group_id=0, name="root", home=PurePosixPath("/root"))
    system = FakeSystem(tmp_path, user=root)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert "desktop user" in capsys.readouterr().err


def test_refuses_an_unknown_partition_uuid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.partition_present = False

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert list(system.unit_directory.iterdir()) == []
    error = capsys.readouterr().err
    assert "/dev/disk/by-uuid/1111AAAA2222BBBB" in error
    assert "lsblk -f" in error


def test_reports_a_systemctl_failure_with_the_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.enable_error = subprocess.CalledProcessError(1, ["systemctl", "enable"])

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    error = capsys.readouterr().err
    assert "systemctl status mnt-steamlib.mount" in error


# --- The whole report --------------------------------------------------------


def test_a_first_install_reports_each_section_once_and_ends_with_the_next_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.steam_root.mkdir(parents=True)

    install(make_arguments(partition_uuid=None, library_folder=None, yes=True), system.build())

    assert capsys.readouterr().out.splitlines() == [
        "NTFS partitions",
        "  1) /dev/nvme0n1p3  179 GB            Windows system partition (not recommended)",
        "  2) /dev/nvme0n1p4   96 GB  SteamLib  Steam library in SteamLibrary/",
        "",
        "Summary",
        "  Partition      /dev/nvme0n1p4  (SteamLib, 96 GB, UUID 1111AAAA2222BBBB)",
        "  Mounted at     /mnt/steamlib",
        "  Library        /mnt/steamlib/SteamLibrary",
        "  Watch service  for user player",
        f"  Command        steamshare, linked at {system.command_directory}/steamshare",
        "",
        f"Mounts  ({system.unit_directory})",
        "  ✓ created    steamshare-ntfs3.service",
        "  ✓ created    mnt-steamlib.mount",
        "  ✓ created    mnt-steamlib-SteamLibrary-steamapps-compatdata.mount",
        "  ✓ created    mnt-steamlib-SteamLibrary-steamapps-shadercache.mount",
        "  ✓ created    ~/.local/share/steam-shared/compatdata",
        "  ✓ created    ~/.local/share/steam-shared/shadercache",
        "  ✓ started    3 mounts",
        "",
        "Watch service  (~/.config/systemd/user)",
        "  ✓ created    Steam debugger flag (~/.steam/steam/.cef-enable-remote-debugging)",
        "  ✓ created    steamshare-watch.service",
        "  ✓ running    for player",
        "",
        f"Command  ({system.command_directory})",
        f"  ✓ created    steamshare → {system.launcher}",
        "",
        "Done. Next steps:",
        "  1. Restart Steam once (Steam → Exit, then start it again) so it turns its debugger "
        "on. Until then, watch waits for it.",
        "  2. In Steam: Settings → Storage → the shared library → Make default, so a click on "
        "Install puts games there and fetches their Windows build.",
        "  3. Check the setup, without sudo:  steamshare doctor --library "
        "/mnt/steamlib/SteamLibrary",
        "  Follow watch:  journalctl --user -u steamshare-watch.service -f",
    ]


def test_replaced_files_are_summed_up_in_one_backup_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.steam_root.mkdir(parents=True)
    install(make_arguments(), system.build())
    (system.unit_directory / "mnt-steamlib.mount").write_text("old\n")
    system.watch_unit.write_text("old\n")
    capsys.readouterr()

    install(make_arguments(), system.build())

    output = capsys.readouterr().out
    assert "  ✓ updated    mnt-steamlib.mount\n" in output
    assert "  ✓ updated    steamshare-watch.service\n" in output
    [backup] = (system.unit_directory).glob("mnt-steamlib.mount.bak-*")
    timestamp = backup.name.rpartition(".bak-")[2]
    # One run, one timestamp: the units and the watch service share it.
    assert (system.watch_unit.parent / f"steamshare-watch.service.bak-{timestamp}").exists()
    assert f"Replaced files were backed up next to themselves (*.bak-{timestamp}).\n" in output


def test_a_dry_run_reports_the_same_sections_with_would_verbs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)
    system.steam_root.mkdir(parents=True)

    install(make_arguments(dry_run=True), system.build())

    output = capsys.readouterr().out
    mounts = output[output.index(f"Mounts  ({system.unit_directory})") :]
    assert mounts.splitlines()[:11] == [
        f"Mounts  ({system.unit_directory})",
        "  · would create  steamshare-ntfs3.service",
        "  · would create  mnt-steamlib.mount",
        "  · would create  mnt-steamlib-SteamLibrary-steamapps-compatdata.mount",
        "  · would create  mnt-steamlib-SteamLibrary-steamapps-shadercache.mount",
        "  · would create  ~/.local/share/steam-shared/compatdata",
        "  · would create  ~/.local/share/steam-shared/shadercache",
        "  · would run     systemctl daemon-reload",
        "  · would run     systemctl enable --now mnt-steamlib.mount "
        "mnt-steamlib-SteamLibrary-steamapps-compatdata.mount "
        "mnt-steamlib-SteamLibrary-steamapps-shadercache.mount",
        "",
        "Watch service  (~/.config/systemd/user)",
    ]
    closing = output.split("Nothing was changed. After a real install, the next steps are:\n")
    assert closing[1].splitlines()[1].startswith("  2. In Steam: Settings → Storage")
    assert "Done." not in output


def test_puts_the_steamshare_command_on_path(tmp_path: Path) -> None:
    system = FakeSystem(tmp_path)

    assert install(make_arguments(), system.build()) == EXIT_OK

    assert (system.command_directory / "steamshare").readlink() == system.launcher


def test_on_a_read_only_root_the_command_goes_in_the_users_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.command_directory_is_read_only = True

    install(make_arguments(), system.build())

    link = system.home / ".local/bin/steamshare"
    assert link.readlink() == system.launcher
    assert link in system.owners
    assert "~/.local/bin" in capsys.readouterr().out.split("Done. Next steps:")[1]


def test_a_dry_run_shows_the_command_link_and_creates_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    install(make_arguments(dry_run=True), system.build())

    assert not (system.command_directory / "steamshare").is_symlink()
    assert f"  · would create  steamshare → {system.launcher}" in capsys.readouterr().out


# --- Choosing interactively --------------------------------------------------


def test_without_flags_asks_for_the_partition_and_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.interactive = True
    system.answers = ["", ""]

    exit_code = install(make_arguments(partition_uuid=None, library_folder=None), system.build())

    assert exit_code == EXIT_OK
    assert system.prompts == ["Which partition holds the shared library? [2]: ", "Proceed? [Y/n]: "]
    assert system.enabled == [EXPECTED_UNITS[1:]]
    output = capsys.readouterr().out
    assert "  Partition      /dev/nvme0n1p4  (SteamLib, 96 GB, UUID 1111AAAA2222BBBB)" in output
    assert "  Library        /mnt/steamlib/SteamLibrary" in output


def test_declining_the_confirmation_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.interactive = True
    system.answers = ["", "n"]

    exit_code = install(make_arguments(partition_uuid=None, library_folder=None), system.build())

    assert exit_code == EXIT_FAILURE
    assert list(system.unit_directory.iterdir()) == []
    assert system.enabled == []
    assert "nothing was changed" in capsys.readouterr().err


def test_yes_installs_the_suggested_partition_without_any_prompt(tmp_path: Path) -> None:
    system = FakeSystem(tmp_path)

    exit_code = install(
        make_arguments(partition_uuid=None, library_folder=None, yes=True), system.build()
    )

    assert exit_code == EXIT_OK
    assert system.prompts == []
    assert system.enabled == [EXPECTED_UNITS[1:]]


def test_dry_run_without_root_or_terminal_lists_partitions_and_names_the_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    exit_code = install(
        make_arguments(dry_run=True, partition_uuid=None, library_folder=None), system.build()
    )

    assert exit_code == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "contents unknown (run with sudo to inspect)" in captured.out
    assert "--partition-uuid" in captured.err
    assert "/dev/nvme0n1p4  1111AAAA2222BBBB" in captured.err


def test_a_named_folder_that_is_not_a_library_is_created_after_mounting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    exit_code = install(
        make_arguments(
            partition_uuid=None,
            library_folder="Games",
            mount_point=PurePosixPath(mount_point),
            yes=True,
        ),
        system.build(),
    )

    assert exit_code == EXIT_OK
    assert (mount_point / "Games").is_dir()
    assert system.events[-2:] == ["enable", "own Games"]
    assert f"  ✓ created    {mount_point / 'Games'}" in capsys.readouterr().out


def test_dry_run_says_the_named_folder_would_be_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)

    install(
        make_arguments(dry_run=True, partition_uuid=None, library_folder="Games", yes=True),
        system.build(),
    )

    output = capsys.readouterr().out
    assert "  · would create  /mnt/steamlib/Games  (after mounting, if missing)" in output


def test_without_root_the_hint_repeats_only_the_flags_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    install(make_arguments(partition_uuid=None, library_folder=None), system.build())

    error = capsys.readouterr().err
    assert f"sudo {LAUNCHER_PATH} install --mount-point /mnt/steamlib" in error
    assert "--partition-uuid" not in error


# --- The watch part ----------------------------------------------------------


def test_with_steam_installed_the_watch_service_is_set_up_after_the_mounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.steam_root.mkdir(parents=True)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    user_events = [event for event in system.events if event.startswith("user ")]
    assert user_events == ["user daemon-reload", "user steamshare-watch.service"]
    assert system.events.index("enable") < system.events.index(user_events[0])
    assert system.user_commands[0][:3] == ["systemctl", "--user", "--machine=player@"]
    assert f"ExecStart={system.launcher} watch" in system.watch_unit.read_text()
    assert (system.steam_root / ".cef-enable-remote-debugging").exists()
    closing = capsys.readouterr().out.split("Done. Next steps:\n")[1].splitlines()
    assert closing[-2] == (
        "  3. Check the setup, without sudo:  steamshare doctor --library "
        "/mnt/steamlib/SteamLibrary"
    )
    assert closing[-1] == "  Follow watch:  journalctl --user -u steamshare-watch.service -f"


def test_the_summary_shows_the_watch_part_before_asking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.steam_root.mkdir(parents=True)
    system.interactive = True
    system.answers = ["n"]

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert not system.watch_unit.exists()
    assert "  Watch service  for user player" in capsys.readouterr().out


def test_without_steam_the_mounts_are_installed_and_the_watch_part_says_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert system.enabled == [EXPECTED_UNITS[1:]]
    assert system.user_commands == []
    output = capsys.readouterr().out
    assert "  ! skipped    Steam has never run for player" in output
    assert f"run Steam once as player, then run `sudo {LAUNCHER_PATH} install` again" in output


def test_no_watch_installs_only_the_mounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.steam_root.mkdir(parents=True)

    exit_code = install(make_arguments(no_watch=True), system.build())

    assert exit_code == EXIT_OK
    assert system.user_commands == []
    assert not system.watch_unit.exists()
    assert "  Watch service  skipped (--no-watch was given)" in capsys.readouterr().out


def test_a_dry_run_hides_the_unit_files_and_says_how_to_see_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)
    system.steam_root.mkdir(parents=True)

    install(make_arguments(dry_run=True), system.build())

    output = capsys.readouterr().out
    assert "[Unit]" not in output
    assert "--- " not in output
    assert "Add --show-units to print the unit files.\n" in output


def test_show_units_prints_every_unit_file_in_a_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)
    system.steam_root.mkdir(parents=True)

    install(make_arguments(dry_run=True, show_units=True), system.build())

    output = capsys.readouterr().out
    for name in EXPECTED_UNITS:
        assert f"--- {system.unit_directory / name}\n" in output
    assert "--- ~/.config/systemd/user/steamshare-watch.service\n" in output
    assert "Options=uid=1000,gid=1000,umask=022,windows_names,nofail" in output
    assert "--show-units" not in output


def test_dry_run_shows_the_watch_service_running_the_launcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)
    system.steam_root.mkdir(parents=True)

    exit_code = install(make_arguments(dry_run=True, show_units=True), system.build())

    assert exit_code == EXIT_OK
    assert not system.watch_unit.parent.exists()
    assert system.user_commands == []
    output = capsys.readouterr().out
    assert f"ExecStart={system.launcher} watch --library /mnt/steamlib/SteamLibrary" in output
    assert ".venv" not in output


def test_install_has_a_show_units_flag() -> None:
    assert build_parser().parse_args(["install", "--show-units"]).show_units is True
    assert build_parser().parse_args(["install"]).show_units is False


def test_install_has_a_no_watch_flag_and_no_separate_watch_installer() -> None:
    arguments = build_parser().parse_args(["install", "--no-watch"])

    assert arguments.no_watch is True
    with pytest.raises(SystemExit):
        build_parser().parse_args(["install-watch"])


# --- Argument validation -----------------------------------------------------


def test_every_install_flag_is_optional_and_the_mount_point_has_a_default() -> None:
    arguments = build_parser().parse_args(["install"])

    assert arguments.mount_point == PurePosixPath("/mnt/steamlib")
    assert arguments.partition_uuid is None
    assert arguments.library_folder is None
    assert arguments.yes is False


@pytest.mark.parametrize("value", ["mnt/steamlib", "/", ""])
def test_mount_point_must_be_an_absolute_folder_below_root(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        absolute_path(value)


def test_mount_point_accepts_an_absolute_folder() -> None:
    assert absolute_path("/mnt/steamlib") == PurePosixPath("/mnt/steamlib")


@pytest.mark.parametrize("value", ["", ".", "..", "/SteamLibrary", "a/../b", "a//b"])
def test_library_folder_must_be_a_plain_relative_folder(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        relative_folder(value)


@pytest.mark.parametrize("value", ["SteamLibrary", "Games/Steam"])
def test_library_folder_may_be_nested(value: str) -> None:
    assert relative_folder(value) == value
