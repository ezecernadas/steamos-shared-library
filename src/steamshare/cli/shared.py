"""Argument types and constants shared by several commands.

They live here, not in one command's module, so a command never imports
another command.
"""

import argparse
from pathlib import PurePosixPath

ROOT_USER_ID = 0

# How hints tell the user to run steamshare: through the launcher at the
# repository root, from the repository folder, as the README shows.
LAUNCHER_COMMAND = "./steamshare"
SUDO_INSTALL_COMMAND = f"sudo {LAUNCHER_COMMAND} install"


def absolute_path(value: str) -> PurePosixPath:
    # systemd only accepts absolute paths in Where=, and the library path is
    # compared against absolute paths Steam reports.
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/"):
        raise argparse.ArgumentTypeError(
            f"{value!r} must be an absolute folder below /, such as /mnt/steamlib"
        )
    return path


# Path parts that would leave the folder they are relative to, or that the
# `/`-joined text could hide (an empty part comes from "a//b").
UNSAFE_PATH_PARTS = frozenset({"", ".", ".."})


def relative_folder(value: str) -> str:
    # A folder inside another one, such as the library folder inside the
    # partition: "SteamLibrary", or "Games/Steam" for a nested library.
    if value.startswith("/") or UNSAFE_PATH_PARTS.intersection(value.split("/")):
        raise argparse.ArgumentTypeError(
            f"{value!r} must be a folder inside the partition, such as SteamLibrary "
            "or Games/SteamLibrary"
        )
    return value
