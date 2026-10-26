"""Tests for recognizing the Linux tools Steam installs into a game library.

Linux Steam puts Proton and the Steam Linux Runtime into the same library as
the game. Their symlinks are expected there, so `doctor` must tell them apart
from symlinks that should not be on NTFS.
"""

from pathlib import PurePosixPath

import pytest

from steamshare.core.library_layout import LibraryEntry, count_proton_prefixes, is_inside_linux_tool


def make_entry(relative_path: str) -> LibraryEntry:
    return LibraryEntry(relative_path=PurePosixPath(relative_path), is_symlink=True)


@pytest.mark.parametrize(
    "relative_path",
    [
        "steamapps/common/Proton - Experimental",
        "steamapps/common/Proton - Experimental/files/lib/libfoo.so",
        "steamapps/common/Proton 9.0/proton",
        "steamapps/common/SteamLinuxRuntime_sniper/var/link",
        "steamapps/common/SteamLinuxRuntime_4/run",
    ],
)
def test_recognizes_paths_inside_linux_tools(relative_path: str) -> None:
    assert is_inside_linux_tool(make_entry(relative_path))


@pytest.mark.parametrize(
    "relative_path",
    [
        "steamapps/common/Steam.dll",  # Linux Steam's symlink; must be reported
        "steamapps/common/HYPERFIGHT/game.x86_64",
        "steamapps/common",
        "steamapps/Proton - Experimental/file",  # a tool name outside common/
        "steamapps/compatdata/1005870/pfx",
    ],
)
def test_rejects_paths_outside_linux_tools(relative_path: str) -> None:
    assert not is_inside_linux_tool(make_entry(relative_path))


def test_proton_prefixes_are_counted_by_their_appid_folders() -> None:
    # Proton names each prefix after its game's appid; anything else is not one.
    assert count_proton_prefixes(["1005870", "48000", "pfx-backup", "tmp"]) == 2
