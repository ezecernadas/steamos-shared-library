"""Read and rewrite Steam's `config.vdf`.

Steam owns this file and rewrites it when it exits, so the caller must make
sure Steam is closed first (see `steam_process`). A rewrite is backed up and
atomic (see `safe_writes`), and keeps the file's permissions.
"""

from datetime import datetime
from pathlib import Path

from steamshare.adapters.safe_writes import back_up_file, write_file_atomically
from steamshare.core.vdf import KeyValues, parse_vdf, serialize_vdf

# Relative to the user's home: a symlink to the real install on both
# Debian/Ubuntu (~/.steam/debian-installation) and SteamOS/Arch
# (~/.local/share/Steam). The Flatpak lives in
# ~/.var/app/com.valvesoftware.Steam/.local/share/Steam and needs --steam-root.
STEAM_ROOT_IN_HOME = Path(".steam") / "steam"


def default_steam_root(home: Path) -> Path:
    """Return where Steam lives for the user whose home this is.

    `install` runs as root, so it passes the desktop user's home, not its own.
    """
    return home / STEAM_ROOT_IN_HOME


# For the commands the desktop user runs directly, without sudo.
DEFAULT_STEAM_ROOT = default_steam_root(Path.home())

# Relative to the Steam root, the same on every install type (Debian package,
# SteamOS, Flatpak).
CONFIG_RELATIVE_PATH = Path("config") / "config.vdf"

# The permission bits of a file's mode, without the file type.
PERMISSION_BITS = 0o7777


class SteamConfigError(Exception):
    """`config.vdf` could not be read; the message names the file."""


def steam_config_path(steam_root: Path) -> Path:
    return steam_root / CONFIG_RELATIVE_PATH


def read_steam_config(path: Path) -> KeyValues:
    try:
        return parse_vdf(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SteamConfigError(f"{path}: {error}") from error


def write_steam_config(path: Path, config: KeyValues, now: datetime) -> Path:
    """Back up the current file, write the new config, and return the backup's path."""
    mode = path.stat().st_mode & PERMISSION_BITS
    backup = back_up_file(path, now)
    write_file_atomically(path, serialize_vdf(config), mode)
    return backup
