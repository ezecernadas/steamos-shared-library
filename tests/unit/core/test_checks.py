"""Tests for the `doctor` checks.

Each check turns plain data (mounts, library entries) into a result the user
can act on, so the tests also pin down that every failure carries a hint.
"""

from pathlib import PurePosixPath

import pytest

from steamshare.core.checks import (
    MAX_LISTED_PATHS,
    check_library_folder,
    check_library_mount,
    check_linux_only_directories,
    check_unexpected_symlinks,
    check_windows_names,
)
from steamshare.core.doctor import Status
from steamshare.core.library_layout import LibraryEntry
from steamshare.core.mounts import MountPoint

GOOD_NTFS_OPTIONS = frozenset({"rw", "relatime", "uid=1000", "windows_names", "iocharset=utf8"})
STEAMAPPS = PurePosixPath("/mnt/steamlib/SteamLibrary/steamapps")


def make_mount(
    target: str = "/mnt/steamlib",
    filesystem_type: str = "ntfs3",
    options: frozenset[str] = GOOD_NTFS_OPTIONS,
) -> MountPoint:
    return MountPoint(
        target=PurePosixPath(target),
        source="/dev/nvme0n1p5",
        filesystem_type=filesystem_type,
        options=options,
    )


def make_entry(relative_path: str, is_symlink: bool = False) -> LibraryEntry:
    return LibraryEntry(relative_path=PurePosixPath(relative_path), is_symlink=is_symlink)


# check_library_mount


def test_library_mount_is_ok_with_ntfs3_and_windows_names() -> None:
    result = check_library_mount(make_mount())

    assert result.status is Status.OK


def test_library_mount_fails_when_not_mounted() -> None:
    result = check_library_mount(None)

    assert result.status is Status.FAILURE
    assert result.hint is not None
    assert "mount -t ntfs3" in result.hint


def test_library_mount_fails_on_the_ntfs_3g_fuse_driver() -> None:
    result = check_library_mount(make_mount(filesystem_type="fuseblk"))

    assert result.status is Status.FAILURE
    assert result.hint is not None
    assert "modprobe ntfs3" in result.hint


def test_library_mount_fails_on_any_other_filesystem() -> None:
    # The library folder sits on the root filesystem when the partition is
    # not mounted, so this is the usual "not mounted" case.
    result = check_library_mount(make_mount(target="/", filesystem_type="ext4"))

    assert result.status is Status.FAILURE
    assert any("ext4" in detail for detail in result.details)
    assert result.hint is not None
    assert "mount -t ntfs3" in result.hint


def test_library_mount_fails_when_read_only() -> None:
    result = check_library_mount(make_mount(options=GOOD_NTFS_OPTIONS - {"rw"} | {"ro"}))

    assert result.status is Status.FAILURE
    assert result.hint is not None
    assert "powercfg /h off" in result.hint


def test_library_mount_fails_without_windows_names() -> None:
    result = check_library_mount(make_mount(options=GOOD_NTFS_OPTIONS - {"windows_names"}))

    assert result.status is Status.FAILURE
    assert any("windows_names" in detail for detail in result.details)
    assert result.hint is not None


# check_linux_only_directories


def make_bind_mount(name: str, filesystem_type: str = "ext4") -> MountPoint:
    return make_mount(
        target=str(STEAMAPPS / name), filesystem_type=filesystem_type, options=frozenset({"rw"})
    )


def test_linux_only_directories_ok_when_both_bind_mounted_from_ext4() -> None:
    mounts = [make_mount(), make_bind_mount("compatdata"), make_bind_mount("shadercache")]

    result = check_linux_only_directories(mounts, STEAMAPPS)

    assert result.status is Status.OK


