"""Tests for the backup and atomic write shared by every adapter that edits files.

These two helpers carry CONTRIBUTING.md rule 5 for every file steamshare
writes, so the tests pin down what a reader of the file can rely on.
"""

from datetime import datetime
from pathlib import Path

from steamshare.adapters.safe_writes import back_up_file, write_file_atomically

NOW = datetime(2026, 10, 26, 18, 30, 5)


def test_backup_is_a_timestamped_copy_next_to_the_file(tmp_path: Path) -> None:
    original = tmp_path / "config.vdf"
    original.write_text("old\n")

    backup = back_up_file(original, NOW)

    assert backup == tmp_path / "config.vdf.bak-20261026-183005"
    assert backup.read_text() == "old\n"
    assert original.read_text() == "old\n"


def test_atomic_write_replaces_the_content_and_sets_the_mode(tmp_path: Path) -> None:
    target = tmp_path / "config.vdf"
    target.write_text("old\n")

    write_file_atomically(target, "new\n", mode=0o600)

    assert target.read_text() == "new\n"
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    write_file_atomically(tmp_path / "state.json", "{}\n", mode=0o644)

    assert [path.name for path in tmp_path.iterdir()] == ["state.json"]
