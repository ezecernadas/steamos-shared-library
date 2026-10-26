"""The `install` command: everything the shared library needs, in one run with sudo.

It settles the partition and library folder (asking, unless flags give them;
see `install_choices`), writes the units from `core.systemd_units` into
/etc/systemd/system, creates the ext4 folders they bind-mount, and enables
the mounts. Then, unless `--no-watch` is given, it sets up the `watch` user
service for the desktop user who ran sudo (see `watch_service_setup`).

The summary shown before asking to proceed covers both parts. What happens
is then reported in one section per part, laid out by `run_report`, and the
report ends with the backups and the numbered next steps. Every effect on
the machine goes through `System`, so the wiring below is tested without
root and without touching the machine.
"""

import argparse
import functools
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from steamshare.adapters.block_devices import read_ntfs_partitions
from steamshare.adapters.console import Console, terminal_console
from steamshare.adapters.invoking_user import InvokingUser, resolve_invoking_user
from steamshare.adapters.launcher_location import LAUNCHER_PATH
from steamshare.adapters.partition_inspector import PartitionCleanupError, probe_partition
from steamshare.adapters.safe_writes import ChangeOwner
from steamshare.adapters.systemctl import (
    CommandRunner,
    enable_units_now,
    plan_enable_commands,
    run_command,
)
from steamshare.adapters.unit_installer import (
    SYSTEM_UNIT_DIRECTORY,
    DirectoryOutcome,
    FileAction,
    create_owned_directories,
    install_unit_files,
)
from steamshare.cli.install_choices import (
    Discovery,
    InstallChoices,
    InstallError,
    confirm_install,
    missing_partition_error,
    resolve_choices,
    summary_fields,
)
from steamshare.cli.run_report import (
    PLANNED_FILE_WORDS,
    STARTED_WORD,
    WOULD_RUN_WORD,
    ClosingNotes,
    RunReport,
    Symbols,
    aligned_fields,
    choose_symbols,
    combine_notes,
    format_closing,
    section_header,
)
from steamshare.cli.shared import (
    LAUNCHER_COMMAND,
    ROOT_USER_ID,
    SUDO_INSTALL_COMMAND,
    absolute_path,
    relative_folder,
)
from steamshare.cli.watch_service_setup import (
    NO_WATCH_FLAG,
    apply_watch_service,
    plan_watch_service,
    preview_watch_service,
    watch_summary_field,
)
from steamshare.core.proton_mapping import DEFAULT_PROTON_TOOL
from steamshare.core.systemd_units import (
    DEFAULT_MOUNT_POINT,
    InstallConfig,
    UnitFile,
    build_units,
    linux_only_data_paths,
    partition_device,
)

EXIT_OK = 0
EXIT_FAILURE = 1

DRY_RUN_HEADER = "Dry run: nothing is written and systemctl is not run."
MOUNTS_SECTION_TITLE = "Mounts"


@dataclass(frozen=True)
class System:
    """Every effect `install` has on the machine, replaceable in tests."""

    is_root: bool
    invoking_user: InvokingUser
    unit_directory: Path
    partition_exists: Callable[[PurePosixPath], bool]
    enable_units: Callable[[list[str]], None]
    change_owner: ChangeOwner
    discovery: Discovery
    console: Console
    # The script the watch service runs steamshare through.
    launcher: Path
    # Runs the systemctl commands for the user's own service manager.
    run_command: CommandRunner
    # The report's markers and arrow: Unicode, or ASCII where it cannot show.
    symbols: Symbols


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "install",
        help="mount the shared library at boot and set up the watch service (run with sudo)",
        description="Without flags, install lists the NTFS partitions and asks which one "
        "holds the library. Each flag given skips its question. It also sets up the watch "
        "user service for the user who ran sudo, unless --no-watch is given. Running it "
        "again changes only what is out of date.",
    )
    parser.add_argument(
        "--partition-uuid",
        help="UUID of the NTFS partition, as `lsblk -f` shows it (asked if left out)",
    )
    parser.add_argument(
        "--mount-point",
        type=absolute_path,
        default=DEFAULT_MOUNT_POINT,
        help=f"where to mount the partition (default: {DEFAULT_MOUNT_POINT})",
    )
    parser.add_argument(
        "--library-folder",
        type=relative_folder,
        help="the folder on the partition that contains steamapps, such as SteamLibrary; "
        "created after mounting if it is missing (found on the partition if left out)",
    )
    parser.add_argument(
        NO_WATCH_FLAG,
        dest="no_watch",
        action="store_true",
        help="install only the mounts, without the watch service that forces Proton live",
    )
    parser.add_argument(
        "--steam-root",
        type=Path,
        help="the Steam install folder, for the watch service (default: ~/.steam/steam of "
        "the user who ran sudo; for the Flatpak, "
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam)",
    )
    parser.add_argument(
        "--tool",
        default=DEFAULT_PROTON_TOOL,
        help=f"the compatibility tool the watch service forces (default: {DEFAULT_PROTON_TOOL})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="ask nothing: take the suggested choices and do not ask for confirmation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the units and planned changes without writing anything",
    )
    parser.set_defaults(run=run)


