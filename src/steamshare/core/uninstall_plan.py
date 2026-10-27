"""What `uninstall` removes, in which order, and what it keeps.

Pure: it receives the unit files steamshare generated (found by their header;
see `systemd_units.is_generated_unit_file`) with their backups, and a few
facts about the user's files, and returns the plan as ordered lists of
actions, one per report section. `cli.uninstall` shows the plan, asks, and
applies it.

The order is what makes it safe:

- The watch service goes first, in the user's own service manager, so it
  stops touching Steam before anything else changes.
- Mounts are stopped before the services they need, and a mount nested
  inside another one before it: a bind mount sits inside the NTFS mount, and
  the NTFS partition cannot be unmounted while a mount inside it is active.
- A unit with an [Install] section is disabled (`disable --now` also stops
  it); one without, such as the ntfs3 loader, was never enabled and is only
  stopped.
- Files are removed only after their units are stopped, and systemd reloads
  afterwards, so it forgets the units it no longer has files for.
- The `steamshare` command link on PATH goes last; nothing runs through it.

A purge (`--purge`) adds to that, in this order:

- Right after the watch service stops, steamshare's Proton mappings are
  cleared in Steam (`core.mapping_cleanup`). Not before: a running watch
  would map the games again within minutes. And before the state file goes,
  since the state is what says which mappings are steamshare's.
- Last, once the bind mounts that showed them are stopped, the ext4 Proton
  prefixes and shader caches are deleted, and Steam's debugger flag with
  them.

What is kept is part of the plan too, so the summary can say so: without a
purge, the ext4 folders the bind mounts show (Proton prefixes, with some
games' saves), and Steam's debugger flag unless the user asks to remove it;
with one, the mappings of installed games.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from steamshare.core.live_proton_mapping import OwnedGame
from steamshare.core.mapping_cleanup import MappingCleanup
from steamshare.core.mounts import NTFS3_FILESYSTEM
from steamshare.core.systemd_units import BIND_MOUNT_TYPE, MOUNT_UNIT_SUFFIX
from steamshare.core.unit_file_settings import has_section, read_path_setting, read_setting

# Sections and settings read back from the generated units.
MOUNT_SECTION = "Mount"
INSTALL_SECTION = "Install"
WHERE_SETTING = "Where"
WHAT_SETTING = "What"
TYPE_SETTING = "Type"
WANTED_BY_SETTING = "WantedBy"

# `systemctl enable` links a unit into `<target>.wants/`, next to the unit
# file, for each target its WantedBy= names (`man systemctl`, "enable").
WANTS_FOLDER_SUFFIX = ".wants"


class ServiceManager(Enum):
    """Which systemd instance a unit belongs to."""

    # The system manager, for units in /etc/systemd/system.
    SYSTEM = "system"
    # The desktop user's own manager, for units in ~/.config/systemd/user.
    USER = "user"


@dataclass(frozen=True)
class InstalledUnit:
    """A unit file steamshare generated, as found on disk."""

    path: PurePosixPath
    content: str
    # Copies install made before replacing it (`<name>.bak-<timestamp>`).
    backups: tuple[PurePosixPath, ...] = ()

    @property
    def name(self) -> str:
        return self.path.name


# --- Actions -----------------------------------------------------------------


@dataclass(frozen=True)
class UncheckedMappings:
    """A dry run's purge, when Steam could not be asked which mappings are still there."""

    managed_count: int


@dataclass(frozen=True)
class DisableUnit:
    """Stop the unit now and unlink it from boot (`systemctl disable --now`)."""

    manager: ServiceManager
    unit_name: str


@dataclass(frozen=True)
class StopUnit:
    """Stop a unit that was never enabled (`systemctl stop`)."""

    manager: ServiceManager
    unit_name: str


@dataclass(frozen=True)
class RemoveFile:
    path: PurePosixPath


@dataclass(frozen=True)
class ReloadManager:
    """Make the manager reread its unit files (`systemctl daemon-reload`)."""

    manager: ServiceManager


@dataclass(frozen=True)
class RemoveFolderIfEmpty:
    """Remove a folder steamshare made, only when nothing is left in it."""

    path: PurePosixPath


@dataclass(frozen=True)
class RemoveTree:
    """Delete a folder and everything in it: only for a purge's ext4 data."""

    path: PurePosixPath
    # Its size on disk when measured, for the report.
    size_bytes: int


