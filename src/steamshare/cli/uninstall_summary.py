"""The short summary `uninstall` shows before asking to proceed.

The full plan names every unit and backup, which is too long to read
before a yes-or-no question, and a real run lists it once anyway as it
goes. So the question comes after a few aligned lines built from counts:
what goes, what stays. `--dry-run` shows every file.

Pure: it builds strings from what was found and the plan, and prints nothing.
"""

from collections.abc import Sequence
from pathlib import PurePosixPath

from steamshare.cli.run_report import aligned_fields, section_header, shorten_home
from steamshare.core.uninstall_plan import RemoveFolderIfEmpty, UninstallFacts, UninstallPlan

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


def summary_lines(facts: UninstallFacts, plan: UninstallPlan, home: PurePosixPath) -> list[str]:
    """Return the summary: a title, the Remove and Keep fields, and the dry-run hint."""
    fields = [
        *_one_label_per_block(REMOVE_LABEL, remove_values(facts, plan)),
        *_one_label_per_block(KEEP_LABEL, keep_values(plan, home)),
    ]
    return [section_header(TITLE), *aligned_fields(fields), DRY_RUN_HINT, ""]


def remove_values(facts: UninstallFacts, plan: UninstallPlan) -> list[str]:
    """Return what goes, as one comma-separated line of counts."""
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
    return [", ".join(items)]


def keep_values(plan: UninstallPlan, home: PurePosixPath) -> list[str]:
    """Return what stays, one item per line."""
    data = [] if not plan.kept_data_folders else [_data_folders(plan.kept_data_folders, home)]
    flag = [] if plan.kept_debugger_flag is None else [DEBUGGER_FLAG_ITEM]
    return [*data, PROTON_CHOICES_ITEM, *flag, REPOSITORY_ITEM]


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
