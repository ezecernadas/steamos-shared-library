"""Tests for the short summary `uninstall` shows before asking to proceed."""

from pathlib import PurePosixPath

from steamshare.cli.uninstall_summary import keep_values, remove_values, summary_lines
from steamshare.core.uninstall_plan import InstalledUnit, UninstallFacts, build_uninstall_plan

HOME = PurePosixPath("/home/player")
UNITS = PurePosixPath("/etc/systemd/system")
DATA = HOME / ".local/share/steam-shared"
STATE_FILE = HOME / ".config/steamshare/state.json"
FLAG = HOME / ".steam/steam/.cef-enable-remote-debugging"


def bind_mount(name: str, source: PurePosixPath) -> InstalledUnit:
    content = f"[Mount]\nWhat={source}\nWhere=/mnt/steamlib/{name}\nType=none\n[Install]\n"
    return InstalledUnit(
        path=UNITS / f"{name}.mount",
        content=content,
        backups=(UNITS / f"{name}.mount.bak-1", UNITS / f"{name}.mount.bak-2"),
    )


def ntfs_mount() -> InstalledUnit:
    content = "[Mount]\nWhat=/dev/x\nWhere=/mnt/steamlib\nType=ntfs3\n[Install]\n"
    return InstalledUnit(path=UNITS / "mnt-steamlib.mount", content=content)


WATCH = InstalledUnit(
    path=HOME / ".config/systemd/user/steamshare-watch.service",
    content="[Install]\nWantedBy=default.target\n",
    backups=(HOME / ".config/systemd/user/steamshare-watch.service.bak-1",),
)


def facts(
    *,
    system: tuple[InstalledUnit, ...] = (),
    watch: InstalledUnit | None = None,
    state_file: PurePosixPath | None = None,
    flag: PurePosixPath | None = None,
    remove_flag: bool = False,
) -> UninstallFacts:
    return UninstallFacts(system, watch, state_file, flag, remove_flag)


def test_everything_installed_is_counted_in_one_line() -> None:
    found = facts(
        system=(bind_mount("compatdata", DATA / "compatdata"), ntfs_mount()),
        watch=WATCH,
        state_file=STATE_FILE,
    )

    assert remove_values(found, build_uninstall_plan(found)) == [
        "watch service, 2 systemd units, 3 backups, steamshare state, "
        "the mount point folder (if empty)"
    ]


def test_counts_of_one_are_singular_and_missing_parts_are_left_out() -> None:
    found = facts(system=(ntfs_mount(),))
    found_with_backup = facts(watch=WATCH)

    assert remove_values(found, build_uninstall_plan(found)) == [
        "1 systemd unit, the mount point folder (if empty)"
    ]
    assert remove_values(found_with_backup, build_uninstall_plan(found_with_backup)) == [
        "watch service, 1 backup"
    ]


def test_the_debugger_flag_is_on_the_remove_side_only_when_asked() -> None:
    kept = facts(watch=WATCH, flag=FLAG)
    removed = facts(watch=WATCH, flag=FLAG, remove_flag=True)

    assert "Steam debugger flag" in keep_values(build_uninstall_plan(kept), HOME)
    assert "Steam debugger flag" not in keep_values(build_uninstall_plan(removed), HOME)
    assert remove_values(removed, build_uninstall_plan(removed)) == [
        "watch service, 1 backup, Steam debugger flag"
    ]


def test_the_data_folders_are_named_by_the_folder_they_share() -> None:
    found = facts(
        system=(
            bind_mount("compatdata", DATA / "compatdata"),
            bind_mount("shadercache", DATA / "shadercache"),
        )
    )

    assert keep_values(build_uninstall_plan(found), HOME) == [
        "Proton prefixes and shader caches (~/.local/share/steam-shared)",
        "the Proton choices watch made in Steam",
        "this repository and the files on the NTFS partition",
    ]


def test_summary_lines_align_the_values_and_end_with_the_dry_run_hint() -> None:
    found = facts(system=(bind_mount("compatdata", DATA / "compatdata"),), flag=FLAG)

    assert summary_lines(found, build_uninstall_plan(found), HOME) == [
        "Uninstall summary",
        "  Remove  1 systemd unit, 2 backups",
        "  Keep    Proton prefixes and shader caches (~/.local/share/steam-shared)",
        "          the Proton choices watch made in Steam",
        "          Steam debugger flag",
        "          this repository and the files on the NTFS partition",
        "Add --dry-run to see every file.",
        "",
    ]
