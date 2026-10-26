"""Back up a file and replace it atomically: the two halves of a safe write.

Every adapter that edits a file on the machine (systemd units, Steam's
`config.vdf`, steamshare's own state) goes through these, so CONTRIBUTING.md
rule 5 is implemented once:

- A backup is a copy named `<name>.bak-<timestamp>` next to the original, so
  the user finds it where the file lives and can restore it with `cp`.
- The new content is written to a temporary file in the same folder and then
  renamed over the target. A rename within one filesystem is atomic, so a
  reader (systemd, Steam) never sees a half-written file, even if the write
  is interrupted.
"""

import os
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

BACKUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

# Changes a path's owner; `os.chown` in production, a recorder in tests. A
# file root writes on the user's behalf belongs to root until this runs.
ChangeOwner = Callable[[Path, int, int], None]


def back_up_file(path: Path, now: datetime) -> Path:
    """Copy the file to `<name>.bak-<timestamp>` and return the copy's path."""
    backup = path.with_name(f"{path.name}.bak-{now.strftime(BACKUP_TIMESTAMP_FORMAT)}")
    shutil.copy2(path, backup)
    return backup


def write_file_atomically(path: Path, content: str, mode: int) -> None:
    """Replace the file's content in one step, with the given permissions."""
    temporary = path.with_name(f".{path.name}.steamshare-tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)
