"""Settle which partition and library folder `install` sets up, and confirm it.

A normal user does not know partition UUIDs, so `install` lists the NTFS
partitions with what it found on each one, suggests one when the choice is
clear, and asks. Flags still win: each flag given skips its question, so
scripts keep working without a terminal.

Kept apart from `install`, which stays the wiring for the units. Listing and
probing partitions arrive through `Discovery`, and the conversation goes
through a `Console`, so tests script both.
"""

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from steamshare.adapters.console import Console
from steamshare.core.ntfs_partitions import (
    NtfsPartition,
    PartitionFacts,
    classify,
    describe,
    format_partition_list,
    format_size,
    is_recommended,
    suggest_default,
)
from steamshare.core.prompt_answers import parse_numbered_choice, parse_yes_no
from steamshare.core.systemd_units import partition_device


class InstallError(Exception):
    """An expected failure, with a message that says what to do next."""


@dataclass(frozen=True)
class Discovery:
    """How `install` finds the NTFS partitions and looks inside one."""

    list_partitions: Callable[[], list[NtfsPartition]]
    probe_partition: Callable[[NtfsPartition], PartitionFacts]


@dataclass(frozen=True)
class InstallChoices:
    partition_uuid: str
    # Relative to the partition root, such as "SteamLibrary".
    library_folder: str
    # True when the user named a folder the look inside did not find as a
    # library: install creates it once the partition is mounted.
    create_library_folder: bool
    # The chosen partition, for the summary. None when both flags were given,
    # since then no partition is listed or looked into.
    partition: NtfsPartition | None


def resolve_choices(
    arguments: argparse.Namespace, discovery: Discovery, console: Console
) -> InstallChoices:
    if arguments.partition_uuid and arguments.library_folder:
        # Everything is given: behave as before this module existed, without
        # listing or mounting anything. Scripts and the integration tests rely
        # on it.
        return InstallChoices(
            partition_uuid=arguments.partition_uuid,
            library_folder=arguments.library_folder,
            create_library_folder=False,
            partition=None,
        )
    facts = _choose_partition(arguments, discovery, console)
    if facts.partition.uuid is None:
        raise InstallError(
            f"{facts.partition.device} has no UUID, so it cannot be mounted at boot by UUID. "
            "Check it from Windows with `chkdsk`, then run install again."
        )
    library_folder, create = _choose_library_folder(arguments, facts, console)
    return InstallChoices(
        partition_uuid=facts.partition.uuid,
        library_folder=library_folder,
        create_library_folder=create,
        partition=facts.partition,
    )


def missing_partition_error(partition_uuid: str) -> InstallError:
    return InstallError(
        f"No partition at {partition_device(partition_uuid)}. Find the NTFS partition's UUID "
        "with `lsblk -f` and pass it with --partition-uuid, or leave the flag out to choose "
        "from a list."
    )


# --- Partition ---------------------------------------------------------------


def _choose_partition(
    arguments: argparse.Namespace, discovery: Discovery, console: Console
) -> PartitionFacts:
    partitions = _list_partitions(discovery)
    if arguments.partition_uuid:
        matching = [
            partition for partition in partitions if partition.uuid == arguments.partition_uuid
        ]
        if not matching:
            raise missing_partition_error(arguments.partition_uuid)
        return discovery.probe_partition(matching[0])
    if not partitions:
        raise InstallError(
            "No NTFS partition found (`lsblk -f` lists none). Create the library partition "
            "from Windows (Disk Management, formatted as NTFS), then run install again."
        )
    facts_list = [discovery.probe_partition(partition) for partition in partitions]
    console.show("NTFS partitions found:")
    for line in format_partition_list(facts_list):
        console.show(line)
    return _pick_partition(facts_list, arguments.yes, console)


def _list_partitions(discovery: Discovery) -> list[NtfsPartition]:
    try:
        return discovery.list_partitions()
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as error:
        raise InstallError(
            f"Could not list the partitions with `lsblk` ({error}). Check that `lsblk -f` "
            "works, or pass --partition-uuid and --library-folder."
        ) from error


def _pick_partition(
    facts_list: list[PartitionFacts], assume_yes: bool, console: Console
) -> PartitionFacts:
    default = suggest_default(facts_list)
    if assume_yes:
        if default is None:
            raise InstallError(
                "--yes takes the suggested partition, but none is suggested: "
                "no single partition holds a Steam library. "
                + _partition_flags_hint(facts_list, can_take_suggestion=False)
            )
        return facts_list[default]
    if not console.is_interactive:
        raise InstallError(
            "install cannot ask which partition to use, because input is not a terminal. "
            + _partition_flags_hint(facts_list, can_take_suggestion=default is not None)
        )
    while True:
        position = _ask_numbered(
            console, "Which partition holds the shared library?", len(facts_list), default
        )
        chosen = facts_list[position]
        if is_recommended(classify(chosen)):
            return chosen
        question = f"{chosen.partition.device}: {describe(chosen)}. Use it anyway?"
        if _ask_yes_no(console, question, default=False):
            return chosen


