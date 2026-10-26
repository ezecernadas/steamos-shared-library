"""Tests for writing unit files and creating the ext4 data folders.

Both run as root on /etc and on the user's home, so the tests pin down the
safety rules: nothing is rewritten when nothing changed, an old file is backed
up before it is replaced, and a dry run writes nothing at all.
"""

from datetime import datetime
from pathlib import Path

from steamshare.adapters.unit_installer import (
    FileAction,
    create_owned_directories,
    install_owned_unit_files,
    install_unit_files,
)
from steamshare.core.systemd_units import UnitFile

NOW = datetime(2026, 10, 26, 18, 30, 5)
BACKUP_SUFFIX = ".bak-20261026-183005"


def make_unit(content: str = "[Unit]\n", name: str = "mnt-steamlib.mount") -> UnitFile:
    return UnitFile(name=name, content=content, enable_at_boot=True)


# --- Unit files --------------------------------------------------------------


def test_creates_a_missing_unit_file(tmp_path: Path) -> None:
    [outcome] = install_unit_files([make_unit("new\n")], tmp_path, now=NOW)

    assert outcome.action is FileAction.CREATED
    assert outcome.path == tmp_path / "mnt-steamlib.mount"
    assert outcome.backup is None
    assert outcome.path.read_text() == "new\n"


def test_written_unit_files_are_readable_by_everyone_but_writable_only_by_owner(
    tmp_path: Path,
) -> None:
    [outcome] = install_unit_files([make_unit()], tmp_path, now=NOW)

    assert outcome.path.stat().st_mode & 0o777 == 0o644


def test_leaves_an_identical_unit_file_untouched(tmp_path: Path) -> None:
    existing = tmp_path / "mnt-steamlib.mount"
    existing.write_text("same\n")
    modified_before = existing.stat().st_mtime_ns

    [outcome] = install_unit_files([make_unit("same\n")], tmp_path, now=NOW)

    assert outcome.action is FileAction.UNCHANGED
    assert existing.stat().st_mtime_ns == modified_before
    assert list(tmp_path.iterdir()) == [existing]


def test_backs_up_a_changed_unit_file_before_replacing_it(tmp_path: Path) -> None:
    existing = tmp_path / "mnt-steamlib.mount"
    existing.write_text("old\n")

    [outcome] = install_unit_files([make_unit("new\n")], tmp_path, now=NOW)

    assert outcome.action is FileAction.UPDATED
    assert outcome.backup == tmp_path / f"mnt-steamlib.mount{BACKUP_SUFFIX}"
    assert outcome.backup.read_text() == "old\n"
    assert existing.read_text() == "new\n"


def test_dry_run_reports_actions_without_writing(tmp_path: Path) -> None:
    (tmp_path / "changed.mount").write_text("old\n")
    units = [make_unit("new\n", "changed.mount"), make_unit("new\n", "missing.mount")]

    outcomes = install_unit_files(units, tmp_path, now=NOW, dry_run=True)

    assert [outcome.action for outcome in outcomes] == [FileAction.UPDATED, FileAction.CREATED]
    assert all(outcome.backup is None for outcome in outcomes)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["changed.mount"]
    assert (tmp_path / "changed.mount").read_text() == "old\n"


# --- Data folders ------------------------------------------------------------


class RecordingChown:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int, int]] = []

    def __call__(self, path: Path, user_id: int, group_id: int) -> None:
        self.calls.append((Path(path), user_id, group_id))


def test_creates_missing_folders_and_their_missing_parents_owned_by_the_user(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    shared = home / "steam-shared"
    chown = RecordingChown()

    outcomes = create_owned_directories(
        [shared / "compatdata", shared / "shadercache"], 1000, 1000, change_owner=chown
    )

    assert [outcome.action for outcome in outcomes] == [FileAction.CREATED, FileAction.CREATED]
    assert (shared / "compatdata").is_dir()
    assert (shared / "shadercache").is_dir()
    # The parent is created too, and must not be left owned by root.
    assert chown.calls == [
        (shared, 1000, 1000),
        (shared / "compatdata", 1000, 1000),
        (shared / "shadercache", 1000, 1000),
    ]


def test_leaves_existing_folders_and_their_owner_alone(tmp_path: Path) -> None:
    existing = tmp_path / "compatdata"
    existing.mkdir()
    chown = RecordingChown()

    [outcome] = create_owned_directories([existing], 1000, 1000, change_owner=chown)

    assert outcome.action is FileAction.UNCHANGED
    assert chown.calls == []


def test_dry_run_creates_no_folder(tmp_path: Path) -> None:
    missing = tmp_path / "steam-shared" / "compatdata"
    chown = RecordingChown()

    [outcome] = create_owned_directories([missing], 1000, 1000, change_owner=chown, dry_run=True)

    assert outcome.action is FileAction.CREATED
    assert not (tmp_path / "steam-shared").exists()
    assert chown.calls == []


# --- Unit files the user owns ------------------------------------------------


def test_owned_unit_files_and_their_new_folders_are_given_to_the_user(tmp_path: Path) -> None:
    unit_directory = tmp_path / "home" / ".config" / "systemd" / "user"
    (tmp_path / "home").mkdir()
    chown = RecordingChown()

    [outcome] = install_owned_unit_files(
        [make_unit("new\n", "w.service")], unit_directory, 1000, 1000, change_owner=chown, now=NOW
    )

    assert outcome.action is FileAction.CREATED
    assert outcome.path.read_text() == "new\n"
    assert chown.calls == [
        (tmp_path / "home" / ".config", 1000, 1000),
        (tmp_path / "home" / ".config" / "systemd", 1000, 1000),
        (unit_directory, 1000, 1000),
        (unit_directory / "w.service", 1000, 1000),
    ]


def test_an_updated_owned_unit_and_its_backup_are_given_to_the_user(tmp_path: Path) -> None:
    (tmp_path / "w.service").write_text("old\n")
    chown = RecordingChown()

    [outcome] = install_owned_unit_files(
        [make_unit("new\n", "w.service")], tmp_path, 1000, 1000, change_owner=chown, now=NOW
    )

    assert outcome.action is FileAction.UPDATED
    assert chown.calls == [
        (tmp_path / "w.service", 1000, 1000),
        (tmp_path / f"w.service{BACKUP_SUFFIX}", 1000, 1000),
    ]


def test_an_unchanged_owned_unit_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "w.service").write_text("same\n")
    chown = RecordingChown()

    [outcome] = install_owned_unit_files(
        [make_unit("same\n", "w.service")], tmp_path, 1000, 1000, change_owner=chown, now=NOW
    )

    assert outcome.action is FileAction.UNCHANGED
    assert chown.calls == []


def test_a_dry_run_of_owned_units_writes_and_gives_away_nothing(tmp_path: Path) -> None:
    unit_directory = tmp_path / "systemd" / "user"
    chown = RecordingChown()

    [outcome] = install_owned_unit_files(
        [make_unit("new\n", "w.service")],
        unit_directory,
        1000,
        1000,
        change_owner=chown,
        dry_run=True,
    )

    assert outcome.action is FileAction.CREATED
    assert not (tmp_path / "systemd").exists()
    assert chown.calls == []
