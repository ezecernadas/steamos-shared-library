"""NTFS partitions, what each one holds, and which one to suggest for the library.

Why this matters: `install` needs the NTFS partition that holds the shared
library, but a normal user does not know partition UUIDs. Instead, install
lists the NTFS partitions with what it found on each one, and the user picks.

The `block_devices` adapter lists the partitions and the `partition_inspector`
adapter looks inside them. This module turns those facts into a kind, a
suggested default, and the lines of the list. It does no I/O.

To recognize a new kind of partition, add a `PartitionKind`, a rule in
`CLASSIFICATION_RULES`, and a description in `KIND_DESCRIPTIONS`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

# Present only on the partition Windows runs from (the C: drive).
WINDOWS_SYSTEM_MARKER = PurePosixPath("Windows/System32")

# Windows keeps its recovery environment (WinRE) in this root folder: on the
# recovery partition, and sometimes on C: too.
RECOVERY_FOLDER = "Recovery"

# Windows creates its recovery partition at 0.5 to 1 GB, and a Steam library
# partition is never that small. Kept well above that range, since Windows
# updates may grow the recovery partition, and well below any useful library.
RECOVERY_PARTITION_MAX_BYTES = 2 * 1024**3

# What the list shows for a partition `install` could not look into.
CONTENTS_UNKNOWN_WITHOUT_ROOT = "contents unknown (run with sudo to inspect)"

# Binary units with decimal names, as Windows Explorer and Disk Management
# show sizes, so the numbers match what the user saw in Windows.
SIZE_UNITS = (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2))


@dataclass(frozen=True)
class NtfsPartition:
    """One NTFS partition, as the partition table describes it."""

    device: PurePosixPath  # such as /dev/nvme0n1p4
    size_bytes: int
    label: str | None
    uuid: str | None
    # Where it is mounted now, or None when it is not mounted.
    mount_point: PurePosixPath | None


@dataclass(frozen=True)
class PartitionContents:
    """What a look inside the partition found."""

    has_windows_system: bool
    has_recovery_folder: bool
    # Folders that hold a Steam library, relative to the partition root,
    # such as "SteamLibrary".
    library_folders: tuple[str, ...]


@dataclass(frozen=True)
class PartitionFacts:
    """A partition and what is known about its contents."""

    partition: NtfsPartition
    # None when nobody could look inside; `unknown_reason` then says why.
    contents: PartitionContents | None
    unknown_reason: str = ""


class PartitionKind(Enum):
    WINDOWS_SYSTEM = "windows-system"
    RECOVERY = "recovery"
    STEAM_LIBRARY = "steam-library"
    OTHER = "other"
    UNKNOWN = "unknown"


# Partitions Windows itself depends on. The library partition is mounted
# read-write at every boot, and Steam writes gigabytes to it. On C:, Windows'
# Fast Startup leaves the volume hibernated, so Linux writes there risk
# Windows' own files. The recovery partition is too small, and Windows
# rewrites it during updates.
NOT_RECOMMENDED_KINDS = frozenset({PartitionKind.WINDOWS_SYSTEM, PartitionKind.RECOVERY})


def could_not_inspect(partition: NtfsPartition, reason: str) -> PartitionFacts:
    """Return the facts for a partition that a look inside failed on, and why."""
    return PartitionFacts(
        partition=partition, contents=None, unknown_reason=f"could not inspect: {reason}"
    )


def not_inspected_without_root(partition: NtfsPartition) -> PartitionFacts:
    """Return the facts for a partition that needs root to mount and look into."""
    return PartitionFacts(
        partition=partition, contents=None, unknown_reason=CONTENTS_UNKNOWN_WITHOUT_ROOT
    )


# --- Classification ----------------------------------------------------------


def _was_not_inspected(facts: PartitionFacts) -> bool:
    return facts.contents is None


def _holds_windows(facts: PartitionFacts) -> bool:
    return facts.contents is not None and facts.contents.has_windows_system


def _holds_a_steam_library(facts: PartitionFacts) -> bool:
    return facts.contents is not None and bool(facts.contents.library_folders)


def _looks_like_recovery(facts: PartitionFacts) -> bool:
    has_folder = facts.contents is not None and facts.contents.has_recovery_folder
    return has_folder or facts.partition.size_bytes <= RECOVERY_PARTITION_MAX_BYTES


# Checked in order; the first rule that matches decides the kind.
# - Unknown first, so the other rules only judge what was actually seen.
# - Windows before the library: C: often holds Steam's own library, under
#   "Program Files (x86)/Steam", and it is still the Windows partition.
# - Library before recovery: a data partition that happens to have a
#   "Recovery" folder is still where the user keeps their games.
CLASSIFICATION_RULES: tuple[tuple[PartitionKind, Callable[[PartitionFacts], bool]], ...] = (
    (PartitionKind.UNKNOWN, _was_not_inspected),
    (PartitionKind.WINDOWS_SYSTEM, _holds_windows),
    (PartitionKind.STEAM_LIBRARY, _holds_a_steam_library),
    (PartitionKind.RECOVERY, _looks_like_recovery),
)


def classify(facts: PartitionFacts) -> PartitionKind:
    for kind, matches in CLASSIFICATION_RULES:
        if matches(facts):
            return kind
    return PartitionKind.OTHER


def is_recommended(kind: PartitionKind) -> bool:
    return kind not in NOT_RECOMMENDED_KINDS


def suggest_default(facts_list: Sequence[PartitionFacts]) -> int | None:
    """Return the position of the partition to suggest, or None.

    A partition is suggested only when it is the one place holding a Steam
    library outside the Windows partition; with two candidates, guessing
    could put the units on the wrong disk.
    """
    library_positions = [
        position
        for position, facts in enumerate(facts_list)
        if classify(facts) is PartitionKind.STEAM_LIBRARY
    ]
    if len(library_positions) == 1:
        return library_positions[0]
    return None


# --- Listing -----------------------------------------------------------------


def format_size(size_bytes: int) -> str:
    for name, unit_bytes in SIZE_UNITS:
        if size_bytes >= unit_bytes:
            return f"{round(size_bytes / unit_bytes)} {name}"
    smallest_name, smallest_bytes = SIZE_UNITS[-1]
    return f"{round(size_bytes / smallest_bytes)} {smallest_name}"


def _describe_library(facts: PartitionFacts) -> str:
    assert facts.contents is not None  # the STEAM_LIBRARY rule guarantees it
    folders = ", ".join(f"{folder}/" for folder in facts.contents.library_folders)
    if len(facts.contents.library_folders) == 1:
        return f"Steam library in {folders}"
    return f"Steam libraries in {folders}"


KIND_DESCRIPTIONS: dict[PartitionKind, Callable[[PartitionFacts], str]] = {
    PartitionKind.WINDOWS_SYSTEM: lambda facts: "Windows system partition (not recommended)",
    PartitionKind.RECOVERY: lambda facts: "Windows recovery partition (not recommended)",
    PartitionKind.STEAM_LIBRARY: _describe_library,
    PartitionKind.OTHER: lambda facts: "no Steam library found",
    PartitionKind.UNKNOWN: lambda facts: facts.unknown_reason,
}


def describe(facts: PartitionFacts) -> str:
    return KIND_DESCRIPTIONS[classify(facts)](facts)


def format_partition_list(facts_list: Sequence[PartitionFacts]) -> list[str]:
    """Return one numbered line per partition, with aligned columns."""
    devices = [str(facts.partition.device) for facts in facts_list]
    sizes = [format_size(facts.partition.size_bytes) for facts in facts_list]
    labels = [facts.partition.label or "" for facts in facts_list]
    number_width = len(str(len(facts_list)))
    device_width = max(map(len, devices), default=0)
    size_width = max(map(len, sizes), default=0)
    label_width = max(map(len, labels), default=0)
    return [
        f"  {number:>{number_width}}) {device:<{device_width}}  {size:>{size_width}}  "
        f"{label:<{label_width}}  {describe(facts)}"
        for number, (facts, device, size, label) in enumerate(
            zip(facts_list, devices, sizes, labels, strict=True), start=1
        )
    ]
