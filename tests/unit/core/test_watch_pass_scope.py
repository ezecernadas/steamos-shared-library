"""Tests for choosing which games a `watch` pass reads the details of.

A full pass reads every game's details, which costs Steam a few seconds. A
cheap snapshot (each game's appid and installed flag) tells which games
changed since the last pass, so most passes read only those, or none.
"""

from steamshare.core.proton_mapping import SteamshareState
from steamshare.core.watch_pass_scope import (
    GameOverview,
    LibrarySnapshot,
    PassMemory,
    PassScope,
    plan_pass_scope,
    remember_pass,
)

FULL_INTERVAL = 3600.0
NOW = 10_000.0
STEAM_SESSION = 1793032431065.8

LIMBO = GameOverview(appid="48000", installed=False)
BIGFOOT = GameOverview(appid="509980", installed=False)
NO_STATE = SteamshareState()


def snapshot(*games: GameOverview, session: float = STEAM_SESSION) -> LibrarySnapshot:
    return LibrarySnapshot(client_session=session, games=frozenset(games))


def memory(
    previous: LibrarySnapshot,
    *,
    state: SteamshareState = NO_STATE,
    last_full_pass_at: float = NOW - 60,
) -> PassMemory:
    return PassMemory(snapshot=previous, state=state, last_full_pass_at=last_full_pass_at)


def scope_for(
    previous: PassMemory | None,
    current: LibrarySnapshot,
    state: SteamshareState = NO_STATE,
) -> PassScope:
    return plan_pass_scope(previous, current, state, now=NOW, full_interval=FULL_INTERVAL)


def test_the_first_pass_is_full() -> None:
    assert scope_for(None, snapshot(LIMBO)).full


def test_an_unchanged_library_needs_no_details() -> None:
    scope = scope_for(memory(snapshot(LIMBO, BIGFOOT)), snapshot(LIMBO, BIGFOOT))

    assert not scope.full
    assert scope.is_empty()


def test_a_new_game_is_the_only_one_read() -> None:
    scope = scope_for(memory(snapshot(LIMBO)), snapshot(LIMBO, BIGFOOT))

    assert scope == PassScope(full=False, appids=frozenset({"509980"}))


def test_a_game_installed_since_the_last_pass_is_read_again() -> None:
    installed_limbo = GameOverview(appid="48000", installed=True)

    scope = scope_for(memory(snapshot(LIMBO, BIGFOOT)), snapshot(installed_limbo, BIGFOOT))

    assert scope.appids == frozenset({"48000"})


def test_a_game_no_longer_owned_needs_nothing() -> None:
    assert scope_for(memory(snapshot(LIMBO, BIGFOOT)), snapshot(LIMBO)).is_empty()


def test_a_steam_restart_makes_the_pass_full() -> None:
    restarted = snapshot(LIMBO, session=STEAM_SESSION + 1)

    assert scope_for(memory(snapshot(LIMBO)), restarted).full


def test_a_state_file_edited_since_the_last_pass_makes_the_pass_full() -> None:
    kept_native = SteamshareState(keep_native=frozenset({"48000"}))

    assert scope_for(memory(snapshot(LIMBO)), snapshot(LIMBO), kept_native).full


def test_the_pass_is_full_again_once_the_full_interval_has_passed() -> None:
    stale = memory(snapshot(LIMBO), last_full_pass_at=NOW - FULL_INTERVAL)

    assert scope_for(stale, snapshot(LIMBO)).full


def test_a_full_pass_is_remembered_with_its_time() -> None:
    current = snapshot(LIMBO, BIGFOOT)

    remembered = remember_pass(
        None, current, NO_STATE, PassScope.everything(), retry_appids=frozenset(), now=NOW
    )

    assert remembered == PassMemory(snapshot=current, state=NO_STATE, last_full_pass_at=NOW)


def test_a_partial_pass_keeps_the_time_of_the_last_full_one() -> None:
    previous = memory(snapshot(LIMBO), last_full_pass_at=NOW - 600)
    current = snapshot(LIMBO, BIGFOOT)
    partial = PassScope(full=False, appids=frozenset({"509980"}))

    remembered = remember_pass(
        previous, current, NO_STATE, partial, retry_appids=frozenset(), now=NOW
    )

    assert remembered.last_full_pass_at == NOW - 600
    assert remembered.snapshot == current


def test_a_game_to_retry_is_forgotten_so_the_next_pass_reads_it_again() -> None:
    current = snapshot(LIMBO, BIGFOOT)

    remembered = remember_pass(
        None,
        current,
        NO_STATE,
        PassScope.everything(),
        retry_appids=frozenset({"509980"}),
        now=NOW,
    )

    assert scope_for(remembered, current) == PassScope(full=False, appids=frozenset({"509980"}))
