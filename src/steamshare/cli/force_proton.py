"""The `force-proton` command: make Linux Steam keep the Windows build of each game.

The decisions live in `core.proton_mapping`; this module reads the library's
manifests, `config.vdf`, and the state file, prints the plan, and writes the
result. Every effect that is not a plain file in a folder given on the command
line (the state file's location, the Steam process check, the clock) goes
through `System`, so the tests run the whole command in a temporary folder.
"""

import argparse
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from steamshare.adapters.app_manifests import ManifestError, read_library_apps
from steamshare.adapters.state_file import (
    StateFileError,
    read_state,
    state_file_path,
    write_state,
)
from steamshare.adapters.steam_config import (
    SteamConfigError,
    read_steam_config,
    steam_config_path,
    write_steam_config,
)
from steamshare.adapters.steam_process import is_steam_running
from steamshare.core.library_layout import STEAMAPPS_DIRECTORY
from steamshare.core.proton_mapping import (
    LibraryApp,
    MappingPlan,
    SteamshareState,
    allow_proton,
    mark_keep_native,
    plan_proton_mappings,
    read_compat_tool_mappings,
    with_compat_tool_mappings,
)
from steamshare.core.vdf import KeyValues

EXIT_OK = 0
EXIT_FAILURE = 1

DEFAULT_TOOL = "proton_experimental"

# A symlink to the real install on both Debian/Ubuntu
# (~/.steam/debian-installation) and SteamOS/Arch (~/.local/share/Steam).
# The Flatpak lives in ~/.var/app/com.valvesoftware.Steam/.local/share/Steam
# and needs --steam-root.
DEFAULT_STEAM_ROOT = Path.home() / ".steam" / "steam"

# The longest action label, so the appid column lines up.
ACTION_COLUMN_WIDTH = 26
APPID_COLUMN_WIDTH = 8

CLOSE_STEAM_HINT = "close Steam (Steam → Exit) and run again"

REMINDER = (
    "Reminder: run force-proton after installing games from Windows, "
    "before starting Steam on Linux."
)


@dataclass(frozen=True)
class System:
    """The effects of `force-proton` outside the given folders, replaceable in tests."""

    state_path: Path
    is_steam_running: Callable[[Path], bool]
    now: Callable[[], datetime]


class ForceProtonError(Exception):
    """An expected failure, with a message that says what to do next."""


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "force-proton",
        help="force Proton for every game in the shared library, so Linux Steam "
        "keeps the Windows build",
    )
    parser.add_argument(
        "--library",
        type=Path,
        required=True,
        help="the shared Steam library folder (the one that contains steamapps)",
    )
    parser.add_argument(
        "--tool",
        default=DEFAULT_TOOL,
        help=f"the compatibility tool to force (default: {DEFAULT_TOOL})",
    )
    parser.add_argument(
        "--keep-native",
        type=appid,
        action="extend",
        nargs="+",
        default=[],
        metavar="APPID",
        help="never force Proton for these apps, and remove the mapping steamshare made",
    )
    parser.add_argument(
        "--allow-proton",
        type=appid,
        action="extend",
        nargs="+",
        default=[],
        metavar="APPID",
        help="undo --keep-native for these apps; they are mapped again in this run",
    )
    parser.add_argument(
        "--steam-root",
        type=Path,
        default=DEFAULT_STEAM_ROOT,
        help="the Steam install folder (default: ~/.steam/steam; for the Flatpak, "
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without writing anything",
    )
    parser.set_defaults(run=run)


def appid(value: str) -> str:
    # Steam appids are positive integers; the manifests and config.vdf store
    # them as strings, so they stay strings here.
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"{value!r} is not an appid, such as 1005870")
    return value


def run(arguments: argparse.Namespace) -> int:
    system = System(
        state_path=state_file_path(os.environ, Path.home()),
        is_steam_running=is_steam_running,
        now=datetime.now,
    )
    return force_proton(arguments, system)


def force_proton(arguments: argparse.Namespace, system: System) -> int:
    try:
        _check_requested_appids(arguments)
        config_path = _check_paths(arguments)
        steam_running = system.is_steam_running(arguments.steam_root)
        # Checked before reading anything: while Steam runs, config.vdf on
        # disk may be stale, and Steam overwrites it when it exits.
        if steam_running and not arguments.dry_run:
            raise ForceProtonError(
                "Steam is running, and it rewrites config.vdf when it exits, so the "
                f"change would be lost; {CLOSE_STEAM_HINT}."
            )
        apps, config, state = _read_inputs(arguments, config_path, system)
        requested_state = allow_proton(state, arguments.allow_proton)
        requested_state = mark_keep_native(requested_state, arguments.keep_native)
        plan = plan_proton_mappings(
            apps, read_compat_tool_mappings(config), requested_state, arguments.tool
        )
        new_config = with_compat_tool_mappings(config, plan.mappings)

        print_plan(plan, dry_run=arguments.dry_run)
        print_keep_native_warnings(arguments.keep_native, apps)
        if arguments.dry_run:
            print_dry_run_footer(steam_running)
        else:
            _write_changes(config_path, config, new_config, state, plan.state, system)
    except ForceProtonError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    print(REMINDER)
    return EXIT_OK


