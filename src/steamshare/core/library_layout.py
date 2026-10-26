"""Where things live inside a Steam library folder.

Why this matters: a shared library mixes files both operating systems need
(the games) with files that only Linux may own. This module names those
places once, so every check and command agrees on them.

The layout below is Steam's convention, observed on the real setup during
Phase 0 (see docs/PLAN.md, "Phase 0 results").
"""

from dataclasses import dataclass
from pathlib import PurePosixPath

# Folder inside the library root where Steam keeps manifests and installs.
STEAMAPPS_DIRECTORY = "steamapps"

# Folder inside `steamapps` that holds one folder per installed app.
COMMON_DIRECTORY = "common"

# Steam describes each installed app in `steamapps/appmanifest_<appid>.acf`.
APP_MANIFEST_PATTERN = "appmanifest_*.acf"

# Folders inside `steamapps` that must never live on NTFS. Proton prefixes in
# `compatdata` contain names such as `dosdevices/c:`, which Windows treats as
# corruption. Shader caches are compiled per OS and driver, so Windows has no
# use for the Linux ones. Both are bind-mounted from ext4 instead.
LINUX_ONLY_DIRECTORIES = ("compatdata", "shadercache")

# The ext4 folder that holds the real `compatdata` and `shadercache`, which
# are bind-mounted over the library (docs/PLAN.md, "Bind-mount units").
# Relative to the user's home: hints show it as `~/...`, while systemd units
# need the absolute path, since they cannot expand `~`.
LINUX_ONLY_DATA_DIRECTORY = PurePosixPath(".local/share/steam-shared")

# Name prefixes of the Linux tools that Steam installs into the game's library,
# under `steamapps/common` (for example "Proton - Experimental" and
# "SteamLinuxRuntime_sniper"). Phase 0 found about 3,000 symlinks inside them.
# Windows tolerates them, so they are accepted for now; they should move to a
# dedicated ext4 library later.
LINUX_TOOL_FOLDER_PREFIXES = ("Proton", "SteamLinuxRuntime")


@dataclass(frozen=True)
class LibraryEntry:
    """One file, folder, or symlink found in the library."""

    relative_path: PurePosixPath  # relative to the library root
    is_symlink: bool


def is_inside_linux_tool(entry: LibraryEntry) -> bool:
    """Return True if the entry is a Linux tool folder or anything inside one."""
    parts = entry.relative_path.parts
    if parts[:2] != (STEAMAPPS_DIRECTORY, COMMON_DIRECTORY) or len(parts) < 3:
        return False
    app_folder = parts[2]
    return app_folder.startswith(LINUX_TOOL_FOLDER_PREFIXES)