@dataclass(frozen=True)
class ClearProtonMappings:
    """Clear, in the running Steam, the mappings steamshare made for games not installed."""

    # Unchecked only in a dry run that could not reach Steam: which games
    # would be cleared is then unknown. A real run checks Steam before it
    # changes anything.
    mappings: MappingCleanup | UncheckedMappings


UninstallAction = (
    DisableUnit
    | StopUnit
    | RemoveFile
    | ReloadManager
    | RemoveFolderIfEmpty
    | RemoveTree
    | ClearProtonMappings
)


# --- Plan --------------------------------------------------------------------


@dataclass(frozen=True)
class FolderSize:
    path: PurePosixPath
    size_bytes: int


@dataclass(frozen=True)
class UninstallFacts:
    """What `uninstall` found on the machine."""

    # Generated units in /etc/systemd/system, in any order.
    system_units: tuple[InstalledUnit, ...]
    # The user's steamshare-watch.service, when it is there and generated.
    watch_unit: InstalledUnit | None
    # steamshare's state file, when it exists.
    state_file: PurePosixPath | None
    # Steam's debugger flag file, when it exists.
    debugger_flag: PurePosixPath | None
    remove_debugger_flag: bool
    # The rest is only for a purge.
    purge: bool = False
    # The bind mounts' ext4 folders that exist, with their sizes.
    data_folders_on_disk: tuple[FolderSize, ...] = ()
    # How many Proton prefixes (one per game) those folders hold.
    proton_prefix_count: int = 0
    # What Steam said about steamshare's mappings; None when Steam is not
    # asked (no purge, or --keep-steam-settings).
    mappings: MappingCleanup | UncheckedMappings | None = None
    # The `steamshare` links install put on PATH, found by what they point at.
    command_links: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True)
class UninstallPlan:
    # For the "Watch service" section: the user's unit, state, and Steam flag.
    watch_actions: tuple[UninstallAction, ...]
    # For the "Mounts" section: the system units and the mount point.
    mount_actions: tuple[UninstallAction, ...]
    # For a purge's last section: the ext4 data and Steam's debugger flag.
    purge_actions: tuple[UninstallAction, ...]
    # For the "Command" section: the `steamshare` links on PATH.
    command_actions: tuple[UninstallAction, ...]
    # The ext4 folders the bind mounts showed in the library.
    kept_data_folders: tuple[PurePosixPath, ...]
    kept_debugger_flag: PurePosixPath | None
    removed_debugger_flag: PurePosixPath | None
    # Purge only: steamshare's mappings of installed games, which stay.
    kept_installed_mappings: tuple[OwnedGame, ...]


def nothing_installed(facts: UninstallFacts) -> bool:
    """Tell whether nothing install made is left, so there is nothing to uninstall.

    The state file and the debugger flag alone do not count: steamshare's
    other commands make them without installing anything. The command link
    does: only install makes it.
    """
    return not facts.system_units and facts.watch_unit is None and not facts.command_links


def build_uninstall_plan(facts: UninstallFacts) -> UninstallPlan:
    removes_flag = facts.remove_debugger_flag and facts.debugger_flag is not None
    flag_removal = [RemoveFile(facts.debugger_flag)] if removes_flag else []
    return UninstallPlan(
        watch_actions=(
            *_watch_service_actions(facts.watch_unit),
            *_mapping_actions(facts.mappings),
            *_state_file_actions(facts.state_file),
            # A purge deletes the flag last, with the rest of its data.
            *([] if facts.purge else flag_removal),
        ),
        mount_actions=_system_unit_actions(facts.system_units),
        purge_actions=(*_data_actions(facts), *flag_removal) if facts.purge else (),
        command_actions=tuple(RemoveFile(link) for link in facts.command_links),
        kept_data_folders=() if facts.purge else bind_mount_sources(facts.system_units),
        kept_debugger_flag=None if removes_flag else facts.debugger_flag,
        removed_debugger_flag=facts.debugger_flag if removes_flag else None,
        kept_installed_mappings=_kept_installed(facts.mappings),
    )


def order_for_stopping(units: Iterable[InstalledUnit]) -> list[InstalledUnit]:
    """Return mounts first, the most deeply nested first, then every other unit.

    A bind mount sits inside the NTFS mount, so it must be stopped first; the
    ntfs3 loader, a service the NTFS mount requires, comes after the mounts.
    """
    return sorted(units, key=_stopping_rank)


def enablement_links(unit: InstalledUnit) -> tuple[PurePosixPath, ...]:
    """Return the links `systemctl enable` made for the unit."""
    wanted_by = read_setting(unit.content, INSTALL_SECTION, WANTED_BY_SETTING) or ""
    return tuple(
        unit.path.parent / f"{target}{WANTS_FOLDER_SUFFIX}" / unit.name
        for target in wanted_by.split()
    )