def _check_requested_appids(arguments: argparse.Namespace) -> None:
    conflicting = sorted(set(arguments.keep_native) & set(arguments.allow_proton))
    if conflicting:
        raise ForceProtonError(
            f"{', '.join(conflicting)} passed to both --keep-native and --allow-proton. "
            "Pass each app to only one of them."
        )


def _check_paths(arguments: argparse.Namespace) -> Path:
    """Check the library and Steam root, and return the path of config.vdf."""
    if not (arguments.library / STEAMAPPS_DIRECTORY).is_dir():
        raise ForceProtonError(
            f"{arguments.library} has no {STEAMAPPS_DIRECTORY} folder. Mount the NTFS "
            "partition first, then pass the folder that contains steamapps with --library."
        )
    config_path = steam_config_path(arguments.steam_root)
    if not config_path.is_file():
        raise ForceProtonError(
            f"{config_path} does not exist. Start Steam once and log in so it creates "
            "the file, then exit Steam; or, if Steam is installed elsewhere (such as the "
            "Flatpak), pass its folder with --steam-root."
        )
    return config_path


def _read_inputs(
    arguments: argparse.Namespace, config_path: Path, system: System
) -> tuple[list[LibraryApp], KeyValues, SteamshareState]:
    try:
        apps = read_library_apps(arguments.library)
    except ManifestError as error:
        raise ForceProtonError(
            f"Could not read an app manifest ({error}). Let Steam repair it by "
            "starting the game's OS once, then run again."
        ) from error
    try:
        config = read_steam_config(config_path)
    except SteamConfigError as error:
        raise ForceProtonError(
            f"Could not parse Steam's config ({error}). Nothing was changed. Start and "
            "exit Steam once so it rewrites the file, then run again."
        ) from error
    try:
        state = read_state(system.state_path)
    except StateFileError as error:
        raise ForceProtonError(
            f"Could not read steamshare's state ({error}). Fix the JSON by hand, or move "
            "the file away to start over; mappings steamshare made earlier will then "
            "count as yours."
        ) from error
    return apps, config, state


def _write_changes(
    config_path: Path,
    old_config: KeyValues,
    new_config: KeyValues,
    old_state: SteamshareState,
    new_state: SteamshareState,
    system: System,
) -> None:
    # Nothing is written when nothing changed, so a second run leaves no new
    # backup and no new timestamps.
    if new_config == old_config and new_state == old_state:
        print("Nothing to change.")
        return
    # config.vdf goes first. If the state write then fails, the new mappings
    # just look like the user's, and steamshare never removes those: the safe
    # direction to fail in.
    try:
        if new_config != old_config:
            backup = write_steam_config(config_path, new_config, system.now())
            print(f"Updated {config_path} (backup: {backup})")
        if new_state != old_state:
            write_state(system.state_path, new_state)
            print(f"Updated {system.state_path}")
    except OSError as error:
        raise ForceProtonError(
            f"Could not write {error.filename}: {error.strerror}. Fix the cause and run "
            "again; a backup of config.vdf is next to it if it was already replaced."
        ) from error


def print_plan(plan: MappingPlan, *, dry_run: bool) -> None:
    print("Plan (dry run):" if dry_run else "Changes:")
    if not plan.decisions:
        print("  No apps in the library.")
    for decision in plan.decisions:
        print(
            f"  {decision.action.value:<{ACTION_COLUMN_WIDTH}} "
            f"{decision.app.appid:>{APPID_COLUMN_WIDTH}}  {decision.app.name}"
        )


def print_keep_native_warnings(appids: Iterable[str], apps: Iterable[LibraryApp]) -> None:
    names = {app.appid: app.name for app in apps}
    for appid_kept in appids:
        label = f"{appid_kept} {names[appid_kept]}" if appid_kept in names else appid_kept
        print(
            f"Warning: {label} is kept native. Once Linux Steam downloads its Linux build "
            "into the shared library, Windows can no longer run that game. Installing it "
            "in an ext4 library instead is the better choice."
        )


def print_dry_run_footer(steam_running: bool) -> None:
    print("Dry run: nothing was written.")
    if steam_running:
        print(f"Steam is running: {CLOSE_STEAM_HINT} without --dry-run.")
