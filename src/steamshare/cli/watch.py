"""The `watch` command: force Proton live, so "Install" in Steam fetches the Windows build.

`force-proton` edits `config.vdf`, which only works with Steam closed. This
command instead runs while Steam is open, as a user service that `install`
sets up (see `watch_service_setup`): it asks the running client which games
are native Linux ones and maps them to Proton before the user installs them,
through the same API the game's Properties > Compatibility uses
(`adapters.steam_client_api`).

Each pass first takes a cheap snapshot of the library, and from it
`core.watch_pass_scope` decides which games' details to read: every game on
the first pass, after Steam restarts or comes back, and every
`--full-interval`; otherwise only the games that changed, which is usually
none, so the pass ends there silently. It then plans with
`core.live_proton_mapping`, asks Steam for the changes in batches, checks that
Steam took each one, and records the confirmed ones in the state file shared
with `force-proton`. Between passes it sleeps; while Steam is closed or
starting, it waits and retries with a growing delay.

Every effect goes through `System`, so the tests run whole passes and the
loop with a fake Steam client.
"""

import argparse
import os
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from steamshare.adapters.state_file import (
    StateFileError,
    read_state,
    save_state_changes,
    state_file_path,
)
from steamshare.adapters.steam_cdp import (
    SteamDebuggerError,
    SteamDebuggerUnavailableError,
)
from steamshare.adapters.steam_client_api import OwnedGames, connect_to_steam_client
from steamshare.adapters.steam_config import DEFAULT_STEAM_ROOT
from steamshare.cli.live_changes import apply_batch, format_game_line
from steamshare.cli.shared import absolute_path
from steamshare.core.live_proton_mapping import (
    CompatToolChange,
    CompatToolSetting,
    LiveDecision,
    is_change,
    plan_live_mappings,
    record_decision,
)
from steamshare.core.proton_mapping import DEFAULT_PROTON_TOOL, MappingAction, SteamshareState
from steamshare.core.watch_pass_scope import (
    LibrarySnapshot,
    PassMemory,
    PassScope,
    plan_pass_scope,
    remember_pass,
)

EXIT_OK = 0
EXIT_FAILURE = 1

# Five minutes: a game bought or added since the last pass gets mapped long
# before most users click "Install". A pass that finds nothing new costs Steam
# one instant snapshot.
DEFAULT_INTERVAL_SECONDS = 300.0
# An hour: how long a tool the user changed in Properties > Compatibility can
# go unnoticed, since the snapshot does not show it (see
# `core.watch_pass_scope`). A full pass reads every game, a few seconds of
# Steam's time.
DEFAULT_FULL_INTERVAL_SECONDS = 3600.0

# Changes asked of Steam in one script. Each batch costs one settle wait, and
# the state is saved after each one, so a pass stopped midway keeps what it
# did; 50 keeps one script to a few seconds.
CHANGES_PER_BATCH = 50

# While Steam is closed or starting, retry after this, doubling each time up
# to the longest delay: quick to notice Steam starting, quiet while it is off.
FIRST_RETRY_SECONDS = 5.0
LONGEST_RETRY_SECONDS = 60.0

DECISION_COLUMN_WIDTH = 29

# How each change reads in a dry run, before it happens.
PLANNED_CHANGE_LABELS = {MappingAction.MAP: "would map", MappingAction.UNMAP: "would unmap"}


class SteamGameClient(Protocol):
    """What a pass needs from Steam; `SteamClientApi` in production."""

    def library_snapshot(self) -> LibrarySnapshot: ...
    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames: ...
    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]: ...


@dataclass(frozen=True)
class System:
    """Every effect `watch` has outside its arguments, replaceable in tests."""

    state_path: Path
    # Opens a connection to the running Steam client, closed on leaving.
    connect_to_steam: Callable[[], AbstractContextManager[SteamGameClient]]
    # Sleeps up to the given seconds; returns True when a stop was requested.
    wait: Callable[[float], bool]
    stop_requested: Callable[[], bool]
    # Seconds on a clock that only moves forward, to time full passes.
    clock: Callable[[], float]


