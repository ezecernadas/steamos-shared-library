"""Tell whether the Steam client is running, by reading /proc.

Why this matters: Steam keeps `config.vdf` in memory and writes it back when
it exits, so an edit made while it runs is silently lost (docs/PLAN.md,
"Known risks"). Commands that edit Steam files call this first and refuse.

The rule: a process is the Steam client when its name (`/proc/<pid>/comm`)
is exactly `steam`, or its executable (`/proc/<pid>/exe`) is the client
binary under the Steam root. Matching the whole name, not a substring of the
command line, is what keeps `python -m steamshare ...`, `steamwebhelper`, or
an editor with `steam` in a file name from counting as Steam. The name check
also covers the `steam` launcher script and the Flatpak, whose binary paths
differ; the binary check covers a client whose process was renamed.
"""

import os
from pathlib import Path

PROC_DIRECTORY = Path("/proc")

# The process name the Steam client and its launcher script run under.
STEAM_PROCESS_NAME = "steam"

# The client binary, relative to the Steam root.
STEAM_CLIENT_BINARY = Path("ubuntu12_32") / "steam"


def is_steam_running(steam_root: Path, proc: Path = PROC_DIRECTORY) -> bool:
    """`proc` is a parameter so tests can pass a fake /proc folder."""
    # /proc/<pid>/exe holds the resolved path, so the root's symlinks
    # (~/.steam/steam) are resolved before comparing.
    client_binary = (steam_root / STEAM_CLIENT_BINARY).resolve()
    return any(
        _process_name(process) == STEAM_PROCESS_NAME
        or _process_executable(process) == client_binary
        for process in _process_folders(proc)
    )


def _process_folders(proc: Path) -> list[Path]:
    # Only the numbered folders are processes; /proc also holds `self`,
    # `cpuinfo`, and so on.
    return [entry for entry in proc.iterdir() if entry.name.isdigit()]


# A process can exit between listing /proc and reading its files, and the
# `exe` link of another user's process is unreadable. Either way there is
# nothing to match, so the process is skipped.


def _process_name(process: Path) -> str | None:
    try:
        return (process / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _process_executable(process: Path) -> Path | None:
    try:
        return Path(os.readlink(process / "exe"))
    except OSError:
        return None
