"""Tests for classifying NTFS partitions and listing them for the user.

The partitions mirror the real machine: the Windows C: drive, the library
partition labeled SteamLib, and the small Windows recovery partition.
"""

from pathlib import PurePosixPath

import pytest

from steamshare.core.ntfs_partitions import (
    NtfsPartition,
    PartitionContents,
    PartitionFacts,
    PartitionKind,
    classify,
    could_not_inspect,
    format_partition_list,
    format_size,
    is_recommended,
    not_inspected_without_root,
    suggest_default,
)

GIB = 1024**3
MIB = 1024**2


def make_partition(
    device: str = "/dev/nvme0n1p4",
    size_bytes: int = 103078166528,
    label: str | None = "SteamLib",
    uuid: str | None = "1111AAAA2222BBBB",
) -> NtfsPartition:
    return NtfsPartition(
        device=PurePosixPath(device),
        size_bytes=size_bytes,
        label=label,
        uuid=uuid,
        mount_point=None,
    )


def make_contents(
    *,
    windows: bool = False,
    recovery_folder: bool = False,
    libraries: tuple[str, ...] = (),
) -> PartitionContents:
    return PartitionContents(
        has_windows_system=windows,
        has_recovery_folder=recovery_folder,
        library_folders=libraries,
    )


WINDOWS = PartitionFacts(
    make_partition("/dev/nvme0n1p3", 192277381120, None, "3333CCCC4444DDDD"),
    make_contents(windows=True, recovery_folder=True),
)
LIBRARY = PartitionFacts(make_partition(), make_contents(libraries=("SteamLibrary",)))
RECOVERY = PartitionFacts(
    make_partition("/dev/nvme0n1p5", 975175680, None, "5555EEEE6666FFFF"),
    make_contents(recovery_folder=True),
)


# --- Classification ----------------------------------------------------------


def test_a_partition_with_windows_is_the_windows_system_partition() -> None:
    assert classify(WINDOWS) is PartitionKind.WINDOWS_SYSTEM


def test_windows_wins_over_a_steam_library_on_the_same_partition() -> None:
    facts = PartitionFacts(
        make_partition(), make_contents(windows=True, libraries=("Program Files (x86)/Steam",))
    )

    assert classify(facts) is PartitionKind.WINDOWS_SYSTEM


def test_a_partition_with_a_library_folder_is_a_steam_library() -> None:
    assert classify(LIBRARY) is PartitionKind.STEAM_LIBRARY


def test_a_library_wins_over_a_folder_named_recovery() -> None:
    facts = PartitionFacts(
        make_partition(), make_contents(recovery_folder=True, libraries=("SteamLibrary",))
    )

    assert classify(facts) is PartitionKind.STEAM_LIBRARY


def test_a_recovery_folder_marks_a_recovery_partition() -> None:
    facts = PartitionFacts(make_partition(size_bytes=50 * GIB), make_contents(recovery_folder=True))

    assert classify(facts) is PartitionKind.RECOVERY


def test_a_small_partition_is_a_recovery_partition() -> None:
    facts = PartitionFacts(make_partition(size_bytes=930 * MIB), make_contents())

    assert classify(facts) is PartitionKind.RECOVERY


def test_a_large_partition_with_nothing_known_is_other() -> None:
    facts = PartitionFacts(make_partition(size_bytes=500 * GIB), make_contents())

    assert classify(facts) is PartitionKind.OTHER


def test_a_partition_that_was_not_inspected_is_unknown() -> None:
    facts = could_not_inspect(make_partition(size_bytes=930 * MIB), "volume is dirty")

    assert classify(facts) is PartitionKind.UNKNOWN


@pytest.mark.parametrize(
    ("kind", "recommended"),
    [
        (PartitionKind.WINDOWS_SYSTEM, False),
        (PartitionKind.RECOVERY, False),
        (PartitionKind.STEAM_LIBRARY, True),
        (PartitionKind.OTHER, True),
        (PartitionKind.UNKNOWN, True),
    ],
)
def test_only_windows_own_partitions_are_not_recommended(
    kind: PartitionKind, recommended: bool
) -> None:
    assert is_recommended(kind) is recommended


# --- Default suggestion ------------------------------------------------------


def test_suggests_the_only_steam_library_partition() -> None:
    assert suggest_default([WINDOWS, LIBRARY, RECOVERY]) == 1


def test_suggests_nothing_when_two_partitions_hold_a_library() -> None:
    other_library = PartitionFacts(make_partition("/dev/sda1"), make_contents(libraries=("Games",)))

    assert suggest_default([LIBRARY, other_library]) is None


def test_suggests_nothing_when_no_partition_holds_a_library() -> None:
    assert suggest_default([WINDOWS, RECOVERY]) is None


def test_does_not_suggest_a_library_on_the_windows_partition() -> None:
    windows_with_steam = PartitionFacts(
        WINDOWS.partition, make_contents(windows=True, libraries=("Program Files (x86)/Steam",))
    )

    assert suggest_default([windows_with_steam, RECOVERY]) is None


# --- Listing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (192277381120, "179 GB"),
        (103078166528, "96 GB"),
        (975175680, "930 MB"),
        (2 * 1024**4, "2 TB"),
    ],
)
def test_sizes_read_as_windows_shows_them(size_bytes: int, expected: str) -> None:
    assert format_size(size_bytes) == expected


def test_lists_the_partitions_in_aligned_numbered_lines() -> None:
    lines = format_partition_list([WINDOWS, LIBRARY, RECOVERY])

    assert lines == [
        "  1) /dev/nvme0n1p3  179 GB            Windows system partition (not recommended)",
        "  2) /dev/nvme0n1p4   96 GB  SteamLib  Steam library in SteamLibrary/",
        "  3) /dev/nvme0n1p5  930 MB            Windows recovery partition (not recommended)",
    ]


def test_lists_every_library_folder_of_a_partition() -> None:
    facts = PartitionFacts(make_partition(), make_contents(libraries=("Games/Steam", "Steam")))

    [line] = format_partition_list([facts])

    assert line.endswith("Steam libraries in Games/Steam/, Steam/")


def test_says_when_a_partition_holds_no_library() -> None:
    facts = PartitionFacts(make_partition(size_bytes=500 * GIB), make_contents())

    [line] = format_partition_list([facts])

    assert line.endswith("no Steam library found")


def test_says_why_the_contents_of_a_partition_are_unknown() -> None:
    without_root = not_inspected_without_root(make_partition())
    failed = could_not_inspect(make_partition(), "volume is dirty")

    lines = format_partition_list([without_root, failed])

    assert lines[0].endswith("contents unknown (run with sudo to inspect)")
    assert lines[1].endswith("could not inspect: volume is dirty")
