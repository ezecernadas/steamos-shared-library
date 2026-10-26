"""Tests for measuring and deleting a purge's ext4 data, in temporary folders."""

import os
from pathlib import Path

import pytest

from steamshare.adapters.folder_contents import folder_size, remove_tree, subfolder_names

OUTSIDE_SIZE = 1_000_000


def make_prefix_with_a_link_outside(tmp_path: Path) -> tuple[Path, Path]:
    """Return a compatdata folder whose prefix links outside it, and the link's target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x" * OUTSIDE_SIZE)
    compatdata = tmp_path / "compatdata"
    dosdevices = compatdata / "1005870" / "pfx" / "dosdevices"
    dosdevices.mkdir(parents=True)
    (compatdata / "1005870" / "pfx" / "user.reg").write_text("y" * 10)
    # As in every Proton prefix: z: points at the root of the filesystem.
    os.symlink(outside, dosdevices / "z:")
    return compatdata, outside


def test_remove_tree_deletes_links_without_following_them(tmp_path: Path) -> None:
    compatdata, outside = make_prefix_with_a_link_outside(tmp_path)

    remove_tree(compatdata)

    assert not compatdata.exists()
    assert (outside / "precious.txt").stat().st_size == OUTSIDE_SIZE


def test_remove_tree_refuses_a_folder_that_is_itself_a_link(tmp_path: Path) -> None:
    compatdata, outside = make_prefix_with_a_link_outside(tmp_path)
    link = tmp_path / "linked"
    os.symlink(outside, link)

    with pytest.raises(OSError):
        remove_tree(link)
    assert (outside / "precious.txt").exists()


def test_folder_size_counts_files_but_not_what_links_point_to(tmp_path: Path) -> None:
    compatdata, _ = make_prefix_with_a_link_outside(tmp_path)

    size = folder_size(compatdata)

    # The file, the folders and the link itself, never the megabyte behind it.
    assert 10 <= size < OUTSIDE_SIZE


def test_subfolder_names_lists_folders_only(tmp_path: Path) -> None:
    (tmp_path / "1005870").mkdir()
    (tmp_path / "48000").mkdir()
    (tmp_path / "file").write_text("")
    os.symlink(tmp_path / "48000", tmp_path / "link")

    assert subfolder_names(tmp_path) == ["1005870", "48000"]
    assert subfolder_names(tmp_path / "missing") == []