def _stopping_rank(unit: InstalledUnit) -> tuple[int, int, str]:
    if not unit.name.endswith(MOUNT_UNIT_SUFFIX):
        return (1, 0, unit.name)
    # systemd refuses to load a mount without Where=, so one missing it is
    # not mounted and its place in the order does not matter.
    where = _mount_setting(unit, WHERE_SETTING) or PurePosixPath("/")
    # More path parts means nested deeper; the name breaks ties, so the
    # order is the same on every run.
    return (0, -len(where.parts), unit.name)


def _watch_service_actions(unit: InstalledUnit | None) -> list[UninstallAction]:
    if unit is None:
        return []
    return [
        *_stop_actions([unit], ServiceManager.USER),
        *_file_removals([unit]),
        ReloadManager(ServiceManager.USER),
    ]


def _mapping_actions(
    mappings: MappingCleanup | UncheckedMappings | None,
) -> list[UninstallAction]:
    has_work = (isinstance(mappings, MappingCleanup) and mappings.to_clear) or (
        isinstance(mappings, UncheckedMappings) and mappings.managed_count
    )
    return [ClearProtonMappings(mappings)] if has_work else []


def _kept_installed(mappings: MappingCleanup | UncheckedMappings | None) -> tuple[OwnedGame, ...]:
    return mappings.kept_installed if isinstance(mappings, MappingCleanup) else ()


def _data_actions(facts: UninstallFacts) -> list[UninstallAction]:
    """Delete the bind mounts' ext4 folders, then their shared parent if it is empty."""
    sources = set(bind_mount_sources(facts.system_units))
    on_disk = [folder for folder in facts.data_folders_on_disk if folder.path in sources]
    parents = sorted({folder.parent for folder in sources})
    return [
        *(RemoveTree(folder.path, folder.size_bytes) for folder in on_disk),
        *(RemoveFolderIfEmpty(parent) for parent in parents),
    ]


def _state_file_actions(state_file: PurePosixPath | None) -> list[UninstallAction]:
    if state_file is None:
        return []
    # The folder is steamshare's own; it goes too, unless something else is in it.
    return [RemoveFile(state_file), RemoveFolderIfEmpty(state_file.parent)]


def _system_unit_actions(units: Sequence[InstalledUnit]) -> tuple[UninstallAction, ...]:
    if not units:
        return ()
    ordered = order_for_stopping(units)
    return (
        *_stop_actions(ordered, ServiceManager.SYSTEM),
        *_file_removals(ordered),
        ReloadManager(ServiceManager.SYSTEM),
        # The mount point was created for the NTFS partition. Removed only if
        # empty once unmounted: anything still in it is not ours to delete.
        *(RemoveFolderIfEmpty(where) for where in _ntfs_mount_points(ordered)),
    )


def _stop_actions(units: Iterable[InstalledUnit], manager: ServiceManager) -> list[UninstallAction]:
    return [
        DisableUnit(manager, unit.name)
        if has_section(unit.content, INSTALL_SECTION)
        else StopUnit(manager, unit.name)
        for unit in units
    ]


def _file_removals(units: Iterable[InstalledUnit]) -> list[UninstallAction]:
    return [RemoveFile(path) for unit in units for path in (unit.path, *unit.backups)]


def _ntfs_mount_points(units: Iterable[InstalledUnit]) -> list[PurePosixPath]:
    return _mount_paths(units, mount_type=NTFS3_FILESYSTEM, setting=WHERE_SETTING)


def bind_mount_sources(units: Iterable[InstalledUnit]) -> tuple[PurePosixPath, ...]:
    """Return the ext4 folders the bind mounts show in the library, sorted."""
    paths = _mount_paths(units, mount_type=BIND_MOUNT_TYPE, setting=WHAT_SETTING)
    return tuple(sorted(paths))


def _mount_paths(
    units: Iterable[InstalledUnit], *, mount_type: str, setting: str
) -> list[PurePosixPath]:
    paths = []
    for unit in units:
        if read_setting(unit.content, MOUNT_SECTION, TYPE_SETTING) != mount_type:
            continue
        path = _mount_setting(unit, setting)
        if path is not None:
            paths.append(path)
    return paths


def _mount_setting(unit: InstalledUnit, setting: str) -> PurePosixPath | None:
    return read_path_setting(unit.content, MOUNT_SECTION, setting)
