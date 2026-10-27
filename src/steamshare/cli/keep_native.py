"""The `keep-native` command: tick, in a list, the games Linux Steam should run natively.

`force-proton --keep-native APPID` does the same with Steam closed, for one
appid the user looked up. This command runs while Steam is open, like
`watch`: it asks the running client for the owned games, lists the ones
steamshare has a say in (`core.keep_native_choice`) in a full-screen
checkbox list (`core.checkbox_picker`, drawn by `adapters.curses_picker`),
and applies the choice at once:

- a game newly kept native: saved in `keep_native`, and the Proton mapping
  steamshare made for it, if any, is cleared in Steam;
- a game unticked: removed from `keep_native`, and mapped to Proton again
  if `watch` would map it, by the same rules (`core.live_proton_mapping`).

The choice is saved before Steam is asked for anything, so if Steam does not
take a change, `watch` makes it on its next pass. Only this command's own
changes are saved (`adapters.state_file`), so a `watch` pass saving at the
same time keeps its own.

Every effect goes through `System`, so the tests run the whole command with
a fake Steam and scripted key presses.
"""

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from steamshare.adapters.curses_picker import run_picker
from steamshare.adapters.generated_units import find_generated_unit
from steamshare.adapters.state_file import (
    StateFileError,
    read_state,
    save_state_changes,
    state_file_path,
)
from steamshare.adapters.steam_cdp import SteamDebuggerError
from steamshare.adapters.steam_client_api import OwnedGames, connect_to_steam_client
from steamshare.adapters.steam_config import DEFAULT_STEAM_ROOT
from steamshare.cli.live_changes import CompatToolClient, apply_batch, format_game_line
from steamshare.cli.shared import INSTALLED_COMMAND, CommandError, absolute_path
from steamshare.cli.watch_service_setup import USER_UNIT_DIRECTORY_IN_HOME
from steamshare.core.checkbox_picker import (
    PickerItem,
    PickerOutcome,
    PickerState,
    new_picker,
)
from steamshare.core.keep_native_choice import (
    KeepNativeChange,
    keep_native_candidates,
    plan_keep_native_change,
    with_keep_native_change,
)
from steamshare.core.live_proton_mapping import (
    LiveContext,
    LiveDecision,
    OwnedGame,
    is_change,
    plan_live_mappings,
)
from steamshare.core.proton_mapping import DEFAULT_PROTON_TOOL, SteamshareState
from steamshare.core.systemd_units import WATCH_SERVICE, read_watch_service_library

EXIT_OK = 0
EXIT_FAILURE = 1

PICKER_TITLE = "steamshare keep-native: tick the games Linux Steam should run natively"
INSTALLED_NOTE = "installed"
# Wide enough for the longest reason a given-back game is left alone with.
DECISION_COLUMN_WIDTH = 29

FORCE_PROTON_HINT = (
    f"`{INSTALLED_COMMAND} force-proton --library <library> --keep-native APPID` "
    "(with Steam closed)"
)
SEPARATE_LIBRARY_HINT = (
    "To play a native game on Linux and keep the shared library working for Windows, "
    "install it in a separate Linux library instead (an ext4 folder added in Steam > "
    "Settings > Storage)."
)


class SteamGameClient(CompatToolClient, Protocol):
    """What `keep-native` needs from Steam; `SteamClientApi` in production."""

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames: ...


@dataclass(frozen=True)
class System:
    """Every effect `keep-native` has outside its arguments, replaceable in tests."""

    state_path: Path
    # The user's watch service, whose --library is the default one.
    watch_unit_path: Path
    connect_to_steam: Callable[[], AbstractContextManager[SteamGameClient]]
    # Whether both input and output are a terminal the list can be drawn on.
    is_interactive: bool
    # Shows the list until the user confirms or cancels; returns the last state.
    pick: Callable[[PickerState, str], PickerState]


class KeepNativeError(CommandError):
    """An expected failure of `keep-native`, with a message that says what to do next."""


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "keep-native",
        help="while Steam runs, pick from a list the games Linux Steam should run natively "
        "instead of through Proton",
        description="Lists the native Linux games steamshare decides about, with the ones "
        "already kept native ticked. Space ticks, / or typing filters by name, Enter saves, "
        "q or Esc cancels. Mappings steamshare made for newly kept games are cleared at once, "
        "and unticked games are mapped to Proton again. Steam must be running.",
    )
    parser.add_argument(
        "--library",
        type=absolute_path,
        help="the shared Steam library folder (default: the one the watch service uses)",
    )
    parser.add_argument(
        "--tool",
        default=DEFAULT_PROTON_TOOL,
        help=f"the compatibility tool for games given back to Proton "
        f"(default: {DEFAULT_PROTON_TOOL})",
    )
    parser.add_argument(
        "--steam-root",
        type=Path,
        default=DEFAULT_STEAM_ROOT,
        help="the Steam install folder (default: ~/.steam/steam; for the Flatpak, "
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam)",
    )
    parser.set_defaults(run=run)


def run(arguments: argparse.Namespace) -> int:
    home = Path.home()
    system = System(
        state_path=state_file_path(os.environ, home),
        watch_unit_path=home / USER_UNIT_DIRECTORY_IN_HOME / WATCH_SERVICE,
        connect_to_steam=lambda: connect_to_steam_client(arguments.steam_root),
        is_interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        pick=run_picker,
    )
    return keep_native(arguments, system)


