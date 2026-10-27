"""Tests for the `steamshare` link on PATH, in temporary folders."""

import os
from pathlib import Path

import pytest

from steamshare.adapters.command_link import (
    ExistingCommand,
    find_command_links,
    inspect_command_path,
    is_on_read_only_filesystem,
    is_steamshare_link,
    link_command,
)


def make_checkout(folder: Path) -> Path:
    """Return the launcher of a fake steamshare checkout in `folder`."""
    (folder / "src" / "steamshare").mkdir(parents=True)
    launcher = folder / "steamshare"
    launcher.write_text("#!/usr/bin/env bash\n")
    return launcher


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    return make_checkout(tmp_path / "repository")


@pytest.fixture
def bin_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    return folder


def test_a_free_name_is_missing(bin_folder: Path, launcher: Path) -> None:
    assert inspect_command_path(bin_folder / "steamshare", launcher) is ExistingCommand.MISSING


def test_a_link_to_this_launcher_is_current(bin_folder: Path, launcher: Path) -> None:
    link = bin_folder / "steamshare"
    link.symlink_to(launcher)

    assert inspect_command_path(link, launcher) is ExistingCommand.CURRENT_LINK


def test_a_link_to_another_checkout_is_an_old_link(
    tmp_path: Path, bin_folder: Path, launcher: Path
) -> None:
    link = bin_folder / "steamshare"
    link.symlink_to(make_checkout(tmp_path / "old-clone"))

    assert inspect_command_path(link, launcher) is ExistingCommand.OLD_LINK


@pytest.mark.parametrize("kind", ["file", "foreign link", "dangling link"])
def test_anything_else_is_another_file(
    tmp_path: Path, bin_folder: Path, launcher: Path, kind: str
) -> None:
    path = bin_folder / "steamshare"
    if kind == "file":
        path.write_text("#!/bin/sh\n")
    elif kind == "foreign link":
        other = tmp_path / "other" / "steamshare"
        other.parent.mkdir()
        other.write_text("")
        path.symlink_to(other)
    else:
        path.symlink_to(tmp_path / "gone" / "steamshare")

    assert inspect_command_path(path, launcher) is ExistingCommand.OTHER_FILE
    assert not is_steamshare_link(path)


def test_linking_creates_an_absolute_link_to_the_launcher(bin_folder: Path, launcher: Path) -> None:
    link = bin_folder / "steamshare"

    link_command(link, launcher)

    assert os.readlink(link) == str(launcher)
    assert is_steamshare_link(link)


def test_linking_replaces_an_old_link(tmp_path: Path, bin_folder: Path, launcher: Path) -> None:
    link = bin_folder / "steamshare"
    link.symlink_to(make_checkout(tmp_path / "old-clone"))

    link_command(link, launcher)

    assert os.readlink(link) == str(launcher)
    assert [path.name for path in bin_folder.iterdir()] == ["steamshare"]


def test_finds_only_steamshare_links(tmp_path: Path, launcher: Path) -> None:
    ours, theirs, empty = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for folder in (ours, theirs, empty):
        folder.mkdir()
    (ours / "steamshare").symlink_to(launcher)
    (theirs / "steamshare").write_text("")

    assert find_command_links([ours, theirs, empty, tmp_path / "missing"]) == [ours / "steamshare"]


def test_a_writable_folder_is_not_on_a_read_only_filesystem(bin_folder: Path) -> None:
    assert not is_on_read_only_filesystem(bin_folder)
