"""Tests for finding the `steamshare` launcher that the watch service runs."""

from pathlib import Path

from steamshare.adapters.launcher_location import LAUNCHER_PATH

REPOSITORY = Path(__file__).resolve().parents[3]


def test_the_launcher_is_the_script_at_the_repository_root() -> None:
    assert LAUNCHER_PATH == REPOSITORY / "steamshare"
    assert LAUNCHER_PATH.is_file()
