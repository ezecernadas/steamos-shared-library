"""Tests for walking the library folder.

The walk must stay on the NTFS partition: it never follows symlinks and never
enters the bind-mounted ext4 folders, where Windows-invalid names are fine.
"""

import os
from pathlib import Path, PurePosixPath

from steamshare.adapters.library_walker import walk_library
from steamshare.core.library_layout import LibraryEntry


def relative_paths(entries: list[LibraryEntry]) -> set[str]:
    return {str(entry.relative_path) for entry in entries}


def test_yields_every_file_and_folder_below_the_root(tmp_path: Path) -> None:
    (tmp_path / "steamapps" / "common" / "Game").mkdir(parents=True)
    (tmp_path / "steamapps" / "common" / "Game" / "game.exe").write_text("")

    entries = list(walk_library(tmp_path))

    assert relative_paths(entries) == {
        "steamapps",
        "steamapps/common",
        "steamapps/common/Game",
        "steamapps/common/Game/game.exe",
    }
    assert not any(entry.is_symlink for entry in entries)


def test_reports_symlinks_without_following_them(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    library = tmp_path / "library"
    library.mkdir()
    (library / "folder_link").symlink_to(outside)
    (library / "Steam.dll").symlink_to("/home/user/.steam/legacycompat/Steam.dll")

    entries = list(walk_library(library))

    assert entries == [
        LibraryEntry(PurePosixPath("Steam.dll"), is_symlink=True),
        LibraryEntry(PurePosixPath("folder_link"), is_symlink=True),
    ]


def test_does_not_enter_folders_on_another_device(tmp_path: Path) -> None:
    bind_mounted = tmp_path / "steamapps" / "compatdata"
    (bind_mounted / "1005870").mkdir(parents=True)
    root_device = os.lstat(tmp_path).st_dev

    def fake_device_of(path: str) -> int:
        # Pretend compatdata is bind-mounted from another filesystem.
        return root_device + 1 if Path(path) == bind_mounted else root_device

    entries = list(walk_library(tmp_path, device_of=fake_device_of))

    assert relative_paths(entries) == {"steamapps", "steamapps/compatdata"}
