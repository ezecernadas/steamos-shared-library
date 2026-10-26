"""Tests for looking inside an NTFS partition.

Folder trees in `tmp_path` stand in for mounted partitions. The commands that
load ntfs3, mount and unmount are recorded by a fake runner; its `mount`
fills the temporary folder, the way a real mount would show the partition.
"""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.partition_inspector import (
    PartitionCleanupError,
    find_library_folders,
    inspect_contents,
    probe_partition,
)
from steamshare.core.ntfs_partitions import (
    CONTENTS_UNKNOWN_WITHOUT_ROOT,
    NtfsPartition,
    PartitionFacts,
)


def make_partition(mount_point: Path | None = None) -> NtfsPartition:
    return NtfsPartition(
        device=PurePosixPath("/dev/nvme0n1p4"),
        size_bytes=103078166528,
        label="SteamLib",
        uuid="1111AAAA2222BBBB",
        mount_point=PurePosixPath(mount_point) if mount_point else None,
    )


def make_library(root: Path, folder: str) -> None:
    (root / folder / "steamapps").mkdir(parents=True)


# --- What is inside ----------------------------------------------------------


def test_finds_a_library_folder_at_the_top(tmp_path: Path) -> None:
    make_library(tmp_path, "SteamLibrary")

    assert find_library_folders(tmp_path) == ("SteamLibrary",)


def test_finds_a_library_folder_two_levels_down(tmp_path: Path) -> None:
    make_library(tmp_path, "Program Files (x86)/Steam")
    make_library(tmp_path, "Games")

    assert find_library_folders(tmp_path) == ("Games", "Program Files (x86)/Steam")


def test_does_not_look_deeper_than_two_levels(tmp_path: Path) -> None:
    make_library(tmp_path, "a/b/c")

    assert find_library_folders(tmp_path) == ()


def test_a_steamapps_file_is_not_a_library(tmp_path: Path) -> None:
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "steamapps").write_text("not a folder")

    assert find_library_folders(tmp_path) == ()


def test_does_not_follow_symlinked_folders(tmp_path: Path) -> None:
    make_library(tmp_path / "elsewhere", "Library")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "Link").symlink_to(tmp_path / "elsewhere")

    assert find_library_folders(tmp_path / "root") == ()


def test_skips_folders_it_cannot_read(tmp_path: Path) -> None:
    locked = tmp_path / "System Volume Information"
    locked.mkdir()
    make_library(tmp_path, "SteamLibrary")
    locked.chmod(0)
    try:
        assert find_library_folders(tmp_path) == ("SteamLibrary",)
    finally:
        locked.chmod(0o755)


def test_recognizes_the_windows_partition_and_its_recovery_folder(tmp_path: Path) -> None:
    (tmp_path / "Windows" / "System32").mkdir(parents=True)
    (tmp_path / "Recovery").mkdir()

    contents = inspect_contents(tmp_path)

    assert contents.has_windows_system
    assert contents.has_recovery_folder
    assert contents.library_folders == ()


def test_a_plain_data_partition_has_no_markers(tmp_path: Path) -> None:
    make_library(tmp_path, "SteamLibrary")

    contents = inspect_contents(tmp_path)

    assert not contents.has_windows_system
    assert not contents.has_recovery_folder
    assert contents.library_folders == ("SteamLibrary",)


# --- Probing -----------------------------------------------------------------


class FakeCommands:
    """Records commands; `mount` copies a prepared tree into the target folder."""

    def __init__(self, tmp_path: Path) -> None:
        self.commands: list[list[str]] = []
        self.failures: dict[str, subprocess.CalledProcessError] = {}
        self.populate: Callable[[Path], None] = lambda target: make_library(target, "Games")
        self.temporary = tmp_path / "probe"
        self.removed: list[Path] = []

    def run(self, command: Sequence[str]) -> None:
        self.commands.append(list(command))
        name = Path(command[0]).name
        if name in self.failures:
            raise self.failures[name]
        if name == "mount":
            self.populate(Path(command[-1]))

    def make_directory(self) -> Path:
        self.temporary.mkdir()
        return self.temporary

    def remove_directory(self, path: Path) -> None:
        self.removed.append(path)

    def names(self) -> list[str]:
        return [Path(command[0]).name for command in self.commands]


def probe(
    partition: NtfsPartition, fake: FakeCommands, *, can_mount: bool = True
) -> PartitionFacts:
    return probe_partition(
        partition,
        can_mount=can_mount,
        run=fake.run,
        make_directory=fake.make_directory,
        remove_directory=fake.remove_directory,
    )


def test_inspects_a_mounted_partition_where_it_is_without_mounting(tmp_path: Path) -> None:
    make_library(tmp_path, "SteamLibrary")
    fake = FakeCommands(tmp_path)

    facts = probe(make_partition(mount_point=tmp_path), fake, can_mount=False)

    assert facts.contents is not None
    assert facts.contents.library_folders == ("SteamLibrary",)
    assert fake.commands == []


def test_mounts_an_unmounted_partition_read_only_with_ntfs3_and_cleans_up(
    tmp_path: Path,
) -> None:
    fake = FakeCommands(tmp_path)

    facts = probe(make_partition(), fake)

    assert facts.contents is not None
    assert facts.contents.library_folders == ("Games",)
    assert fake.commands == [
        ["/usr/sbin/modprobe", "ntfs3"],
        ["mount", "-t", "ntfs3", "-o", "ro", "/dev/nvme0n1p4", str(fake.temporary)],
        ["umount", str(fake.temporary)],
    ]
    assert fake.removed == [fake.temporary]


def test_does_not_mount_without_root(tmp_path: Path) -> None:
    fake = FakeCommands(tmp_path)

    facts = probe(make_partition(), fake, can_mount=False)

    assert facts.contents is None
    assert facts.unknown_reason == CONTENTS_UNKNOWN_WITHOUT_ROOT
    assert fake.commands == []


def test_a_refused_mount_is_reported_and_the_folder_removed(tmp_path: Path) -> None:
    fake = FakeCommands(tmp_path)
    fake.failures["mount"] = subprocess.CalledProcessError(
        32, ["mount"], stderr='mount: volume is dirty and "force" flag is not set\n'
    )

    facts = probe(make_partition(), fake)

    assert facts.contents is None
    assert "volume is dirty" in facts.unknown_reason
    assert "umount" not in fake.names()
    assert fake.removed == [fake.temporary]


def test_a_failed_module_load_is_reported_without_mounting(tmp_path: Path) -> None:
    fake = FakeCommands(tmp_path)
    fake.failures["modprobe"] = subprocess.CalledProcessError(1, ["modprobe"], stderr="")

    facts = probe(make_partition(), fake)

    assert facts.contents is None
    assert "ntfs3" in facts.unknown_reason
    assert "mount" not in fake.names()


def test_unmounts_even_when_looking_inside_fails(tmp_path: Path) -> None:
    fake = FakeCommands(tmp_path)
    # Nothing appears at the mount point, so listing it fails.
    fake.populate = lambda target: target.rmdir()

    facts = probe(make_partition(), fake)

    assert facts.contents is None
    assert fake.names()[-1] == "umount"


def test_a_failed_unmount_says_how_to_finish_by_hand(tmp_path: Path) -> None:
    fake = FakeCommands(tmp_path)
    fake.failures["umount"] = subprocess.CalledProcessError(32, ["umount"], stderr="busy")

    with pytest.raises(PartitionCleanupError, match="sudo umount"):
        probe(make_partition(), fake)
    # The folder still holds the mount, so it is left in place.
    assert fake.removed == []
