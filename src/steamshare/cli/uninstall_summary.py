"""The short summary `uninstall` shows before asking to proceed.

The full plan names every unit and backup, which is too long to read
before a yes-or-no question, and a real run lists it once anyway as it
goes. So the question comes after a few aligned lines built from counts:
what goes, what stays. `--dry-run` shows every file.

A purge adds what it deletes on top: the ext4 data with its size, and the
Proton choices it clears in Steam; and what it keeps changes with it.

Pure: it builds strings from what was found and the plan, and prints nothing.
The Kept section of a dry run uses the same wording for the Proton choices
(`kept_choices`).
"""

from collections.abc import Sequence
from pathlib import PurePosixPath

from steamshare.cli.run_report import aligned_fields, section_header, shorten_home
from steamshare.core.mapping_cleanup import MappingCleanup
from steamshare.core.ntfs_partitions import format_size
from steamshare.core.uninstall_plan import (
    RemoveFolderIfEmpty,
    UncheckedMappings,
    UninstallFacts,
    UninstallPlan,
)

TITLE = "Uninstall summary"
REMOVE_LABEL = "Remove"
KEEP_LABEL = "Keep"
DRY_RUN_HINT = "Add --dry-run to see every file."

WATCH_SERVICE_ITEM = "watch service"
STATE_ITEM = "steamshare state"
# Its path is in the dry run; here it only says the folder may go too.
MOUNT_POINT_ITEM = "the mount point folder (if empty)"
DEBUGGER_FLAG_ITEM = "Steam debugger flag"
PROTON_CHOICES_ITEM = "the Proton choices watch made in Steam"
REPOSITORY_ITEM = "this repository and the files on the NTFS partition"
DATA_FOLDERS_ITEM = "Proton prefixes and shader caches ({folders})"
PURGED_DATA_ITEM = "Proton prefixes and shader caches  {size}, {games}"
CLEARED_CHOICES_ITEM = "Proton choices of {count} not installed"
UNCHECKED_CLEARED_CHOICES_ITEM = (
    "Proton choices of the games not installed, among {count} steamshare mapped "
    "(Steam is not running, so they were not checked)"
)
# Why a purge keeps them: see `core.mapping_cleanup`.
RE_DOWNLOAD_REASON = "clearing them would re-download them as Linux builds"
KEPT_INSTALLED_CHOICES_ITEM = f"Proton choices of {{count}} ({RE_DOWNLOAD_REASON})"
UNCHECKED_KEPT_CHOICES_ITEM = f"Proton choices of installed games ({RE_DOWNLOAD_REASON})"


def summary_lines(facts: UninstallFacts, plan: UninstallPlan, home: PurePosixPath) -> list[str]:
    """Return the summary: a title, the Remove and Keep fields, and the dry-run hint."""
    fields = [
        *_one_label_per_block(REMOVE_LABEL, remove_values(facts, plan)),
        *_one_label_per_block(KEEP_LABEL, keep_values(facts, plan, home)),
    ]
    return [section_header(TITLE), *aligned_fields(fields), DRY_RUN_HINT, ""]


def remove_values(facts: UninstallFacts, plan: UninstallPlan) -> list[str]:
    """Return what goes: one comma-separated line of counts, then a purge's own lines."""
    units = len(facts.system_units)
    installed = [*facts.system_units, *([facts.watch_unit] if facts.watch_unit else [])]
    backups = sum(len(unit.backups) for unit in installed)
    items = [
        *([WATCH_SERVICE_ITEM] if facts.watch_unit is not None else []),
        *([_counted(units, "systemd unit")] if units else []),
        *([_counted(backups, "backup")] if backups else []),
        *([STATE_ITEM] if facts.state_file is not None else []),
        *([DEBUGGER_FLAG_ITEM] if plan.removed_debugger_flag is not None else []),
        *([MOUNT_POINT_ITEM] if _removes_a_mount_point(plan) else []),
    ]
    return [", ".join(items), *_purged_data(facts), *cleared_choices(facts.mappings)]


def keep_values(facts: UninstallFacts, plan: UninstallPlan, home: PurePosixPath) -> list[str]:
    """Return what stays, one item per line."""
    data = [] if not plan.kept_data_folders else [_data_folders(plan.kept_data_folders, home)]
    flag = [] if plan.kept_debugger_flag is None else [DEBUGGER_FLAG_ITEM]
    return [*data, *kept_choices(facts), *flag, REPOSITORY_ITEM]


def kept_choices(facts: UninstallFacts) -> list[str]:
    """Return the Proton choices left in Steam, as one item, or none when all go."""
    mappings = facts.mappings
    if not facts.purge or mappings is None:
        # Without a purge, or with --keep-steam-settings, Steam is not touched.
        return [PROTON_CHOICES_ITEM]
    if isinstance(mappings, UncheckedMappings):
        return [UNCHECKED_KEPT_CHOICES_ITEM]
    if mappings.kept_installed:
        count = _counted(len(mappings.kept_installed), "installed game")
        return [KEPT_INSTALLED_CHOICES_ITEM.format(count=count)]
    return []


def _purged_data(facts: UninstallFacts) -> list[str]:
    if not facts.purge or not facts.data_folders_on_disk:
        return []
    size = sum(folder.size_bytes for folder in facts.data_folders_on_disk)
    games = _games(facts.proton_prefix_count)
    return [PURGED_DATA_ITEM.format(size=format_size(size), games=games)]


def cleared_choices(mappings: MappingCleanup | UncheckedMappings | None) -> list[str]:
    """Return the Proton choices a purge clears, as one item, or none."""
    if isinstance(mappings, UncheckedMappings) and mappings.managed_count:
        return [UNCHECKED_CLEARED_CHOICES_ITEM.format(count=mappings.managed_count)]
    if isinstance(mappings, MappingCleanup) and mappings.to_clear:
        return [cleared_choices_item(len(mappings.to_clear))]
    return []


def cleared_choices_item(count: int) -> str:
    return CLEARED_CHOICES_ITEM.format(count=_games(count))


def _games(count: int) -> str:
    return _counted(count, "game")


def _one_label_per_block(label: str, values: Sequence[str]) -> list[tuple[str, str]]:
    # The label goes on the first line only; the others line up under it.
    return [(label if position == 0 else "", value) for position, value in enumerate(values)]


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _data_folders(folders: Sequence[PurePosixPath], home: PurePosixPath) -> str:
    # install puts them side by side, so their shared folder names them all.
    parents = {folder.parent for folder in folders}
    shown = parents if len(parents) == 1 else folders
    return DATA_FOLDERS_ITEM.format(
        folders=", ".join(shorten_home(path, home) for path in sorted(shown))
    )


def _removes_a_mount_point(plan: UninstallPlan) -> bool:
    return any(isinstance(action, RemoveFolderIfEmpty) for action in plan.mount_actions)
