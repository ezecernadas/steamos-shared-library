"""Write the generated unit files and create the ext4 folders they mount.

Both run as root and touch system files and the user's home, so they follow
CONTRIBUTING.md rule 5:

- Idempotent: a unit file whose content already matches is not rewritten,
  and an existing folder is left alone.
- Backed up: a unit file that changes is first copied to
  `<name>.bak-<timestamp>` next to it. systemd ignores that file, since
  `.bak-...` is not a unit type.
- Dry run: every function reports what it would do and writes nothing.
"""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from steamshare.adapters.safe_writes import back_up_file, write_file_atomically
from steamshare.core.systemd_units import UnitFile

# Where administrators put their own units. It survives SteamOS A/B updates,
# which replace /usr but keep /etc (docs/PLAN.md, "Architecture").
SYSTEM_UNIT_DIRECTORY = Path("/etc/systemd/system")

# Readable by everyone, as systemd and `systemctl cat` expect; writable by root.
UNIT_FILE_MODE = 0o644

# Changes a path's owner; `os.chown` in production, a recorder in tests.
ChangeOwner = Callable[[Path, int, int], None]


class FileAction(Enum):
    UNCHANGED = "unchanged"
    CREATED = "created"
    UPDATED = "updated"


@dataclass(frozen=True)
class UnitFileOutcome:
    path: Path
    action: FileAction
    # Where the old content went; None when nothing was backed up.
    backup: Path | None = None


@dataclass(frozen=True)
class DirectoryOutcome:
    path: Path
    action: FileAction


# --- Unit files --------------------------------------------------------------


def install_unit_files(
    units: Iterable[UnitFile],
    directory: Path = SYSTEM_UNIT_DIRECTORY,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[UnitFileOutcome]:
    """Write each unit into `directory` unless it is already up to date."""
    now = now or datetime.now()
    outcomes = []
    for unit in units:
        path = directory / unit.name
        action = _decide_unit_file_action(path, unit.content)
        backup = None
        if not dry_run and action is not FileAction.UNCHANGED:
            if action is FileAction.UPDATED:
                backup = back_up_file(path, now)
            # Atomic, so systemd never reads a half-written unit.
            write_file_atomically(path, unit.content, UNIT_FILE_MODE)
        outcomes.append(UnitFileOutcome(path=path, action=action, backup=backup))
    return outcomes


def _decide_unit_file_action(path: Path, content: str) -> FileAction:
    if not path.exists():
        return FileAction.CREATED
    if path.read_text(encoding="utf-8") == content:
        return FileAction.UNCHANGED
    return FileAction.UPDATED


# --- Data folders ------------------------------------------------------------


def create_owned_directories(
    paths: Iterable[Path],
    user_id: int,
    group_id: int,
    *,
    change_owner: ChangeOwner = os.chown,
    dry_run: bool = False,
) -> list[DirectoryOutcome]:
    """Create each missing folder, and any missing parent, owned by the user.

    This runs as root, so without the change of owner the folders would
    belong to root and Steam, running as the user, could not write to them.
    """
    outcomes = []
    for path in paths:
        if path.is_dir():
            outcomes.append(DirectoryOutcome(path=path, action=FileAction.UNCHANGED))
            continue
        if not dry_run:
            for missing in _missing_folders_from_top(path):
                missing.mkdir()
                change_owner(missing, user_id, group_id)
        outcomes.append(DirectoryOutcome(path=path, action=FileAction.CREATED))
    return outcomes


def _missing_folders_from_top(path: Path) -> list[Path]:
    missing = [path]
    for parent in path.parents:
        if parent.exists():
            break
        missing.append(parent)
    return list(reversed(missing))