@dataclass(frozen=True)
class PassOutcome:
    # False if Steam did not confirm a change.
    all_confirmed: bool
    # What the next pass compares its snapshot with.
    memory: PassMemory


@dataclass(frozen=True)
class AppliedChanges:
    state: SteamshareState
    # Games whose change Steam did not confirm: the pass failed.
    unconfirmed: frozenset[str] = frozenset()
    # Games whose change was not asked for: a dry run, or a stop request.
    not_attempted: frozenset[str] = frozenset()

    def to_retry(self) -> frozenset[str]:
        return self.unconfirmed | self.not_attempted


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
        "--full-interval",
        type=positive_seconds,
        default=DEFAULT_FULL_INTERVAL_SECONDS,
        help="seconds between passes that read every game, to notice tools changed by hand "
        f"in Steam (default: {DEFAULT_FULL_INTERVAL_SECONDS:g})",
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
    # ends the current pass between two batches of changes, and the loop
    # then exits.
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda number, frame: stop.set())
    system = System(
        state_path=state_file_path(os.environ, Path.home()),
        connect_to_steam=lambda: connect_to_steam_client(arguments.steam_root),
        wait=stop.wait,
        stop_requested=stop.is_set,
        clock=time.monotonic,
    )
    return watch(arguments, system)


