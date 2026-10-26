"""Tests for looking up mounts by path.

`doctor` needs two lookups: which mount holds the library (to check its
driver and options), and whether a bind mount sits exactly on a directory.
"""

from pathlib import PurePosixPath

from steamshare.core.mounts import MountPoint, find_mount_at, find_mount_containing


def make_mount(target: str, filesystem_type: str = "ext4") -> MountPoint:
    return MountPoint(
        target=PurePosixPath(target),
        source="/dev/sda1",
        filesystem_type=filesystem_type,
        options=frozenset({"rw"}),
    )


ROOT = make_mount("/")
STEAMLIB = make_mount("/mnt/steamlib", "ntfs3")
STEAMLIB_SIBLING = make_mount("/mnt/steamlib2")
MOUNTS = [ROOT, STEAMLIB, STEAMLIB_SIBLING]


def test_containing_mount_is_the_longest_parent() -> None:
    path = PurePosixPath("/mnt/steamlib/SteamLibrary")

    assert find_mount_containing(MOUNTS, path) == STEAMLIB


def test_containing_mount_includes_an_exact_target() -> None:
    assert find_mount_containing(MOUNTS, PurePosixPath("/mnt/steamlib")) == STEAMLIB


def test_containing_mount_compares_whole_components_not_text_prefixes() -> None:
    path = PurePosixPath("/mnt/steamlib2/SteamLibrary")

    assert find_mount_containing(MOUNTS, path) == STEAMLIB_SIBLING


def test_containing_mount_falls_back_to_root() -> None:
    assert find_mount_containing(MOUNTS, PurePosixPath("/home/user")) == ROOT


def test_containing_mount_is_none_when_no_mount_matches() -> None:
    assert find_mount_containing([STEAMLIB], PurePosixPath("/home/user")) is None


def test_later_mount_on_the_same_target_hides_the_earlier_one() -> None:
    hidden = make_mount("/mnt/steamlib", "fuseblk")
    visible = make_mount("/mnt/steamlib", "ntfs3")
    path = PurePosixPath("/mnt/steamlib/SteamLibrary")

    assert find_mount_containing([hidden, visible], path) == visible
    assert find_mount_at([hidden, visible], PurePosixPath("/mnt/steamlib")) == visible


def test_mount_at_requires_an_exact_target() -> None:
    assert find_mount_at(MOUNTS, PurePosixPath("/mnt/steamlib")) == STEAMLIB
    assert find_mount_at(MOUNTS, PurePosixPath("/mnt/steamlib/SteamLibrary")) is None
