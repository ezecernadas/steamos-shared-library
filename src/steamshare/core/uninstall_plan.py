"""What `uninstall` removes, in which order, and what it keeps.

Pure: it receives the unit files steamshare generated (found by their header;
see `systemd_units.is_generated_unit_file`) with their backups, and a few
facts about the user's files, and returns the plan as two ordered lists of
actions. `cli.uninstall` shows the plan, asks, and applies it.

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

What is kept is part of the plan too, so the summary can say so: the ext4
folders the bind mounts show (Proton prefixes, with some games' saves), and
Steam's debugger flag unless the user asks to remove it.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

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


UninstallAction = DisableUnit | StopUnit | RemoveFile | ReloadManager | RemoveFolderIfEmpty


# --- Plan --------------------------------------------------------------------


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


@dataclass(frozen=True)
class UninstallPlan:
    # For the "Watch service" section: the user's unit, state, and Steam flag.
    watch_actions: tuple[UninstallAction, ...]
    # For the "Mounts" section: the system units and the mount point.
    mount_actions: tuple[UninstallAction, ...]
    # The ext4 folders the bind mounts showed in the library.
    kept_data_folders: tuple[PurePosixPath, ...]
    kept_debugger_flag: PurePosixPath | None
    removed_debugger_flag: PurePosixPath | None


def nothing_installed(facts: UninstallFacts) -> bool:
    """Tell whether no steamshare unit is left, so there is nothing to uninstall.

    The state file and the debugger flag alone do not count: steamshare's
    other commands make them without installing anything.
    """
    return not facts.system_units and facts.watch_unit is None


def build_uninstall_plan(facts: UninstallFacts) -> UninstallPlan:
    removes_flag = facts.remove_debugger_flag and facts.debugger_flag is not None
    return UninstallPlan(
        watch_actions=(
            *_watch_service_actions(facts.watch_unit),
            *_state_file_actions(facts.state_file),
            *([RemoveFile(facts.debugger_flag)] if removes_flag else []),
        ),
        mount_actions=_system_unit_actions(facts.system_units),
        kept_data_folders=_bind_mount_sources(facts.system_units),
        kept_debugger_flag=None if removes_flag else facts.debugger_flag,
        removed_debugger_flag=facts.debugger_flag if removes_flag else None,
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


def _bind_mount_sources(units: Iterable[InstalledUnit]) -> tuple[PurePosixPath, ...]:
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