def watch(arguments: argparse.Namespace, system: System) -> int:
    if arguments.once:
        try:
            outcome = run_pass(arguments, system, memory=None)
        except (WatchError, SteamDebuggerError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return EXIT_FAILURE
        return EXIT_OK if outcome.all_confirmed else EXIT_FAILURE
    watch_until_stopped(arguments, system)
    return EXIT_OK


def watch_until_stopped(arguments: argparse.Namespace, system: System) -> None:
    retry_delay: float | None = None  # None while Steam is reachable
    # Forgotten after any failed pass, so the first pass after Steam comes
    # back, or after an error, reads every game.
    memory: PassMemory | None = None
    while not system.stop_requested():
        try:
            memory = run_pass(arguments, system, memory).memory
            if retry_delay is not None:
                print("Steam is reachable again.")
            retry_delay = None
            delay = arguments.interval
        except SteamDebuggerUnavailableError as error:
            memory = None
            if retry_delay is None:
                print(f"Waiting for Steam: {error}")
            retry_delay = next_retry_delay(retry_delay)
            delay = retry_delay
        except (WatchError, SteamDebuggerError) as error:
            memory = None
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


def run_pass(
    arguments: argparse.Namespace, system: System, memory: PassMemory | None
) -> PassOutcome:
    """Plan and apply one pass over the games that need it; remember it for the next."""
    state = _read_state(system.state_path)
    now = system.clock()
    with system.connect_to_steam() as steam:
        snapshot = steam.library_snapshot()
        scope = plan_pass_scope(
            memory, snapshot, state, now=now, full_interval=arguments.full_interval
        )
        if scope.is_empty():
            # Nothing changed since the last pass; most passes end here, silently.
            unchanged = remember_pass(
                memory, snapshot, state, scope, retry_appids=frozenset(), now=now
            )
            return PassOutcome(all_confirmed=True, memory=unchanged)
        owned = steam.owned_games(None if scope.full else scope.appids)
        decisions = plan_live_mappings(owned.games, state, arguments.library)
        print_plan(decisions, owned, scope, arguments)
        if arguments.dry_run:
            applied = AppliedChanges(state, not_attempted=changed_appids(decisions))
        else:
            applied = apply_decisions(decisions, state, steam, arguments.tool, system)
    # Games left unfinished are read again on the next pass.
    retry_appids = applied.to_retry() | frozenset(owned.without_details)
    remembered = remember_pass(
        memory, snapshot, applied.state, scope, retry_appids=retry_appids, now=now
    )
    return PassOutcome(all_confirmed=not applied.unconfirmed, memory=remembered)


def print_plan(
    decisions: Sequence[LiveDecision],
    owned: OwnedGames,
    scope: PassScope,
    arguments: argparse.Namespace,
) -> None:
    print(format_summary(decisions, owned.without_details, scope, dry_run=arguments.dry_run))
    if arguments.verbose:
        print_left_alone(decisions)
    if arguments.dry_run:
        print_planned_changes(decisions)
        print("Dry run: nothing was changed.")


def changed_appids(decisions: Sequence[LiveDecision]) -> frozenset[str]:
    return frozenset(decision.game.appid for decision in decisions if is_change(decision))


def apply_decisions(
    decisions: Sequence[LiveDecision],
    state: SteamshareState,
    steam: SteamGameClient,
    tool: str,
    system: System,
) -> AppliedChanges:
    # Decisions that change nothing in Steam may still change the state (a
    # mapping the user took over is forgotten); they take effect at once.
    state = _save_if_changed(system.state_path, state, record_left_alone(decisions, state))
    changes = [decision for decision in decisions if is_change(decision)]
    unconfirmed: set[str] = set()
    not_attempted = set(changed_appids(changes))
    for batch in split_into_batches(changes, CHANGES_PER_BATCH):
        if system.stop_requested():
            break
        applied = apply_batch(batch, state, steam, tool)
        unconfirmed |= applied.unconfirmed
        not_attempted -= changed_appids(batch)
        # Saved after each batch, not at the end: if Steam closes mid-pass,
        # the mappings already made must still count as steamshare's, or a
        # later --keep-native could never remove them.
        state = _save_if_changed(system.state_path, state, applied.state)
    return AppliedChanges(state, frozenset(unconfirmed), frozenset(not_attempted))


def record_left_alone(decisions: Sequence[LiveDecision], state: SteamshareState) -> SteamshareState:
    for decision in decisions:
        if not is_change(decision):
            state = record_decision(state, decision)
    return state


def split_into_batches(
    changes: Sequence[LiveDecision], batch_size: int
) -> list[Sequence[LiveDecision]]:
    return [changes[start : start + batch_size] for start in range(0, len(changes), batch_size)]


def _save_if_changed(
    path: Path, saved: SteamshareState, new_state: SteamshareState
) -> SteamshareState:
    # Only this pass's own changes are saved: `keep-native` may have saved
    # the file since the pass read it, and that must not be undone. The pass
    # goes on with its own view; the next one reads the file again.
    if new_state != saved:
        _save_state_changes(path, saved, new_state)
    return new_state


def _read_state(path: Path) -> SteamshareState:
    try:
        return read_state(path)
    except StateFileError as error:
        raise WatchError(_unreadable_state_message(error)) from error


def _save_state_changes(path: Path, before: SteamshareState, after: SteamshareState) -> None:
    try:
        save_state_changes(path, before=before, after=after)
    except StateFileError as error:
        # Saving reads the file again, and someone may have broken it since.
        raise WatchError(_unreadable_state_message(error)) from error
    except OSError as error:
        raise WatchError(
            f"Could not write {error.filename}: {error.strerror}. The change in Steam was "
            "made; fix the cause, or steamshare will treat that mapping as yours."
        ) from error


def _unreadable_state_message(error: StateFileError) -> str:
    return (
        f"Could not read steamshare's state ({error}). Fix the JSON by hand, or move "
        "the file away to start over; mappings steamshare made earlier will then "
        "count as yours."
    )


# --- Output --------------------------------------------------------------------


def format_summary(
    decisions: Sequence[LiveDecision],
    without_details: Sequence[str],
    scope: PassScope,
    *,
    dry_run: bool,
) -> str:
    """One line: how many games got each decision, in the order the actions are listed."""
    counts = Counter(decision.action for decision in decisions)
    parts = [f"{counts[action]} {action.value}" for action in MappingAction if counts[action]]
    games = "games" if scope.full else "changed games"
    summary = f"{len(decisions)} {games}: {', '.join(parts) or 'none'}"
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
