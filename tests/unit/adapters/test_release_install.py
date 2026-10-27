"""Tests for placing a downloaded release in its install folder, in temporary folders."""

from pathlib import Path

import pytest

from steamshare.adapters.release_install import (
    RELEASE_MARKER_FILE_NAME,
    ForeignFolderError,
    installed_release_version,
    is_release_install,
    place_release,
)


def make_release(folder: Path, launcher_text: str = "#!/usr/bin/env bash\n") -> Path:
    """Return a fake extracted release: the launcher and the package folder."""
    (folder / "src" / "steamshare").mkdir(parents=True)
    (folder / "src" / "steamshare" / "__init__.py").write_text("")
    launcher = folder / "steamshare"
    launcher.write_text(launcher_text)
    launcher.chmod(0o755)
    return folder


def test_a_folder_with_the_marker_is_a_release_install(tmp_path: Path) -> None:
    (tmp_path / RELEASE_MARKER_FILE_NAME).write_text("0.2.0\n")

    assert is_release_install(tmp_path)
    assert installed_release_version(tmp_path) == "0.2.0"


def test_a_clone_is_not_a_release_install(tmp_path: Path) -> None:
    make_release(tmp_path)

    assert not is_release_install(tmp_path)
    assert installed_release_version(tmp_path) is None


def test_a_missing_folder_is_not_a_release_install(tmp_path: Path) -> None:
    assert not is_release_install(tmp_path / "missing")


def test_placing_copies_the_release_and_marks_it(tmp_path: Path) -> None:
    source = make_release(tmp_path / "download")
    destination = tmp_path / "opt" / "steamshare"
    destination.parent.mkdir()

    place_release(source, destination, version="0.2.0", owner=None)

    assert (destination / "src" / "steamshare" / "__init__.py").is_file()
    assert (destination / "steamshare").stat().st_mode & 0o111
    assert installed_release_version(destination) == "0.2.0"


def test_placing_again_replaces_the_previous_release(tmp_path: Path) -> None:
    destination = tmp_path / "opt" / "steamshare"
    destination.parent.mkdir()
    place_release(make_release(tmp_path / "old"), destination, version="0.1.0", owner=None)
    (destination / "removed-in-0.2.0.py").write_text("")

    place_release(
        make_release(tmp_path / "new", "#!/usr/bin/env bash\n# 0.2.0\n"),
        destination,
        version="0.2.0",
        owner=None,
    )

    assert installed_release_version(destination) == "0.2.0"
    assert "# 0.2.0" in (destination / "steamshare").read_text()
    assert not (destination / "removed-in-0.2.0.py").exists()
    # Nothing is left beside it.
    assert sorted(path.name for path in destination.parent.iterdir()) == ["steamshare"]


def test_leftovers_of_an_interrupted_run_are_cleared(tmp_path: Path) -> None:
    destination = tmp_path / "opt" / "steamshare"
    destination.parent.mkdir()
    (destination.parent / ".steamshare.new" / "half").mkdir(parents=True)
    (destination.parent / ".steamshare.old" / "older").mkdir(parents=True)

    place_release(make_release(tmp_path / "download"), destination, version="0.2.0", owner=None)

    assert sorted(path.name for path in destination.parent.iterdir()) == ["steamshare"]


def test_another_folder_with_that_name_is_never_replaced(tmp_path: Path) -> None:
    destination = tmp_path / "opt" / "steamshare"
    make_release(destination)
    (destination / "my-notes.txt").write_text("mine")

    with pytest.raises(ForeignFolderError):
        place_release(make_release(tmp_path / "download"), destination, version="0.2.0", owner=None)

    assert (destination / "my-notes.txt").read_text() == "mine"


def test_placing_for_a_user_gives_them_every_file(tmp_path: Path) -> None:
    destination = tmp_path / "home" / ".local" / "share" / "steamshare"
    destination.parent.mkdir(parents=True)
    owned: list[tuple[Path, int, int]] = []

    place_release(
        make_release(tmp_path / "download"),
        destination,
        version="0.2.0",
        owner=(1000, 1001),
        change_owner=lambda path, user_id, group_id: owned.append((path, user_id, group_id)),
    )

    # Recorded under the staging name, before the rename into place.
    names = {path.name for path, _, _ in owned}
    assert {
        ".steamshare.new",
        "steamshare",
        "src",
        "__init__.py",
        RELEASE_MARKER_FILE_NAME,
    } <= names
    assert {(user_id, group_id) for _, user_id, group_id in owned} == {(1000, 1001)}
