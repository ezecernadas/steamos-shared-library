"""Tests for the short summary `uninstall` shows before asking to proceed."""

from pathlib import PurePosixPath

from steamshare.cli.uninstall_summary import keep_values, remove_values, summary_lines
from steamshare.core.live_proton_mapping import CompatToolSetting, OwnedGame
from steamshare.core.mapping_cleanup import MappingCleanup
from steamshare.core.uninstall_plan import (
    FolderSize,
    InstalledUnit,
    UncheckedMappings,
    UninstallFacts,
    build_uninstall_plan,
)

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


def test_the_command_link_is_on_the_remove_side() -> None:
    found = UninstallFacts(
        (), WATCH, None, None, False, command_links=(PurePosixPath("/usr/local/bin/steamshare"),)
    )

    assert remove_values(found, build_uninstall_plan(found)) == [
        "watch service, 1 backup, the steamshare command"
    ]


def test_the_debugger_flag_is_on_the_remove_side_only_when_asked() -> None:
    kept = facts(watch=WATCH, flag=FLAG)
    removed = facts(watch=WATCH, flag=FLAG, remove_flag=True)

    assert "Steam debugger flag" in keep_values(kept, build_uninstall_plan(kept), HOME)
    assert "Steam debugger flag" not in keep_values(removed, build_uninstall_plan(removed), HOME)
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

    assert keep_values(found, build_uninstall_plan(found), HOME) == [
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


# --- Purge -------------------------------------------------------------------


def mapped(appid: str, *, installed: bool) -> OwnedGame:
    return OwnedGame(appid, appid, installed, None, CompatToolSetting("proton_experimental", 250))


def purge_facts(mappings: MappingCleanup | UncheckedMappings | None) -> UninstallFacts:
    return UninstallFacts(
        system_units=(
            bind_mount("compatdata", DATA / "compatdata"),
            bind_mount("shadercache", DATA / "shadercache"),
        ),
        watch_unit=None,
        state_file=STATE_FILE,
        debugger_flag=FLAG,
        remove_debugger_flag=True,
        purge=True,
        data_folders_on_disk=(
            FolderSize(DATA / "compatdata", 2_000_000_000),
            FolderSize(DATA / "shadercache", 300_000_000),
        ),
        proton_prefix_count=47,
        mappings=mappings,
    )


CLEANUP = MappingCleanup(
    to_clear=tuple(mapped(str(appid), installed=False) for appid in range(129)),
    kept_installed=tuple(mapped(str(appid), installed=True) for appid in range(200, 212)),
)


def test_a_purge_summary_adds_the_data_with_its_size_and_the_choices_it_clears() -> None:
    found = purge_facts(CLEANUP)

    assert summary_lines(found, build_uninstall_plan(found), HOME) == [
        "Uninstall summary",
        "  Remove  2 systemd units, 4 backups, steamshare state, Steam debugger flag",
        "          Proton prefixes and shader caches  2 GB, 47 games",
        "          Proton choices of 129 games not installed",
        "  Keep    Proton choices of 12 installed games (clearing them would re-download "
        "them as Linux builds)",
        "          this repository and the files on the NTFS partition",
        "Add --dry-run to see every file.",
        "",
    ]


def test_a_purge_that_keeps_steam_settings_keeps_every_choice() -> None:
    found = purge_facts(None)
    plan = build_uninstall_plan(found)

    assert "Proton choices" not in " ".join(remove_values(found, plan))
    assert keep_values(found, plan, HOME)[0] == "the Proton choices watch made in Steam"


def test_without_steam_a_dry_run_summary_says_how_many_games_it_would_check() -> None:
    found = purge_facts(UncheckedMappings(managed_count=141))
    plan = build_uninstall_plan(found)

    assert remove_values(found, plan)[-1] == (
        "Proton choices of the games not installed, among 141 steamshare mapped "
        "(Steam is not running, so they were not checked)"
    )
    assert keep_values(found, plan, HOME)[0] == (
        "Proton choices of installed games (clearing them would re-download them as Linux builds)"
    )
