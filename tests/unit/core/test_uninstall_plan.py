"""Tests for what `uninstall` removes, in which order, and what it keeps.

The units are the ones `install` renders, so the plan is checked against
real generated content, with no file on disk.
"""

from pathlib import PurePosixPath

from steamshare.core.live_proton_mapping import CompatToolSetting, OwnedGame
from steamshare.core.mapping_cleanup import MappingCleanup
from steamshare.core.systemd_units import (
    InstallConfig,
    WatchServiceConfig,
    build_units,
    render_watch_service,
)
from steamshare.core.uninstall_plan import (
    ClearProtonMappings,
    DisableUnit,
    FolderSize,
    InstalledUnit,
    ReloadManager,
    RemoveFile,
    RemoveFolderIfEmpty,
    RemoveTree,
    ServiceManager,
    StopUnit,
    UncheckedMappings,
    UninstallFacts,
    build_uninstall_plan,
    enablement_links,
    nothing_installed,
    order_for_stopping,
)

SYSTEM = ServiceManager.SYSTEM
USER = ServiceManager.USER
UNIT_DIRECTORY = PurePosixPath("/etc/systemd/system")
USER_UNIT_DIRECTORY = PurePosixPath("/home/player/.config/systemd/user")
STATE_FILE = PurePosixPath("/home/player/.config/steamshare/state.json")
DEBUGGER_FLAG = PurePosixPath("/home/player/.steam/steam/.cef-enable-remote-debugging")

CONFIG = InstallConfig(
    partition_uuid="1111AAAA2222BBBB",
    mount_point=PurePosixPath("/mnt/steamlib"),
    library_folder="SteamLibrary",
    user_id=1000,
    group_id=1000,
    home=PurePosixPath("/home/player"),
)
LOADER = "steamshare-ntfs3.service"
NTFS_MOUNT = "mnt-steamlib.mount"
COMPATDATA_MOUNT = "mnt-steamlib-SteamLibrary-steamapps-compatdata.mount"
SHADERCACHE_MOUNT = "mnt-steamlib-SteamLibrary-steamapps-shadercache.mount"


def system_units(*, with_backups: bool = False) -> tuple[InstalledUnit, ...]:
    return tuple(
        InstalledUnit(
            path=UNIT_DIRECTORY / unit.name,
            content=unit.content,
            backups=(UNIT_DIRECTORY / f"{unit.name}.bak-20261026-183131",) if with_backups else (),
        )
        for unit in build_units(CONFIG)
    )


def watch_unit() -> InstalledUnit:
    unit = render_watch_service(
        WatchServiceConfig(
            launcher=PurePosixPath("/home/player/steamshare/steamshare"),
            library=PurePosixPath("/mnt/steamlib/SteamLibrary"),
            tool="proton_experimental",
            steam_root=PurePosixPath("/home/player/.steam/steam"),
        )
    )
    return InstalledUnit(path=USER_UNIT_DIRECTORY / unit.name, content=unit.content)


def facts(
    *,
    system: tuple[InstalledUnit, ...] = (),
    watch: InstalledUnit | None = None,
    state_file: PurePosixPath | None = None,
    debugger_flag: PurePosixPath | None = None,
    remove_debugger_flag: bool = False,
) -> UninstallFacts:
    return UninstallFacts(
        system_units=system,
        watch_unit=watch,
        state_file=state_file,
        debugger_flag=debugger_flag,
        remove_debugger_flag=remove_debugger_flag,
    )


# --- Nothing installed -------------------------------------------------------


def test_without_any_unit_there_is_nothing_to_uninstall() -> None:
    # The state file and the debugger flag alone are not an installation.
    assert nothing_installed(facts(state_file=STATE_FILE, debugger_flag=DEBUGGER_FLAG))


def test_either_kind_of_unit_alone_is_something_to_uninstall() -> None:
    assert not nothing_installed(facts(watch=watch_unit()))
    assert not nothing_installed(facts(system=system_units()[:1]))


# --- Stopping order ----------------------------------------------------------


def test_bind_mounts_stop_before_the_ntfs_mount_and_the_loader_stops_last() -> None:
    ordered = order_for_stopping(system_units())

    assert [unit.name for unit in ordered] == [
        COMPATDATA_MOUNT,
        SHADERCACHE_MOUNT,
        NTFS_MOUNT,
        LOADER,
    ]


def test_the_order_comes_from_the_mount_paths_not_from_the_input_order() -> None:
    ordered = order_for_stopping(tuple(reversed(system_units())))

    assert [unit.name for unit in ordered][:3] == [COMPATDATA_MOUNT, SHADERCACHE_MOUNT, NTFS_MOUNT]