def keep_native(arguments: argparse.Namespace, system: System) -> int:
    try:
        return _keep_native(arguments, system)
    except (CommandError, SteamDebuggerError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE


def _keep_native(arguments: argparse.Namespace, system: System) -> int:
    if not system.is_interactive:
        raise KeepNativeError(
            "keep-native shows a list to tick games in, but input or output is not a "
            f"terminal. From a script, use {FORCE_PROTON_HINT}."
        )
    library = arguments.library or _watch_service_library(system.watch_unit_path)
    state = _read_state(system.state_path)
    # The list stays open as long as the user likes, so Steam is asked once
    # for the games and again, afresh, for the changes.
    with system.connect_to_steam() as steam:
        owned = steam.owned_games()
    candidates = keep_native_candidates(owned.games, state, library)
    _print_games_without_details(owned)
    if not candidates:
        print("No native Linux games to choose from: nothing was changed.")
        return EXIT_OK
    try:
        picked = system.pick(_picker_for(candidates, state), PICKER_TITLE)
    except KeyboardInterrupt:
        # Ctrl+C in the list cancels, as q does.
        print()
        picked = None
    if picked is None or picked.outcome is not PickerOutcome.CONFIRMED:
        print("Cancelled; nothing was changed.")
        return EXIT_OK
    change = plan_keep_native_change(candidates, state, picked.checked)
    if change.is_empty():
        print("Nothing to change.")
        return EXIT_OK
    new_state = with_keep_native_change(state, change)
    # Saved first: if Steam then fails to take a change, watch makes it.
    _save_state_changes(system.state_path, state, new_state)
    all_confirmed = _apply_in_steam(change, new_state, library, arguments.tool, system)
    print_keep_native_warnings(change.newly_kept, library)
    return EXIT_OK if all_confirmed else EXIT_FAILURE


def _picker_for(candidates: Sequence[OwnedGame], state: SteamshareState) -> PickerState:
    items = tuple(
        PickerItem(game.appid, game.name, INSTALLED_NOTE if game.installed else "")
        for game in candidates
    )
    kept = frozenset(game.appid for game in candidates if game.appid in state.keep_native)
    # The adapter sets the real height from the terminal before drawing.
    return new_picker(items, checked=kept, page_height=1)


def _apply_in_steam(
    change: KeepNativeChange,
    state: SteamshareState,
    library: PurePosixPath,
    tool: str,
    system: System,
) -> bool:
    """Make the changes the new state calls for; return whether Steam took them all."""
    changed_appids = frozenset(game.appid for game in (*change.newly_kept, *change.given_back))
    with system.connect_to_steam() as steam:
        # Read again: the tools may have changed while the list was open.
        games = steam.owned_games(changed_appids).games
        decisions = plan_live_mappings(games, state, library)
        print_left_alone(decisions)
        changes = [decision for decision in decisions if is_change(decision)]
        if not changes:
            return True
        applied = apply_batch(changes, state, steam, tool)
    _save_state_changes(system.state_path, state, applied.state)
    return not applied.unconfirmed


# --- Reading and saving ----------------------------------------------------------


def _watch_service_library(unit_path: Path) -> PurePosixPath:
    unit = find_generated_unit(unit_path)
    library = None if unit is None else read_watch_service_library(unit.content)
    if library is None:
        raise KeepNativeError(
            f"No watch service was found at {unit_path} to take the library from. Pass the "
            "shared library folder (the one that contains steamapps) with --library."
        )
    return library


def _read_state(path: Path) -> SteamshareState:
    try:
        return read_state(path)
    except StateFileError as error:
        raise _unreadable_state_error(error) from error


def _save_state_changes(path: Path, before: SteamshareState, after: SteamshareState) -> None:
    try:
        save_state_changes(path, before=before, after=after)
    except StateFileError as error:
        raise _unreadable_state_error(error) from error
    except OSError as error:
        raise KeepNativeError(
            f"Could not write {error.filename}: {error.strerror}. Fix the cause and run "
            f"`{INSTALLED_COMMAND} keep-native` again."
        ) from error


def _unreadable_state_error(error: StateFileError) -> KeepNativeError:
    return KeepNativeError(
        f"Could not read steamshare's state ({error}). Fix the JSON by hand, or move "
        "the file away to start over; mappings steamshare made earlier will then "
        "count as yours."
    )


# --- Output ----------------------------------------------------------------------


def print_left_alone(decisions: Sequence[LiveDecision]) -> None:
    # Each changed game gets one line: the ones Steam changes are printed as
    # it confirms them (`apply_batch`), and these are the rest, such as a game
    # given back that is installed outside the shared library, whose working
    # install there is not replaced.
    for decision in decisions:
        if not is_change(decision):
            print(format_game_line(decision.action.value, decision, DECISION_COLUMN_WIDTH))


def _print_games_without_details(owned: OwnedGames) -> None:
    if owned.without_details:
        print(
            f"{len(owned.without_details)} games did not report their details and are not "
            "listed; run keep-native again in a moment to see them."
        )


def print_keep_native_warnings(games: Sequence[OwnedGame], library: PurePosixPath) -> None:
    if not games:
        return
    context = LiveContext(state=SteamshareState(), library=library)
    for game in games:
        print(f"Warning: {game.appid} {game.name} is kept native. {_why(game, context)}")
    print(SEPARATE_LIBRARY_HINT)


def _why(game: OwnedGame, context: LiveContext) -> str:
    if game.install_folder is not None and context.is_in_library(game.install_folder):
        return (
            "It is installed in the shared library, so Steam may now replace its Windows "
            "build with the Linux one there, and Windows can no longer run it."
        )
    return (
        "Once Linux Steam installs its Linux build into the shared library, Windows can "
        "no longer run that game."
    )
