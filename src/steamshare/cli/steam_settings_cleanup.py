"""The Steam part of `uninstall --purge`: clear the Proton mappings steamshare made.

Steam keeps those mappings in memory and writes them back on exit, so they
can only be cleared through the running client, as `watch` set them
(`adapters.steam_client_api`). Which ones to clear is decided in
`core.mapping_cleanup`; this module reads the state file, asks Steam, and
reports in words what went wrong and what to do.

Steam is checked while `uninstall` is still only looking, so a purge that
cannot reach Steam refuses before anything is removed. A dry run does not
refuse: it says how many games a real purge would check. The debugger
listens on 127.0.0.1:8080, which root reaches as well as the user, so the
check needs no switch to the desktop user.
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from steamshare.adapters.state_file import StateFileError, read_state
from steamshare.adapters.steam_cdp import SteamDebuggerError
from steamshare.adapters.steam_client_api import OwnedGames
from steamshare.cli.shared import SUDO_UNINSTALL_COMMAND, CommandError
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.mapping_cleanup import (
    MappingCleanup,
    clear_requests,
    plan_mapping_cleanup,
    unconfirmed_clears,
)
from steamshare.core.proton_mapping import SteamshareState
from steamshare.core.uninstall_plan import UncheckedMappings

PURGE_FLAG = "--purge"
KEEP_STEAM_SETTINGS_FLAG = "--keep-steam-settings"
SUDO_PURGE_COMMAND = f"{SUDO_UNINSTALL_COMMAND} {PURGE_FLAG}"

# The debugger's own error is left out: it suggests running install, which
# does not apply here, and the steps below cover both of its causes (Steam
# closed, or still starting).
STEAM_UNREACHABLE_MESSAGE = (
    f"{PURGE_FLAG} clears the Proton choices steamshare made in Steam, so Steam must be "
    "running, but it is not reachable. Start Steam (it must have been restarted once since "
    "install so its debugger is on), wait for the library to load, then run "
    f"`{SUDO_PURGE_COMMAND}` again. To purge without opening Steam, add "
    f"{KEEP_STEAM_SETTINGS_FLAG}: those choices then stay in Steam. Nothing was changed."
)


class SteamMappingClient(Protocol):
    """What the purge needs from Steam; `SteamClientApi` in production."""

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames: ...
    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]: ...


# Opens the running Steam client, given the Steam root; raises
# `SteamDebuggerError` when Steam or its debugger is not up.
ConnectToSteam = Callable[[Path], AbstractContextManager[SteamMappingClient]]


def check_steam_mappings(
    state_file: Path | None, steam_root: Path, connect: ConnectToSteam, *, dry_run: bool
) -> MappingCleanup | UncheckedMappings:
    """Ask Steam which of steamshare's mappings are still in place, before any change."""
    state = _read_state(state_file)
    managed = state.managed_mappings
    if not managed:
        return MappingCleanup(to_clear=(), kept_installed=())
    try:
        with connect(steam_root) as steam:
            owned = steam.owned_games(managed)
    except SteamDebuggerError as error:
        if dry_run:
            return UncheckedMappings(managed_count=len(managed))
        raise CommandError(STEAM_UNREACHABLE_MESSAGE) from error
    if owned.without_details:
        if dry_run:
            return UncheckedMappings(managed_count=len(managed))
        raise CommandError(
            f"Steam did not report {len(owned.without_details)} of the games steamshare "
            "mapped in time, so it cannot tell which are installed. Wait until the Steam "
            f"window shows the library, then run `{SUDO_PURGE_COMMAND}` again. "
            "Nothing was changed."
        )
    return plan_mapping_cleanup(owned.games, state)


def clear_steam_mappings(
    cleanup: MappingCleanup, steam_root: Path, connect: ConnectToSteam
) -> list[OwnedGame]:
    """Clear the mappings and return the games Steam did not confirm."""
    # One script for every game: unlike watch, which saves the state after
    # each batch, nothing here needs a midway save.
    try:
        with connect(steam_root) as steam:
            results = steam.specify_compat_tools(clear_requests(cleanup))
    except SteamDebuggerError as error:
        raise CommandError(
            f"Steam stopped answering while its Proton choices were cleared ({error}). "
            "The watch service is already removed. Start Steam, wait for the library to "
            f"load, then run `{SUDO_PURGE_COMMAND}` again; it continues with what is left."
        ) from error
    return unconfirmed_clears(cleanup, results)


def _read_state(state_file: Path | None) -> SteamshareState:
    if state_file is None:
        return SteamshareState()
    try:
        return read_state(state_file)
    except StateFileError as error:
        raise CommandError(
            f"Could not read steamshare's state ({error}), which lists the Proton choices "
            f"steamshare made. Fix or remove the file, or add {KEEP_STEAM_SETTINGS_FLAG} to "
            "leave those choices in Steam. Nothing was changed."
        ) from error
