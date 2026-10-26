"""Store `SteamshareState` as JSON in the user's config folder.

The state is what lets steamshare tell its own mappings from the user's
(`managed_mappings`), and remember which apps stay native (`keep_native`).
Lists are written sorted, so the file only changes when the state does.
"""

import json
from collections.abc import Mapping
from pathlib import Path

from steamshare.adapters.safe_writes import write_file_atomically
from steamshare.adapters.xdg_directories import user_config_home
from steamshare.core.proton_mapping import SteamshareState

# Relative to the user's config folder (see `xdg_directories`).
STATE_RELATIVE_PATH = Path("steamshare") / "state.json"

KEEP_NATIVE_KEY = "keep_native"
MANAGED_MAPPINGS_KEY = "managed_mappings"

STATE_FILE_MODE = 0o644


class StateFileError(Exception):
    """The state file could not be read; the message names the file."""


def state_file_path(environment: Mapping[str, str], home: Path) -> Path:
    return user_config_home(environment, home) / STATE_RELATIVE_PATH


def read_state(path: Path) -> SteamshareState:
    """Read the state, or return an empty one before the first run."""
    if not path.exists():
        return SteamshareState()
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        return SteamshareState(
            keep_native=_read_appids(content, KEEP_NATIVE_KEY),
            managed_mappings=_read_appids(content, MANAGED_MAPPINGS_KEY),
        )
    except ValueError as error:
        raise StateFileError(f"{path}: {error}") from error


def _read_appids(content: object, key: str) -> frozenset[str]:
    if not isinstance(content, dict):
        raise ValueError("expected a JSON object")
    appids = content.get(key, [])
    if not isinstance(appids, list) or not all(isinstance(appid, str) for appid in appids):
        raise ValueError(f"{key!r} must be a list of appid strings")
    return frozenset(appids)


def write_state(path: Path, state: SteamshareState) -> None:
    content = {
        KEEP_NATIVE_KEY: sorted(state.keep_native),
        MANAGED_MAPPINGS_KEY: sorted(state.managed_mappings),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file_atomically(path, json.dumps(content, indent=2) + "\n", STATE_FILE_MODE)