def test_linux_only_directories_fail_when_a_bind_mount_is_missing() -> None:
    mounts = [make_mount(), make_bind_mount("compatdata")]

    result = check_linux_only_directories(mounts, STEAMAPPS)

    assert result.status is Status.FAILURE
    assert any("shadercache" in detail for detail in result.details)
    assert not any("compatdata" in detail for detail in result.details)
    assert result.hint is not None
    assert f"mount --bind ~/.local/share/steam-shared/shadercache {STEAMAPPS}/shadercache" in (
        result.hint
    )


@pytest.mark.parametrize("filesystem_type", ["ntfs3", "fuseblk"])
def test_linux_only_directories_fail_when_mounted_from_ntfs(filesystem_type: str) -> None:
    mounts = [
        make_mount(),
        make_bind_mount("compatdata", filesystem_type),
        make_bind_mount("shadercache"),
    ]

    result = check_linux_only_directories(mounts, STEAMAPPS)

    assert result.status is Status.FAILURE
    assert any("compatdata" in detail for detail in result.details)


# check_windows_names


def test_windows_names_ok_when_every_name_is_valid() -> None:
    entries = [make_entry("steamapps"), make_entry("steamapps/appmanifest_1005870.acf")]

    assert check_windows_names(entries).status is Status.OK


def test_windows_names_lists_offending_paths_with_reasons() -> None:
    entries = [make_entry("steamapps/common/Game/aux"), make_entry("steamapps/common/Game/ok")]

    result = check_windows_names(entries)

    assert result.status is Status.FAILURE
    assert len(result.details) == 1
    assert "steamapps/common/Game/aux" in result.details[0]
    assert "reserved device name" in result.details[0]
    assert result.hint is not None


def test_windows_names_reports_a_bad_folder_once_not_for_every_child() -> None:
    # The walker yields the folder itself as an entry, so checking only each
    # entry's own name finds it exactly once.
    entries = [
        make_entry("steamapps/common/Game/c:"),
        make_entry("steamapps/common/Game/c:/file1"),
        make_entry("steamapps/common/Game/c:/file2"),
    ]

    result = check_windows_names(entries)

    assert len(result.details) == 1


def test_windows_names_caps_the_listed_paths() -> None:
    extra = 5
    entries = [make_entry(f"bad{number}?") for number in range(MAX_LISTED_PATHS + extra)]

    result = check_windows_names(entries)

    assert len(result.details) == MAX_LISTED_PATHS + 1
    assert result.details[-1] == f"...and {extra} more"


# check_unexpected_symlinks


def test_unexpected_symlinks_ok_when_only_linux_tools_have_symlinks() -> None:
    entries = [
        make_entry("steamapps/common/Proton - Experimental/files/lib/x.so", is_symlink=True),
        make_entry("steamapps/common/Game/game.exe"),
    ]

    assert check_unexpected_symlinks(entries).status is Status.OK


def test_unexpected_symlinks_warns_about_symlinks_outside_linux_tools() -> None:
    entries = [
        make_entry("steamapps/common/Steam.dll", is_symlink=True),
        make_entry("steamapps/common/SteamLinuxRuntime_4/run", is_symlink=True),
    ]

    result = check_unexpected_symlinks(entries)

    assert result.status is Status.WARNING
    assert result.details == ("steamapps/common/Steam.dll",)


def test_unexpected_symlinks_caps_the_listed_paths() -> None:
    entries = [
        make_entry(f"link{number}", is_symlink=True) for number in range(MAX_LISTED_PATHS + 1)
    ]

    result = check_unexpected_symlinks(entries)

    assert result.details[-1] == "...and 1 more"


# check_library_folder


def test_library_folder_ok_when_it_is_a_directory() -> None:
    result = check_library_folder(PurePosixPath("/mnt/steamlib/SteamLibrary"), True)

    assert result.status is Status.OK


def test_library_folder_fails_when_missing() -> None:
    result = check_library_folder(PurePosixPath("/mnt/steamlib/SteamLibrary"), False)

    assert result.status is Status.FAILURE
    assert result.hint is not None
