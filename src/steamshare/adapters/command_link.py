"""The `steamshare` command on PATH: a symlink to the launcher at the repository root.

`install` creates it, so after the first `sudo ./steamshare install` the
user types `steamshare` from any folder. The launcher finds the repository
from its own real location (`readlink -f`), so a symlink is all it takes.

A file there is ours only when it is a symlink to the launcher of a
steamshare checkout (`is_steamshare_link`): anything else with that name is
the user's, and is never replaced or removed. A link whose checkout is gone
cannot be told from someone else's, so it is left alone too.
"""

import os
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

from steamshare.adapters.launcher_location import LAUNCHER_FILE_NAME

COMMAND_NAME = LAUNCHER_FILE_NAME

# Where administrators put their own commands; on every distribution's PATH.
SYSTEM_COMMAND_DIRECTORY = Path("/usr/local/bin")
# The user's own commands (XDG Base Directory spec, "user-specific executable
# files"), for when the system folder cannot be written, as on SteamOS,
# whose root filesystem is read-only.
USER_COMMAND_DIRECTORY_IN_HOME = Path(".local") / "bin"

# Next to the launcher, in every steamshare checkout: the package itself.
CHECKOUT_PACKAGE_FOLDER = Path("src") / "steamshare"


class ExistingCommand(Enum):
    """What is at the link's path before install."""

    MISSING = "missing"
    # A link to this launcher: nothing to do.
    CURRENT_LINK = "current link"
    # A link to another checkout's launcher, such as a clone moved since.
    OLD_LINK = "old link"
    # The user's own file or link: left alone.
    OTHER_FILE = "other file"


def inspect_command_path(path: Path, launcher: Path) -> ExistingCommand:
    # `exists` follows links, so a dangling one only shows as a link.
    if not path.is_symlink() and not path.exists():
        return ExistingCommand.MISSING
    if not is_steamshare_link(path):
        return ExistingCommand.OTHER_FILE
    if Path(os.readlink(path)) == launcher:
        return ExistingCommand.CURRENT_LINK
    return ExistingCommand.OLD_LINK


def is_steamshare_link(path: Path) -> bool:
    """Tell whether the path is a symlink to the launcher of a steamshare checkout."""
    if not path.is_symlink():
        return False
    target = path.parent / os.readlink(path)
    return (
        target.name == LAUNCHER_FILE_NAME
        and target.is_file()
        and (target.parent / CHECKOUT_PACKAGE_FOLDER).is_dir()
    )


def link_command(path: Path, launcher: Path) -> None:
    """Point `path` at the launcher, replacing an old link in one step."""
    # Made under a temporary name and renamed over the old link, so the
    # command never goes missing halfway (see `safe_writes`).
    temporary = path.with_name(f".{path.name}.steamshare-tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(launcher)
    os.replace(temporary, path)


def find_command_links(directories: Iterable[Path]) -> list[Path]:
    """Return the steamshare links in the folders, for `uninstall`."""
    paths = (directory / COMMAND_NAME for directory in directories)
    return [path for path in paths if is_steamshare_link(path)]


def is_on_read_only_filesystem(directory: Path) -> bool:
    """Tell whether the folder's filesystem is mounted read-only, as SteamOS's root is.

    The mount is checked, not the permissions: a dry run without root must
    choose the same folder the real run with root does.
    """
    return bool(os.statvfs(directory).f_flag & os.ST_RDONLY)
