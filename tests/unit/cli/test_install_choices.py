"""Tests for how `install` settles the partition and the library folder.

The partitions mirror the real machine (Windows C:, the SteamLib library, the
recovery partition). A scripted console plays the user: it answers from a
list and records everything shown.
"""

import argparse
from collections.abc import Callable
from pathlib import PurePosixPath

import pytest

from steamshare.adapters.console import Console
from steamshare.cli.install_choices import (
    Discovery,
    InstallChoices,
    InstallError,
    confirm_install,
    resolve_choices,
)
from steamshare.core.ntfs_partitions import (
    NtfsPartition,
    PartitionContents,
    PartitionFacts,
    could_not_inspect,
    not_inspected_without_root,
)

WINDOWS = NtfsPartition(PurePosixPath("/dev/nvme0n1p3"), 192277381120, None, "085C", None)
LIBRARY = NtfsPartition(PurePosixPath("/dev/nvme0n1p4"), 103078166528, "SteamLib", "D8E6", None)
RECOVERY = NtfsPartition(PurePosixPath("/dev/nvme0n1p5"), 975175680, None, "98EA", None)
PARTITIONS = [WINDOWS, LIBRARY, RECOVERY]


def contents(*libraries: str, windows: bool = False) -> PartitionContents:
    return PartitionContents(
        has_windows_system=windows, has_recovery_folder=False, library_folders=libraries
    )


REAL_CONTENTS = {
    WINDOWS.uuid: contents(windows=True),
    LIBRARY.uuid: contents("SteamLibrary"),
    RECOVERY.uuid: contents(),
}


class ScriptedConsole:
    def __init__(self, answers: list[str], *, interactive: bool = True) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []
        self.interactive = interactive

    def build(self) -> Console:
        return Console(ask=self._ask, show=self.shown.append, is_interactive=self.interactive)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.shown)


def make_discovery(
    contents_by_uuid: dict[str | None, PartitionContents] = REAL_CONTENTS,
    partitions: list[NtfsPartition] = PARTITIONS,
    probe: Callable[[NtfsPartition], PartitionFacts] | None = None,
) -> Discovery:
    def probe_from_table(partition: NtfsPartition) -> PartitionFacts:
        return PartitionFacts(partition, contents_by_uuid[partition.uuid])

    return Discovery(list_partitions=lambda: partitions, probe_partition=probe or probe_from_table)


