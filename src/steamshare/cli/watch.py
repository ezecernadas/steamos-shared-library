"""The `watch` command: force Proton live, so "Install" in Steam fetches the Windows build.

`force-proton` edits `config.vdf`, which only works with Steam closed. This
command instead runs while Steam is open, as a user service (see
`install-watch`): it asks the running client which games are native Linux
ones and maps them to Proton before the user installs them, through the same
API the game's Properties > Compatibility uses (`adapters.steam_client_api`).

Each pass gathers the owned games, plans with `core.live_proton_mapping`,
makes each change, checks that Steam took it, and records it in the state
file shared with `force-proton`. Between passes it sleeps; while Steam is
closed or starting, it waits and retries with a growing delay.

Every effect goes through `System`, so the tests run whole passes and the
loop with a fake Steam client.
"""

import argparse
import os
import signal
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from steamshare.adapters.state_file import (
    StateFileError,
    read_state,
    state_file_path,
    write_state,
)
from steamshare.adapters.steam_cdp import (
    SteamDebuggerError,
    SteamDebuggerUnavailableError,
    open_steam_debugger,
)
from steamshare.adapters.steam_client_api import OwnedGames, SteamClientApi
from steamshare.adapters.steam_config import DEFAULT_STEAM_ROOT
from steamshare.cli.shared import absolute_path
from steamshare.core.live_proton_mapping import (
    CompatToolSetting,
    LiveDecision,
    is_change,
    is_change_confirmed,
    plan_live_mappings,
    record_decision,
    requested_compat_tool,
)
from steamshare.core.proton_mapping import DEFAULT_PROTON_TOOL, MappingAction, SteamshareState

EXIT_OK = 0
EXIT_FAILURE = 1

# Five minutes: a game bought or added since the last pass gets mapped long
# before most users click "Install", and a pass costs Steam about 30 s of
# light work.
DEFAULT_INTERVAL_SECONDS = 300.0

# While Steam is closed or starting, retry after this, doubling each time up
# to the longest delay: quick to notice Steam starting, quiet while it is off.
FIRST_RETRY_SECONDS = 5.0
LONGEST_RETRY_SECONDS = 60.0

APPID_COLUMN_WIDTH = 8
CHANGE_COLUMN_WIDTH = 11
DECISION_COLUMN_WIDTH = 29

# How each change reads, before (dry run) and after it happened.
PLANNED_CHANGE_LABELS = {MappingAction.MAP: "would map", MappingAction.UNMAP: "would unmap"}
DONE_CHANGE_LABELS = {MappingAction.MAP: "mapped", MappingAction.UNMAP: "unmapped"}

# Why a change may not take, and what to do about it.
UNCONFIRMED_CHANGE_HINTS = {
    MappingAction.MAP: (
        "Steam falls back to its default for a tool it did not find when it started. "
        "Check the tool's exact name (such as proton_experimental) and pass it with "
        "--tool; if the tool was just installed, restart Steam."
    ),
    MappingAction.UNMAP: (
        "Clear the tool by hand in the game's Properties > Compatibility, then run watch again."
    ),
}


class SteamGameClient(Protocol):
    """What a pass needs from Steam; `SteamClientApi` in production."""

    def owned_games(self) -> OwnedGames: ...
    def specify_compat_tool(self, appid: str, tool: str) -> CompatToolSetting: ...


@dataclass(frozen=True)
class System:
    """Every effect `watch` has outside its arguments, replaceable in tests."""

    state_path: Path
    # Opens a connection to the running Steam client, closed on leaving.
    connect_to_steam: Callable[[], AbstractContextManager[SteamGameClient]]
    # Sleeps up to the given seconds; returns True when a stop was requested.
    wait: Callable[[float], bool]
    stop_requested: Callable[[], bool]


class WatchError(Exception):
    """An expected failure, with a message that says what to do next."""


def add_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subcommands.add_parser(
        "watch",
        help="while Steam runs, force Proton for native Linux games, so installing one "
        "into the shared library fetches its Windows build",
    )
    parser.add_argument(
        "--library",
        type=absolute_path,
        required=True,
        help="the shared Steam library folder (the one that contains steamapps)",
    )
    parser.add_argument(
        "--tool",
        default=DEFAULT_PROTON_TOOL,
        help=f"the compatibility tool to force (default: {DEFAULT_PROTON_TOOL})",
    )
    parser.add_argument(
        "--interval",
        type=positive_seconds,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between passes (default: {DEFAULT_INTERVAL_SECONDS:g})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one pass and exit; the exit status is non-zero if it failed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without changing anything",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also list every game left alone, and why",
    )
    parser.add_argument(
        "--steam-root",
        type=Path,
        default=DEFAULT_STEAM_ROOT,
        help="the Steam install folder (default: ~/.steam/steam; for the Flatpak, "
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam)",
    )
    parser.set_defaults(run=run)


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive number of seconds")
    return seconds


def run(arguments: argparse.Namespace) -> int:
    # Under systemd, stdout goes to the journal; flush each line so the log
    # shows a pass as it happens, not when a buffer fills.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    stop = threading.Event()
    # SIGTERM is how systemd stops the service, SIGINT is Ctrl+C. Either one
    # ends the current pass between two changes, and the loop then exits.
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda number, frame: stop.set())
    system = System(
        state_path=state_file_path(os.environ, Path.home()),
        connect_to_steam=lambda: connect_to_steam(arguments.steam_root),
        wait=stop.wait,
        stop_requested=stop.is_set,
    )
    return watch(arguments, system)


