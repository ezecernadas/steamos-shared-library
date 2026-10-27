"""Tests for the file where steamshare remembers its own decisions."""

import fcntl
import json
import os
from pathlib import Path

import pytest

from steamshare.adapters.state_file import (
    StateFileError,
    lock_state,
    read_state,
    save_state_changes,
    state_file_path,
)
from steamshare.core.proton_mapping import SteamshareState


def test_the_state_lives_in_the_users_config_folder() -> None:
    assert state_file_path({}, Path("/home/u")) == Path("/home/u/.config/steamshare/state.json")


def test_the_state_follows_xdg_config_home() -> None:
    environment = {"XDG_CONFIG_HOME": "/data/config"}

    assert state_file_path(environment, Path("/home/u")) == Path(
        "/data/config/steamshare/state.json"
    )


def test_a_missing_state_file_means_an_empty_state(tmp_path: Path) -> None:
    assert read_state(tmp_path / "state.json") == SteamshareState()


def test_writes_sorted_lists_and_reads_them_back(tmp_path: Path) -> None:
    path = tmp_path / "steamshare" / "state.json"
    state = SteamshareState(
        keep_native=frozenset({"570", "400"}), managed_mappings=frozenset({"1005870"})
    )

    save_state_changes(path, before=SteamshareState(), after=state)

    assert json.loads(path.read_text()) == {
        "keep_native": ["400", "570"],
        "managed_mappings": ["1005870"],
    }
    assert read_state(path) == state


def test_a_state_file_missing_a_list_reads_it_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"keep_native": ["400"]}')

    assert read_state(path) == SteamshareState(keep_native=frozenset({"400"}))


@pytest.mark.parametrize("content", ["not json", '["a list"]', '{"keep_native": "400"}'])
def test_a_malformed_state_file_is_reported_with_its_path(tmp_path: Path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content)

    with pytest.raises(StateFileError, match=str(path)):
        read_state(path)


# --- Several commands saving at once -------------------------------------------


def test_saving_keeps_what_another_command_saved_since_it_read(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    read_by_watch = SteamshareState()
    # keep-native saves while watch is in the middle of a pass.
    save_state_changes(
        path, before=SteamshareState(), after=SteamshareState(keep_native=frozenset({"570"}))
    )

    save_state_changes(
        path,
        before=read_by_watch,
        after=SteamshareState(managed_mappings=frozenset({"48000"})),
    )

    assert read_state(path) == SteamshareState(
        keep_native=frozenset({"570"}), managed_mappings=frozenset({"48000"})
    )


def test_saving_no_change_leaves_the_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "steamshare" / "state.json"
    unchanged = SteamshareState(keep_native=frozenset({"570"}))

    save_state_changes(path, before=unchanged, after=unchanged)

    assert not path.exists()


def test_the_state_is_locked_while_a_command_saves(tmp_path: Path) -> None:
    path = tmp_path / "steamshare" / "state.json"

    with lock_state(path):
        other = os.open(path.parent, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
