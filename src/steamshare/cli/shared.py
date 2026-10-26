"""Argument types and output formatting shared by several commands.

They live here, not in one command's module, so a command never imports
another command.
"""

import argparse
from pathlib import PurePosixPath

from steamshare.adapters.unit_installer import DirectoryOutcome, FileAction, UnitFileOutcome

ROOT_USER_ID = 0

# How each outcome reads, before (dry run) and after it happened.
PLANNED_ACTION_LABELS = {
    FileAction.UNCHANGED: "unchanged",
    FileAction.CREATED: "would create",
    FileAction.UPDATED: "would update (old file backed up first)",
}
DONE_ACTION_LABELS = {
    FileAction.UNCHANGED: "unchanged",
    FileAction.CREATED: "created",
    FileAction.UPDATED: "updated",
}


def absolute_path(value: str) -> PurePosixPath:
    # systemd only accepts absolute paths in Where=, and the library path is
    # compared against absolute paths Steam reports.
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/"):
        raise argparse.ArgumentTypeError(
            f"{value!r} must be an absolute folder below /, such as /mnt/steamlib"
        )
    return path


def folder_name(value: str) -> str:
    if not value or "/" in value or value in (".", ".."):
        raise argparse.ArgumentTypeError(
            f"{value!r} must be a single folder name, such as SteamLibrary"
        )
    return value


def format_outcome(
    outcome: UnitFileOutcome | DirectoryOutcome, labels: dict[FileAction, str]
) -> str:
    line = f"  {labels[outcome.action]}: {outcome.path}"
    if isinstance(outcome, UnitFileOutcome) and outcome.backup is not None:
        line += f" (backup: {outcome.backup})"
    return line