@contextmanager
def connect_to_steam(steam_root: Path) -> Iterator[SteamGameClient]:
    with open_steam_debugger(steam_root) as session:
        yield SteamClientApi(session)


def watch(arguments: argparse.Namespace, system: System) -> int:
    if arguments.once:
        try:
            all_confirmed = run_pass(arguments, system)
        except (WatchError, SteamDebuggerError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return EXIT_FAILURE
        return EXIT_OK if all_confirmed else EXIT_FAILURE
    watch_until_stopped(arguments, system)
    return EXIT_OK


def watch_until_stopped(arguments: argparse.Namespace, system: System) -> None:
    retry_delay: float | None = None  # None while Steam is reachable
    while not system.stop_requested():
        try:
            run_pass(arguments, system)
            if retry_delay is not None:
                print("Steam is reachable again.")
            retry_delay = None
            delay = arguments.interval
        except SteamDebuggerUnavailableError as error:
            if retry_delay is None:
                print(f"Waiting for Steam: {error}")
            retry_delay = next_retry_delay(retry_delay)
            delay = retry_delay
        except (WatchError, SteamDebuggerError) as error:
            print(f"Error: {error}", file=sys.stderr)
            delay = arguments.interval
        if system.wait(delay):
            break
    print("Stopped.")


def next_retry_delay(previous: float | None) -> float:
    if previous is None:
        return FIRST_RETRY_SECONDS
    return min(previous * 2, LONGEST_RETRY_SECONDS)


# --- One pass ------------------------------------------------------------------


def run_pass(arguments: argparse.Namespace, system: System) -> bool:
    """Plan and apply one pass; return False if Steam did not take a change."""
    state = _read_state(system.state_path)
    with system.connect_to_steam() as steam:
        owned = steam.owned_games()
        decisions = plan_live_mappings(owned.games, state, arguments.library)
        print(format_summary(decisions, owned.without_details, dry_run=arguments.dry_run))
        if arguments.verbose:
            print_left_alone(decisions)
        if arguments.dry_run:
            print_planned_changes(decisions)
            print("Dry run: nothing was changed.")
            return True
        return apply_decisions(decisions, state, steam, arguments.tool, system)


def apply_decisions(
    decisions: Sequence[LiveDecision],
    state: SteamshareState,
    steam: SteamGameClient,
    tool: str,
    system: System,
) -> bool:
    all_confirmed = True
    for decision in decisions:
        if system.stop_requested():
            break
        if is_change(decision) and not apply_change(decision, steam, tool):
            all_confirmed = False
            continue
        new_state = record_decision(state, decision)
        # Saved after each change, not at the end: if Steam closes mid-pass,
        # the mappings already made must still count as steamshare's, or a
        # later --keep-native could never remove them.
        if new_state != state:
            _write_state(system.state_path, new_state)
            state = new_state
    return all_confirmed


def apply_change(decision: LiveDecision, steam: SteamGameClient, tool: str) -> bool:
    """Ask Steam for the change, and return whether Steam now reports it."""
    requested = requested_compat_tool(decision, tool)
    after = steam.specify_compat_tool(decision.game.appid, requested)
    if is_change_confirmed(decision, after, tool):
        print(format_game_line(DONE_CHANGE_LABELS[decision.action], decision))
        return True
    reported = after.name or "no tool"
    print(
        f"Error: asked Steam to set {decision.game.appid} {decision.game.name} to "
        f"{requested or 'no tool'}, but it now reports {reported} (priority "
        f"{after.priority}). {UNCONFIRMED_CHANGE_HINTS[decision.action]}",
        file=sys.stderr,
    )
    return False


def _read_state(path: Path) -> SteamshareState:
    try:
        return read_state(path)
    except StateFileError as error:
        raise WatchError(
            f"Could not read steamshare's state ({error}). Fix the JSON by hand, or move "
            "the file away to start over; mappings steamshare made earlier will then "
            "count as yours."
        ) from error


def _write_state(path: Path, state: SteamshareState) -> None:
    try:
        write_state(path, state)
    except OSError as error:
        raise WatchError(
            f"Could not write {error.filename}: {error.strerror}. The change in Steam was "
            "made; fix the cause, or steamshare will treat that mapping as yours."
        ) from error


# --- Output --------------------------------------------------------------------


def format_summary(
    decisions: Sequence[LiveDecision], without_details: Sequence[str], *, dry_run: bool
) -> str:
    """One line: how many games got each decision, in the order the actions are listed."""
    counts = Counter(decision.action for decision in decisions)
    parts = [f"{counts[action]} {action.value}" for action in MappingAction if counts[action]]
    summary = f"{len(decisions)} games: {', '.join(parts) or 'none'}"
    if without_details:
        summary += f"; {len(without_details)} did not report their details (retried next pass)"
    return ("Dry run, " if dry_run else "") + summary + "."


def print_planned_changes(decisions: Sequence[LiveDecision]) -> None:
    for decision in decisions:
        if is_change(decision):
            print(format_game_line(PLANNED_CHANGE_LABELS[decision.action], decision))


def print_left_alone(decisions: Sequence[LiveDecision]) -> None:
    for decision in decisions:
        if not is_change(decision):
            print(format_game_line(decision.action.value, decision, DECISION_COLUMN_WIDTH))


def format_game_line(
    label: str, decision: LiveDecision, label_width: int = CHANGE_COLUMN_WIDTH
) -> str:
    game = decision.game
    return f"  {label:<{label_width}} {game.appid:>{APPID_COLUMN_WIDTH}}  {game.name}"