# --- Mounts ------------------------------------------------------------------


def test_mounts_are_disabled_the_loader_stopped_then_files_removed_and_systemd_reloaded() -> None:
    plan = build_uninstall_plan(facts(system=system_units(with_backups=True)))

    backup = "{}.bak-20261026-183131"
    assert plan.mount_actions == (
        DisableUnit(SYSTEM, COMPATDATA_MOUNT),
        DisableUnit(SYSTEM, SHADERCACHE_MOUNT),
        DisableUnit(SYSTEM, NTFS_MOUNT),
        StopUnit(SYSTEM, LOADER),
        RemoveFile(UNIT_DIRECTORY / COMPATDATA_MOUNT),
        RemoveFile(UNIT_DIRECTORY / backup.format(COMPATDATA_MOUNT)),
        RemoveFile(UNIT_DIRECTORY / SHADERCACHE_MOUNT),
        RemoveFile(UNIT_DIRECTORY / backup.format(SHADERCACHE_MOUNT)),
        RemoveFile(UNIT_DIRECTORY / NTFS_MOUNT),
        RemoveFile(UNIT_DIRECTORY / backup.format(NTFS_MOUNT)),
        RemoveFile(UNIT_DIRECTORY / LOADER),
        RemoveFile(UNIT_DIRECTORY / backup.format(LOADER)),
        ReloadManager(SYSTEM),
        RemoveFolderIfEmpty(PurePosixPath("/mnt/steamlib")),
    )


def test_the_ext4_folders_of_the_bind_mounts_are_kept() -> None:
    plan = build_uninstall_plan(facts(system=system_units()))

    assert plan.kept_data_folders == (
        PurePosixPath("/home/player/.local/share/steam-shared/compatdata"),
        PurePosixPath("/home/player/.local/share/steam-shared/shadercache"),
    )


def test_a_unit_removed_by_hand_is_simply_not_in_the_plan() -> None:
    without_shadercache = tuple(u for u in system_units() if u.name != SHADERCACHE_MOUNT)

    plan = build_uninstall_plan(facts(system=without_shadercache))

    assert DisableUnit(SYSTEM, SHADERCACHE_MOUNT) not in plan.mount_actions
    assert plan.mount_actions[:3] == (
        DisableUnit(SYSTEM, COMPATDATA_MOUNT),
        DisableUnit(SYSTEM, NTFS_MOUNT),
        StopUnit(SYSTEM, LOADER),
    )


def test_without_system_units_the_mounts_part_is_empty() -> None:
    plan = build_uninstall_plan(facts(watch=watch_unit()))

    assert plan.mount_actions == ()
    assert plan.kept_data_folders == ()


# --- Watch service -----------------------------------------------------------


def test_the_watch_service_is_disabled_in_the_users_manager_then_removed() -> None:
    unit = watch_unit()
    backup = USER_UNIT_DIRECTORY / "steamshare-watch.service.bak-20261026-183133"
    unit = InstalledUnit(path=unit.path, content=unit.content, backups=(backup,))

    plan = build_uninstall_plan(facts(watch=unit))

    assert plan.watch_actions == (
        DisableUnit(USER, "steamshare-watch.service"),
        RemoveFile(unit.path),
        RemoveFile(backup),
        ReloadManager(USER),
    )


def test_the_state_file_and_its_folder_go_too() -> None:
    plan = build_uninstall_plan(facts(watch=watch_unit(), state_file=STATE_FILE))

    assert plan.watch_actions[-2:] == (
        RemoveFile(STATE_FILE),
        RemoveFolderIfEmpty(STATE_FILE.parent),
    )


def test_only_system_units_still_removes_the_state_file() -> None:
    plan = build_uninstall_plan(facts(system=system_units(), state_file=STATE_FILE))

    assert plan.watch_actions == (RemoveFile(STATE_FILE), RemoveFolderIfEmpty(STATE_FILE.parent))


# --- Steam's debugger flag ---------------------------------------------------


def test_the_debugger_flag_is_kept_by_default() -> None:
    plan = build_uninstall_plan(facts(watch=watch_unit(), debugger_flag=DEBUGGER_FLAG))

    assert plan.kept_debugger_flag == DEBUGGER_FLAG
    assert plan.removed_debugger_flag is None
    assert RemoveFile(DEBUGGER_FLAG) not in plan.watch_actions


def test_the_debugger_flag_is_removed_only_when_asked() -> None:
    plan = build_uninstall_plan(
        facts(watch=watch_unit(), debugger_flag=DEBUGGER_FLAG, remove_debugger_flag=True)
    )

    assert plan.watch_actions[-1] == RemoveFile(DEBUGGER_FLAG)
    assert plan.removed_debugger_flag == DEBUGGER_FLAG
    assert plan.kept_debugger_flag is None