def _partition_flags_hint(
    facts_list: Sequence[PartitionFacts], *, can_take_suggestion: bool
) -> str:
    yes_hint = ", or add --yes to take the suggested one" if can_take_suggestion else ""
    uuids = "\n".join(f"  {facts.partition.device}  {facts.partition.uuid}" for facts in facts_list)
    return (
        "Pass --partition-uuid with one of these UUIDs, and --library-folder NAME "
        f"if that partition holds several Steam libraries or none{yes_hint}:\n{uuids}"
    )


# --- Library folder ----------------------------------------------------------


def _choose_library_folder(
    arguments: argparse.Namespace, facts: PartitionFacts, console: Console
) -> tuple[str, bool]:
    """Return the library folder, and whether install must create it."""
    device = facts.partition.device
    found = facts.contents.library_folders if facts.contents is not None else ()
    if arguments.library_folder:
        return arguments.library_folder, arguments.library_folder not in found
    if facts.contents is None:
        raise InstallError(
            f"install could not see which folders on {device} hold a Steam library "
            f"({facts.unknown_reason}). Run install with sudo so it can look, "
            "or pass --library-folder NAME."
        )
    if not found:
        raise InstallError(
            f"No Steam library on {device}. Either add the partition as a Steam library "
            "from Windows (Steam > Settings > Storage > Add Drive) and run install again, "
            "or pass --library-folder NAME to create that folder now, "
            "such as --library-folder SteamLibrary."
        )
    if len(found) == 1:
        console.show(f"Using the Steam library in {found[0]}/ on {device}.")
        return found[0], False
    return _pick_library_folder(found, device, arguments.yes, console), False


def _pick_library_folder(
    folders: Sequence[str], device: PurePosixPath, assume_yes: bool, console: Console
) -> str:
    console.show(f"{device} holds several Steam libraries:")
    for number, folder in enumerate(folders, start=1):
        console.show(f"  {number}) {folder}/")
    flag_hint = "Pass --library-folder with one of: " + ", ".join(folders)
    if assume_yes:
        raise InstallError(f"--yes cannot pick which library to share. {flag_hint}")
    if not console.is_interactive:
        raise InstallError(
            f"install cannot ask which library to share, because input is not a terminal. "
            f"{flag_hint}"
        )
    return folders[_ask_numbered(console, "Which library should be shared?", len(folders), None)]


# --- Confirmation ------------------------------------------------------------


def format_summary(
    choices: InstallChoices,
    mount_point: PurePosixPath,
    unit_names: Sequence[str],
) -> list[str]:
    """Return the lines that sum up what install is about to set up."""
    partition = choices.partition
    if partition is None:
        partition_line = str(partition_device(choices.partition_uuid))
    else:
        details = ", ".join(filter(None, [partition.label, format_size(partition.size_bytes)]))
        partition_line = f"{partition.device} ({details})"
    library_line = str(mount_point / choices.library_folder)
    if choices.create_library_folder:
        library_line += " (created after mounting, if missing)"
    return [
        "Install summary:",
        f"  Partition:      {partition_line}",
        f"  UUID:           {choices.partition_uuid}",
        f"  Mount point:    {mount_point}",
        f"  Library folder: {library_line}",
        f"  Units:          {', '.join(unit_names)}",
    ]


def confirm_install(
    summary: Sequence[str], console: Console, *, assume_yes: bool, dry_run: bool
) -> None:
    """Show the summary and, when someone can answer, ask before writing."""
    for line in summary:
        console.show(line)
    # A dry run writes nothing, so there is nothing to confirm. Without a
    # terminal, every choice already came from flags, as scripts expect.
    if assume_yes or dry_run or not console.is_interactive:
        return
    if not _ask_yes_no(console, "Proceed?", default=True):
        raise InstallError("Cancelled; nothing was changed.")


# --- Asking ------------------------------------------------------------------


def _ask_numbered(console: Console, question: str, option_count: int, default: int | None) -> int:
    default_hint = f" [{default + 1}]" if default is not None else ""
    while True:
        answer = _ask(console, f"{question}{default_hint}: ")
        position = parse_numbered_choice(answer, option_count=option_count, default=default)
        if position is not None:
            return position
        console.show(f"Please answer a number from 1 to {option_count}.")


def _ask_yes_no(console: Console, question: str, *, default: bool) -> bool:
    options = "[Y/n]" if default else "[y/N]"
    while True:
        answer = parse_yes_no(_ask(console, f"{question} {options}: "), default=default)
        if answer is not None:
            return answer
        console.show("Please answer y or n.")


def _ask(console: Console, prompt: str) -> str:
    try:
        return console.ask(prompt)
    except (EOFError, KeyboardInterrupt) as error:
        # Ctrl+D, Ctrl+C, or input that ran out: an expected way to stop.
        raise InstallError(
            "Stopped at the prompt; nothing was changed. Run install again to start over."
        ) from error
