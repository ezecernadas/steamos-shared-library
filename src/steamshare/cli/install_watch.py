"""The `install-watch` command: run `steamshare watch` whenever the user is logged in.

It turns on Steam's debugger (an empty flag file in the Steam root, read when
Steam starts), writes the `steamshare-watch.service` user unit rendered by
`core.systemd_units`, and enables it with `systemctl --user`. Everything
belongs to the desktop user, so it runs without sudo and refuses root: a
root-owned flag file or unit would be the wrong user's.

Security note: Steam's debugger listens on 127.0.0.1 only, so nothing outside
this machine can reach it. Any program running on this machine, though, can
use it to drive Steam as the user: read the library, change settings, start
games. Decky Loader makes the same trade-off. Deleting the flag file and
restarting Steam turns the debugger off again.

Every effect goes through `System`, so the wiring is tested without touching
the machine.
"""

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from steamshare.adapters.steam_cdp import remote_debugging_flag_path, turn_on_remote_debugging
from steamshare.adapters.steam_config import DEFAULT_STEAM_ROOT
from steamshare.adapters.systemctl import USER_SYSTEMCTL, enable_units_now, plan_enable_commands
from steamshare.adapters.unit_installer import install_unit_files
from steamshare.adapters.xdg_directories import user_config_home
from steamshare.cli.shared import (
    DONE_ACTION_LABELS,
    PLANNED_ACTION_LABELS,
    ROOT_USER_ID,
    absolute_path,
    format_outcome,
)
from steamshare.core.library_layout import STEAMAPPS_DIRECTORY
from steamshare.core.proton_mapping import DEFAULT_PROTON_TOOL
from steamshare.core.systemd_units import (
    WATCH_SERVICE,
    UnitFile,
    WatchServiceConfig,
    render_watch_service,
)

EXIT_OK = 0
EXIT_FAILURE = 1

# Where systemd looks for the user's own units, inside the user's config folder.
USER_UNIT_RELATIVE_DIRECTORY = Path("systemd") / "user"

RESTART_STEAM_NOTICE = (
    "Restart Steam once (Steam → Exit, then start it again) so it turns its debugger "
    "on. Until then, watch waits for it."
)


@dataclass(frozen=True)
class System:
    """Every effect `install-watch` has on the machine, replaceable in tests."""

    is_root: bool
    unit_directory: Path
    # The interpreter the service runs steamshare with: the one running now,
    # which has steamshare installed.
    python: PurePosixPath
    enable_units: Callable[[list[str]], None]


class InstallWatchError(Exception):
    """An expected failure, with a message that says what to do next."""


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "install-watch",
        help="run `watch` as a user service, and turn on the Steam debugger it needs",
    )
    parser.add_argument(
        "--library",
        type=absolute_path,
        required=True,
        help="the shared Steam library folder (the one that contains steamapps)",
    )
    parser.add_argument(
        "--tool",
        default=DEFAULT_PROTON_TOOL,
        help=f"the compatibility tool watch forces (default: {DEFAULT_PROTON_TOOL})",
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
        help="print the unit and planned changes without writing anything",
    )
    parser.set_defaults(run=run)


def run(arguments: argparse.Namespace) -> int:
    system = System(
        is_root=os.geteuid() == ROOT_USER_ID,
        unit_directory=user_config_home(os.environ, Path.home()) / USER_UNIT_RELATIVE_DIRECTORY,
        python=PurePosixPath(sys.executable),
        enable_units=lambda names: enable_units_now(names, systemctl=USER_SYSTEMCTL),
    )
    return install_watch(arguments, system)


def install_watch(arguments: argparse.Namespace, system: System) -> int:
    try:
        _check_can_run(arguments, system)
        unit = render_watch_service(
            WatchServiceConfig(
                python=system.python,
                library=arguments.library,
                tool=arguments.tool,
                steam_root=PurePosixPath(arguments.steam_root),
            )
        )
        if arguments.dry_run:
            preview_install_watch(arguments, unit, system)
        else:
            apply_install_watch(arguments, unit, system)
    except InstallWatchError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


def _check_can_run(arguments: argparse.Namespace, system: System) -> None:
    if system.is_root:
        raise InstallWatchError(
            "install-watch sets up a service for the desktop user who runs Steam, so it "
            "must run as that user, without sudo: "
            "`uv run python -m steamshare install-watch --library ...`."
        )
    if not (Path(arguments.library) / STEAMAPPS_DIRECTORY).is_dir():
        raise InstallWatchError(
            f"{arguments.library} has no {STEAMAPPS_DIRECTORY} folder. Mount the NTFS "
            "partition first, then pass the folder that contains steamapps with --library."
        )
    if not arguments.steam_root.is_dir():
        raise InstallWatchError(
            f"{arguments.steam_root} does not exist. Start Steam once so it creates it; or, "
            "if Steam is installed elsewhere (such as the Flatpak), pass its folder with "
            "--steam-root."
        )


def preview_install_watch(arguments: argparse.Namespace, unit: UnitFile, system: System) -> None:
    print("Dry run: nothing is written and systemctl is not run.\n")
    print(f"--- {system.unit_directory / unit.name}")
    print(unit.content)
    print("Planned changes:")
    flag_file = remote_debugging_flag_path(arguments.steam_root)
    flag_is_new = turn_on_remote_debugging(arguments.steam_root, dry_run=True)
    print(f"  {'would create' if flag_is_new else 'unchanged'}: {flag_file}")
    for outcome in install_unit_files([unit], system.unit_directory, dry_run=True):
        print(format_outcome(outcome, PLANNED_ACTION_LABELS))
    for command in plan_enable_commands([unit.name], systemctl=USER_SYSTEMCTL):
        print(f"  would run: {shlex.join(command)}")
    if flag_is_new:
        print(RESTART_STEAM_NOTICE)


def apply_install_watch(arguments: argparse.Namespace, unit: UnitFile, system: System) -> None:
    flag_file = remote_debugging_flag_path(arguments.steam_root)
    try:
        flag_is_new = turn_on_remote_debugging(arguments.steam_root, dry_run=False)
        system.unit_directory.mkdir(parents=True, exist_ok=True)
        outcomes = install_unit_files([unit], system.unit_directory)
    except OSError as error:
        raise InstallWatchError(
            f"Could not write {error.filename}: {error.strerror}. Nothing was enabled; fix "
            "the cause and run install-watch again."
        ) from error
    print(f"  {'created' if flag_is_new else 'unchanged'}: {flag_file}")
    for outcome in outcomes:
        print(format_outcome(outcome, DONE_ACTION_LABELS))

    try:
        system.enable_units([unit.name])
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise InstallWatchError(
            f"The service was written, but enabling it failed ({error}). See why with "
            f"`systemctl --user status {WATCH_SERVICE}`, then run install-watch again."
        ) from error

    print(f"Enabled and started: {unit.name}")
    if flag_is_new:
        print(RESTART_STEAM_NOTICE)
    print(f"Follow what it does with `journalctl --user -u {WATCH_SERVICE} -f`.")