def run(arguments: argparse.Namespace) -> int:
    is_root = os.geteuid() == ROOT_USER_ID
    system = System(
        is_root=is_root,
        invoking_user=resolve_invoking_user(),
        unit_directory=SYSTEM_UNIT_DIRECTORY,
        partition_exists=lambda device: Path(device).exists(),
        enable_units=enable_units_now,
        change_owner=os.chown,
        discovery=Discovery(
            list_partitions=read_ntfs_partitions,
            # Mounting a partition to look inside needs root.
            probe_partition=functools.partial(probe_partition, can_mount=is_root),
        ),
        console=terminal_console(),
        launcher=LAUNCHER_PATH,
        run_command=run_command,
        symbols=choose_symbols(sys.stdout.encoding, os.environ),
    )
    return install(arguments, system)


def install(arguments: argparse.Namespace, system: System) -> int:
    report = RunReport(
        symbols=system.symbols, home=system.invoking_user.home, dry_run=arguments.dry_run
    )
    try:
        config, choices = build_config(arguments, system)
        units = build_units(config)
        watch = plan_watch_service(
            skip=arguments.no_watch,
            user=system.invoking_user,
            library=_library_path(config),
            tool=arguments.tool,
            steam_root=arguments.steam_root,
            launcher=system.launcher,
        )
        summary_lines = aligned_fields(
            [*summary_fields(choices, config.mount_point), watch_summary_field(watch)]
        )
        confirm_install(
            [section_header("Summary"), *summary_lines, ""],
            system.console,
            assume_yes=arguments.yes,
            dry_run=arguments.dry_run,
        )
        if arguments.dry_run:
            mounts_notes = preview_install(config, choices, units, system, report)
            watch_notes = preview_watch_service(watch, report)
        else:
            # One timestamp for the whole run, so every backup it makes shares
            # one suffix and the report can name it once.
            now = datetime.now()
            mounts_notes = apply_install(config, choices, units, system, report, now)
            watch_notes = apply_watch_service(
                watch,
                change_owner=system.change_owner,
                run_command=system.run_command,
                report=report,
                now=now,
            )
        notes = combine_notes(mounts_notes, watch_notes, _check_setup_notes(config))
        for line in format_closing(notes, dry_run=arguments.dry_run):
            print(line)
    except (InstallError, PartitionCleanupError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


def build_config(
    arguments: argparse.Namespace, system: System
) -> tuple[InstallConfig, InstallChoices]:
    """Check that install can run, then gather its configuration."""
    if not arguments.dry_run and not system.is_root:
        raise InstallError(_needs_root_message(arguments))
    user = system.invoking_user
    if user.user_id == ROOT_USER_ID:
        raise InstallError(
            "The files would belong to root, so Steam could not write to them. "
            "Run install with sudo from the desktop user account that runs Steam."
        )
    choices = resolve_choices(arguments, system.discovery, system.console)
    # Checked up front because the mount has `nofail`: with a wrong UUID it
    # would fail silently at every boot instead of here.
    if not system.partition_exists(partition_device(choices.partition_uuid)):
        raise missing_partition_error(choices.partition_uuid)
    config = InstallConfig(
        partition_uuid=choices.partition_uuid,
        mount_point=arguments.mount_point,
        library_folder=choices.library_folder,
        user_id=user.user_id,
        group_id=user.group_id,
        home=user.home,
    )
    return config, choices


def _needs_root_message(arguments: argparse.Namespace) -> str:
    # Repeats only the flags the user gave, so the same choices stay open.
    given_flags = [
        ("--partition-uuid", arguments.partition_uuid),
        ("--mount-point", arguments.mount_point),
        ("--library-folder", arguments.library_folder),
        ("--steam-root", arguments.steam_root),
        ("--tool", None if arguments.tool == DEFAULT_PROTON_TOOL else arguments.tool),
    ]
    given_switches = [(NO_WATCH_FLAG, arguments.no_watch), ("--yes", arguments.yes)]
    install_arguments = shlex.join(
        [part for flag, value in given_flags if value is not None for part in (flag, str(value))]
        + [switch for switch, given in given_switches if given]
    )
    return (
        f"install writes to {SYSTEM_UNIT_DIRECTORY}, so it needs root. "
        "From the repository root, run:\n"
        f"  {SUDO_INSTALL_COMMAND} {install_arguments}\n"
        "To see what it would do first, add --dry-run (no root needed)."
    )


def preview_install(
    config: InstallConfig,
    choices: InstallChoices,
    units: list[UnitFile],
    system: System,
    report: RunReport,
) -> ClosingNotes:
    print(DRY_RUN_HEADER)
    print()
    for unit in units:
        print(f"--- {system.unit_directory / unit.name}")
        print(unit.content)
    print(report.section(MOUNTS_SECTION_TITLE, system.unit_directory))
    unit_outcomes = install_unit_files(units, system.unit_directory, dry_run=True)
    for outcome in unit_outcomes:
        print(report.outcome(outcome, system.unit_directory))
    for outcome in _create_data_directories(config, system, dry_run=True):
        print(report.outcome(outcome, system.unit_directory))
    for command in plan_enable_commands(_names_to_enable(units)):
        print(report.planned(WOULD_RUN_WORD, shlex.join(command)))
    if choices.create_library_folder:
        # Not checked on disk: before the first install the partition is not
        # mounted there yet, so the folder's presence cannot be seen.
        print(
            report.planned(
                PLANNED_FILE_WORDS[FileAction.CREATED],
                f"{_library_path(config)}  (after mounting, if missing)",
            )
        )
    print()
    return ClosingNotes(unit_outcomes=tuple(unit_outcomes))


def apply_install(
    config: InstallConfig,
    choices: InstallChoices,
    units: list[UnitFile],
    system: System,
    report: RunReport,
    now: datetime,
) -> ClosingNotes:
    print(report.section(MOUNTS_SECTION_TITLE, system.unit_directory))
    try:
        unit_outcomes = install_unit_files(units, system.unit_directory, now=now)
        directory_outcomes = _create_data_directories(config, system, dry_run=False)
    except OSError as error:
        raise InstallError(
            f"Could not write {error.filename}: {error.strerror}. "
            "Nothing was enabled; fix the cause and run install again."
        ) from error
    for outcome in [*unit_outcomes, *directory_outcomes]:
        print(report.outcome(outcome, system.unit_directory))

    names_to_enable = _names_to_enable(units)
    try:
        system.enable_units(names_to_enable)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise InstallError(
            f"The units were written, but enabling them failed ({error}). "
            f"See why with `systemctl status {' '.join(names_to_enable)}` "
            "and `journalctl -xe`, then run install again."
        ) from error
    # Only mount units are enabled: the ntfs3 loader is pulled in by them.
    # Their names are in the lines above.
    print(report.done(STARTED_WORD, f"{len(names_to_enable)} mounts"))

    if choices.create_library_folder:
        _create_library_folder(config, system, report)
    print()
    return ClosingNotes(unit_outcomes=tuple(unit_outcomes))


def _check_setup_notes(config: InstallConfig) -> ClosingNotes:
    # doctor reads the user's Steam files, so it runs as that user, not root.
    library = shlex.quote(str(_library_path(config)))
    return ClosingNotes(
        next_steps=(
            f"Check the setup, without sudo:  {LAUNCHER_COMMAND} doctor --library {library}",
        )
    )


def _create_library_folder(config: InstallConfig, system: System, report: RunReport) -> None:
    # Only now: before the mount is started, the path is an empty folder on
    # the Linux root filesystem, not on the partition. systemd may already
    # have created it, as the parent of the bind mounts; then it is reported
    # unchanged. On ntfs3 the owner shown comes from the mount's uid= option,
    # so the change of owner matters only on other filesystems.
    try:
        outcomes = create_owned_directories(
            [Path(_library_path(config))],
            config.user_id,
            config.group_id,
            change_owner=system.change_owner,
        )
    except OSError as error:
        raise InstallError(
            f"The mounts are enabled, but creating {error.filename} failed: {error.strerror}. "
            "Create the folder from Windows or as your user, then run install again."
        ) from error
    for outcome in outcomes:
        print(report.outcome(outcome))


def _library_path(config: InstallConfig) -> PurePosixPath:
    return config.mount_point / config.library_folder


def _create_data_directories(
    config: InstallConfig, system: System, *, dry_run: bool
) -> list[DirectoryOutcome]:
    return create_owned_directories(
        [Path(path) for path in linux_only_data_paths(config)],
        config.user_id,
        config.group_id,
        change_owner=system.change_owner,
        dry_run=dry_run,
    )


def _names_to_enable(units: list[UnitFile]) -> list[str]:
    return [unit.name for unit in units if unit.enable_at_boot]