def test_a_missing_debugger_flag_is_neither_kept_nor_removed() -> None:
    plan = build_uninstall_plan(facts(watch=watch_unit(), remove_debugger_flag=True))

    assert plan.kept_debugger_flag is None
    assert plan.removed_debugger_flag is None


# --- Enablement links --------------------------------------------------------


def test_the_enablement_link_is_in_the_wants_folder_of_each_wanted_by_target() -> None:
    assert enablement_links(watch_unit()) == (
        USER_UNIT_DIRECTORY / "default.target.wants" / "steamshare-watch.service",
    )


# --- Purge -------------------------------------------------------------------

DATA = PurePosixPath("/home/player/.local/share/steam-shared")
DATA_ON_DISK = (
    FolderSize(DATA / "compatdata", 2_000_000_000),
    FolderSize(DATA / "shadercache", 300_000_000),
)
NOT_INSTALLED = OwnedGame("1", "One", False, None, CompatToolSetting("proton_experimental", 250))
INSTALLED = OwnedGame("2", "Two", True, None, CompatToolSetting("proton_experimental", 250))
CLEANUP = MappingCleanup(to_clear=(NOT_INSTALLED,), kept_installed=(INSTALLED,))


def purge_facts(*, mappings: MappingCleanup | UncheckedMappings | None = CLEANUP) -> UninstallFacts:
    return UninstallFacts(
        system_units=system_units(),
        watch_unit=watch_unit(),
        state_file=STATE_FILE,
        debugger_flag=DEBUGGER_FLAG,
        remove_debugger_flag=True,
        purge=True,
        data_folders_on_disk=DATA_ON_DISK,
        mappings=mappings,
    )


def test_purge_stops_watch_then_clears_mappings_before_the_state_goes() -> None:
    plan = build_uninstall_plan(purge_facts())

    # watch would map the games again if it were still running, and the
    # clear needs the state file to know which mappings are steamshare's.
    assert plan.watch_actions == (
        DisableUnit(USER, "steamshare-watch.service"),
        RemoveFile(watch_unit().path),
        ReloadManager(USER),
        ClearProtonMappings(CLEANUP),
        RemoveFile(STATE_FILE),
        RemoveFolderIfEmpty(STATE_FILE.parent),
    )


def test_purge_deletes_the_data_and_the_flag_last() -> None:
    plan = build_uninstall_plan(purge_facts())

    assert plan.purge_actions == (
        RemoveTree(DATA / "compatdata", 2_000_000_000),
        RemoveTree(DATA / "shadercache", 300_000_000),
        RemoveFolderIfEmpty(DATA),
        RemoveFile(DEBUGGER_FLAG),
    )
    assert plan.kept_data_folders == ()
    assert plan.kept_debugger_flag is None
    assert plan.removed_debugger_flag == DEBUGGER_FLAG


def test_purge_keeps_the_mappings_of_installed_games() -> None:
    plan = build_uninstall_plan(purge_facts())

    assert plan.kept_installed_mappings == (INSTALLED,)


def test_mappings_not_checked_in_a_dry_run_are_planned_by_their_count() -> None:
    plan = build_uninstall_plan(purge_facts(mappings=UncheckedMappings(managed_count=141)))

    assert ClearProtonMappings(UncheckedMappings(managed_count=141)) in plan.watch_actions
    assert plan.kept_installed_mappings == ()


def test_purge_with_steam_settings_kept_or_nothing_to_clear_asks_steam_nothing() -> None:
    nothing_to_clear = MappingCleanup(to_clear=(), kept_installed=(INSTALLED,))

    for mappings in (None, nothing_to_clear, UncheckedMappings(managed_count=0)):
        plan = build_uninstall_plan(purge_facts(mappings=mappings))
        assert not any(isinstance(a, ClearProtonMappings) for a in plan.watch_actions)


def test_a_data_folder_missing_on_disk_is_not_purged() -> None:
    facts = purge_facts()
    facts = UninstallFacts(**{**facts.__dict__, "data_folders_on_disk": DATA_ON_DISK[:1]})

    plan = build_uninstall_plan(facts)

    assert plan.purge_actions[:2] == (
        RemoveTree(DATA / "compatdata", 2_000_000_000),
        RemoveFolderIfEmpty(DATA),
    )


def test_without_purge_there_are_no_purge_actions() -> None:
    plan = build_uninstall_plan(facts(system=system_units(), debugger_flag=DEBUGGER_FLAG))

    assert plan.purge_actions == ()
