"""The `install` command: mount the shared library at every boot.

It writes the units from `core.systemd_units` into /etc/systemd/system,
creates the ext4 folders they bind-mount, and enables the mounts. Every
effect on the machine goes through `System`, so the wiring below is tested
without root and without touching the machine.
"""

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from steamshare.adapters.invoking_user import InvokingUser, resolve_invoking_user
from steamshare.adapters.systemctl import enable_units_now, plan_enable_commands
from steamshare.adapters.unit_installer import (
    SYSTEM_UNIT_DIRECTORY,
    ChangeOwner,
    DirectoryOutcome,
    create_owned_directories,
    install_unit_files,
)
from steamshare.cli.shared import (
    DONE_ACTION_LABELS,
    PLANNED_ACTION_LABELS,
    ROOT_USER_ID,
    absolute_path,
    folder_name,
    format_outcome,
)
from steamshare.core.systemd_units import (
    InstallConfig,
    UnitFile,
    build_units,
    linux_only_data_paths,
    partition_device,
)

EXIT_OK = 0
EXIT_FAILURE = 1


# Under sudo, root's PATH usually has no `uv`, so the hint calls the
# project's virtual environment directly, from the repository root.
SUDO_COMMAND_PREFIX = "sudo .venv/bin/python -m steamshare install"


@dataclass(frozen=True)
class System:
    """Every effect `install` has on the machine, replaceable in tests."""

    is_root: bool
    invoking_user: InvokingUser
    unit_directory: Path
    partition_exists: Callable[[PurePosixPath], bool]
    enable_units: Callable[[list[str]], None]
    change_owner: ChangeOwner


class InstallError(Exception):
    """An expected failure, with a message that says what to do next."""


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "install", help="install the systemd units that mount the shared library at boot"
    )
    parser.add_argument(
        "--partition-uuid",
        required=True,
        help="UUID of the NTFS partition, as `lsblk -f` shows it",
    )
    parser.add_argument(
        "--mount-point",
        type=absolute_path,
        required=True,
        help="where to mount the partition, such as /mnt/steamlib",
    )
    parser.add_argument(
        "--library-folder",
        type=folder_name,
        required=True,
        help="the folder on the partition that contains steamapps, such as SteamLibrary",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the units and planned changes without writing anything",
    )
    parser.set_defaults(run=run)


def run(arguments: argparse.Namespace) -> int:
    system = System(
        is_root=os.geteuid() == ROOT_USER_ID,
        invoking_user=resolve_invoking_user(),
        unit_directory=SYSTEM_UNIT_DIRECTORY,
        partition_exists=lambda device: Path(device).exists(),
        enable_units=enable_units_now,
        change_owner=os.chown,
    )
    return install(arguments, system)


def install(arguments: argparse.Namespace, system: System) -> int:
    try:
        config = build_config(arguments, system)
        units = build_units(config)
        if arguments.dry_run:
            preview_install(config, units, system)
        else:
            apply_install(config, units, system)
    except InstallError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


def build_config(arguments: argparse.Namespace, system: System) -> InstallConfig:
    """Check that install can run, then gather its configuration."""
    if not arguments.dry_run and not system.is_root:
        raise InstallError(_needs_root_message(arguments))
    user = system.invoking_user
    if user.user_id == ROOT_USER_ID:
        raise InstallError(
            "The files would belong to root, so Steam could not write to them. "
            "Run install with sudo from the desktop user account that runs Steam."
        )
    device = partition_device(arguments.partition_uuid)
    # Checked up front because the mount has `nofail`: with a wrong UUID it
    # would fail silently at every boot instead of here.
    if not system.partition_exists(device):
        raise InstallError(
            f"No partition at {device}. Find the NTFS partition's UUID with `lsblk -f` "
            "and pass it with --partition-uuid."
        )
    return InstallConfig(
        partition_uuid=arguments.partition_uuid,
        mount_point=arguments.mount_point,
        library_folder=arguments.library_folder,
        user_id=user.user_id,
        group_id=user.group_id,
        home=user.home,
    )


def _needs_root_message(arguments: argparse.Namespace) -> str:
    install_arguments = shlex.join(
        [
            "--partition-uuid",
            arguments.partition_uuid,
            "--mount-point",
            str(arguments.mount_point),
            "--library-folder",
            arguments.library_folder,
        ]
    )
    return (
        f"install writes to {SYSTEM_UNIT_DIRECTORY}, so it needs root. "
        "From the repository root, run:\n"
        f"  {SUDO_COMMAND_PREFIX} {install_arguments}\n"
        "To see what it would do first, add --dry-run (no root needed)."
    )


def preview_install(config: InstallConfig, units: list[UnitFile], system: System) -> None:
    print("Dry run: nothing is written and systemctl is not run.\n")
    for unit in units:
        print(f"--- {system.unit_directory / unit.name}")
        print(unit.content)
    print("Planned changes:")
    for outcome in install_unit_files(units, system.unit_directory, dry_run=True):
        print(format_outcome(outcome, PLANNED_ACTION_LABELS))
    for outcome in _create_data_directories(config, system, dry_run=True):
        print(format_outcome(outcome, PLANNED_ACTION_LABELS))
    for command in plan_enable_commands(_names_to_enable(units)):
        print(f"  would run: {shlex.join(command)}")


def apply_install(config: InstallConfig, units: list[UnitFile], system: System) -> None:
    try:
        unit_outcomes = install_unit_files(units, system.unit_directory)
        directory_outcomes = _create_data_directories(config, system, dry_run=False)
    except OSError as error:
        raise InstallError(
            f"Could not write {error.filename}: {error.strerror}. "
            "Nothing was enabled; fix the cause and run install again."
        ) from error
    for outcome in [*unit_outcomes, *directory_outcomes]:
        print(format_outcome(outcome, DONE_ACTION_LABELS))

    names_to_enable = _names_to_enable(units)
    try:
        system.enable_units(names_to_enable)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise InstallError(
            f"The units were written, but enabling them failed ({error}). "
            f"See why with `systemctl status {' '.join(names_to_enable)}` "
            "and `journalctl -xe`, then run install again."
        ) from error

    library = config.mount_point / config.library_folder
    print(f"Enabled and started: {' '.join(names_to_enable)}")
    print(
        "Next step: check the setup as your desktop user with "
        f"`uv run python -m steamshare doctor --library {shlex.quote(str(library))}`."
    )


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