def make_arguments(
    partition_uuid: str | None = None, library_folder: str | None = None, yes: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(partition_uuid=partition_uuid, library_folder=library_folder, yes=yes)


def resolve(
    console: ScriptedConsole,
    arguments: argparse.Namespace | None = None,
    discovery: Discovery | None = None,
) -> InstallChoices:
    return resolve_choices(
        arguments or make_arguments(), discovery or make_discovery(), console.build()
    )


# --- Partition ---------------------------------------------------------------


def test_lists_the_partitions_and_enter_takes_the_suggested_one() -> None:
    console = ScriptedConsole([""])

    choices = resolve(console)

    assert choices.partition_uuid == "D8E6"
    assert choices.library_folder == "SteamLibrary"
    assert "NTFS partitions found:" in console.shown
    assert "  2) /dev/nvme0n1p4   96 GB  SteamLib  Steam library in SteamLibrary/" in console.shown
    assert console.prompts[0] == "Which partition holds the shared library? [2]: "


def test_asks_again_after_an_invalid_answer() -> None:
    console = ScriptedConsole(["9", "two", "2"])

    choices = resolve(console)

    assert choices.partition_uuid == "D8E6"
    assert len(console.prompts) == 3
    assert "Please answer a number from 1 to 3." in console.shown


def test_a_not_recommended_partition_needs_a_second_yes() -> None:
    console = ScriptedConsole(["1", "y"])

    choices = resolve(console, make_arguments(library_folder="Games"))

    assert choices.partition_uuid == "085C"
    assert "Use it anyway? [y/N]: " in console.prompts[1]


def test_declining_a_not_recommended_partition_asks_again() -> None:
    console = ScriptedConsole(["3", "", "2"])

    choices = resolve(console)

    assert choices.partition_uuid == "D8E6"


def test_without_a_suggestion_enter_is_not_an_answer() -> None:
    no_library = {**REAL_CONTENTS, LIBRARY.uuid: contents()}
    console = ScriptedConsole(["", "2"], interactive=True)

    choices = resolve(
        console, make_arguments(library_folder="SteamLibrary"), make_discovery(no_library)
    )

    assert console.prompts[0] == "Which partition holds the shared library?: "
    assert choices.partition_uuid == "D8E6"


def test_yes_takes_the_suggested_partition_without_asking() -> None:
    console = ScriptedConsole([], interactive=False)

    choices = resolve(console, make_arguments(yes=True))

    assert choices.partition_uuid == "D8E6"
    assert console.prompts == []


def test_yes_without_a_suggestion_names_the_flag() -> None:
    no_library = {**REAL_CONTENTS, LIBRARY.uuid: contents()}

    with pytest.raises(InstallError, match="--partition-uuid"):
        resolve(ScriptedConsole([]), make_arguments(yes=True), make_discovery(no_library))


def test_without_a_terminal_prints_the_list_and_names_the_flags() -> None:
    console = ScriptedConsole([], interactive=False)

    with pytest.raises(InstallError) as raised:
        resolve(console)

    message = str(raised.value)
    assert "--partition-uuid" in message
    assert "--library-folder" in message
    assert "--yes" in message
    assert "/dev/nvme0n1p4  D8E6" in message
    assert "NTFS partitions found:" in console.shown
    assert console.prompts == []


def test_a_partition_uuid_flag_skips_the_question_and_looks_only_at_that_one() -> None:
    probed: list[NtfsPartition] = []

    def probe(partition: NtfsPartition) -> PartitionFacts:
        probed.append(partition)
        return PartitionFacts(partition, contents("SteamLibrary"))

    console = ScriptedConsole([], interactive=False)

    choices = resolve(console, make_arguments(partition_uuid="D8E6"), make_discovery(probe=probe))

    assert choices.library_folder == "SteamLibrary"
    assert probed == [LIBRARY]
    assert "NTFS partitions found:" not in console.shown


def test_an_unknown_partition_uuid_says_how_to_find_the_right_one() -> None:
    with pytest.raises(InstallError, match="lsblk -f"):
        resolve(ScriptedConsole([]), make_arguments(partition_uuid="NOPE"))


def test_both_flags_skip_every_look_and_question() -> None:
    def fail(*_: object) -> list[NtfsPartition]:
        raise AssertionError("no partition may be listed or probed")

    discovery = Discovery(list_partitions=fail, probe_partition=fail)  # type: ignore[arg-type]

    choices = resolve(
        ScriptedConsole([], interactive=False),
        make_arguments(partition_uuid="D8E6", library_folder="SteamLibrary"),
        discovery,
    )

    assert choices == InstallChoices(
        partition_uuid="D8E6",
        library_folder="SteamLibrary",
        create_library_folder=False,
        partition=None,
    )


def test_no_ntfs_partition_at_all_explains_what_to_do() -> None:
    with pytest.raises(InstallError, match="No NTFS partition"):
        resolve(ScriptedConsole([]), discovery=make_discovery(partitions=[]))


def test_a_partition_without_uuid_cannot_be_mounted_at_boot() -> None:
    no_uuid = NtfsPartition(PurePosixPath("/dev/sdb1"), 500 * 1024**3, None, None, None)
    discovery = make_discovery({None: contents("SteamLibrary")}, partitions=[no_uuid])

    with pytest.raises(InstallError, match="no UUID"):
        resolve(ScriptedConsole([""]), discovery=discovery)


# --- Library folder ----------------------------------------------------------


def test_the_only_library_on_the_partition_is_used_and_announced() -> None:
    console = ScriptedConsole([""])

    choices = resolve(console)

    assert choices.create_library_folder is False
    assert "Using the Steam library in SteamLibrary/ on /dev/nvme0n1p4." in console.shown


def test_several_libraries_are_listed_and_asked_for() -> None:
    two = {**REAL_CONTENTS, LIBRARY.uuid: contents("Games/Steam", "SteamLibrary")}
    console = ScriptedConsole(["", "1"])

    choices = resolve(console, discovery=make_discovery(two))

    assert choices.library_folder == "Games/Steam"
    assert "  1) Games/Steam/" in console.shown
    assert console.prompts[1] == "Which library should be shared?: "


def test_yes_cannot_pick_between_several_libraries() -> None:
    two = {**REAL_CONTENTS, LIBRARY.uuid: contents("Games/Steam", "SteamLibrary")}

    with pytest.raises(InstallError, match="--library-folder"):
        resolve(ScriptedConsole([]), make_arguments(yes=True), make_discovery(two))


def test_no_library_on_the_chosen_partition_explains_both_ways_out() -> None:
    with pytest.raises(InstallError) as raised:
        resolve(ScriptedConsole(["1", "y"]))

    message = str(raised.value)
    assert "Add Drive" in message
    assert "--library-folder" in message


def test_a_named_folder_that_is_not_a_library_yet_is_created() -> None:
    console = ScriptedConsole(["1", "y"])

    choices = resolve(console, make_arguments(library_folder="SteamLibrary"))

    assert choices.library_folder == "SteamLibrary"
    assert choices.create_library_folder is True


def test_a_named_folder_that_is_already_a_library_is_not_created() -> None:
    choices = resolve(ScriptedConsole([""]), make_arguments(library_folder="SteamLibrary"))

    assert choices.create_library_folder is False


def test_unknown_contents_need_root_or_the_folder_flag() -> None:
    discovery = make_discovery(probe=not_inspected_without_root)

    with pytest.raises(InstallError, match="sudo"):
        resolve(ScriptedConsole(["1"]), discovery=discovery)


def test_a_failed_look_inside_is_named_in_the_error() -> None:
    discovery = make_discovery(probe=lambda partition: could_not_inspect(partition, "dirty"))

    with pytest.raises(InstallError, match="dirty"):
        resolve(ScriptedConsole(["1"]), discovery=discovery)


def test_input_ending_at_a_prompt_stops_without_a_traceback() -> None:
    with pytest.raises(InstallError, match="nothing was changed"):
        resolve(ScriptedConsole([]))


# --- Confirmation ------------------------------------------------------------

CHOICES = InstallChoices(
    partition_uuid="D8E6",
    library_folder="SteamLibrary",
    create_library_folder=False,
    partition=LIBRARY,
)
SUMMARY = ["  Partition:      /dev/nvme0n1p4 (SteamLib, 96 GB)"]


def confirm(console: ScriptedConsole, *, yes: bool = False, dry_run: bool = False) -> None:
    confirm_install(SUMMARY, console.build(), assume_yes=yes, dry_run=dry_run)


def test_shows_the_summary_and_proceeds_on_enter() -> None:
    console = ScriptedConsole([""])

    confirm(console)

    assert SUMMARY[0] in console.shown
    assert console.prompts == ["Proceed? [Y/n]: "]


def test_no_cancels_and_says_nothing_changed() -> None:
    with pytest.raises(InstallError, match="nothing was changed"):
        confirm(ScriptedConsole(["n"]))


@pytest.mark.parametrize(
    ("yes", "dry_run", "interactive"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_does_not_ask_with_yes_in_a_dry_run_or_without_a_terminal(
    yes: bool, dry_run: bool, interactive: bool
) -> None:
    console = ScriptedConsole([], interactive=interactive)

    confirm(console, yes=yes, dry_run=dry_run)

    assert console.prompts == []
    assert SUMMARY[0] in console.shown
