"""Which games the `keep-native` picker lists, and what the user's choice changes.

Why this exists: keeping a game native (`keep_native` in the state) tells
steamshare never to force Proton on it, so Steam installs its Linux build.
`force-proton --keep-native APPID` does that with Steam closed and an appid
the user must look up; `keep-native` instead lists the games by name, from
the running Steam client, and lets the user tick them.

A game is listed when steamshare has a say in its tool: Steam would run it
natively, or steamshare mapped it to Proton, or it is kept native already
(so it can be given back). Whatever `live_proton_mapping` never touches is
left out, by the same rules `watch` uses (`NOT_STEAMSHARES_RULES`).

This module is pure: `cli.keep_native` asks Steam and saves the state.
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from steamshare.core.live_proton_mapping import (
    LiveContext,
    LiveDecisionRule,
    OwnedGame,
    has_user_mapping,
    has_valve_override,
    is_linux_tool,
    is_steamworks_redistributable,
)
from steamshare.core.proton_mapping import SteamshareState, allow_proton, mark_keep_native

# The decision rules that leave a game alone whatever the state says: Valve's
# and the user's choices, and apps that are not games. Keeping one of these
# native, or not, would change nothing, so the picker does not list them.
NOT_STEAMSHARES_RULES: tuple[LiveDecisionRule, ...] = (
    is_steamworks_redistributable,
    is_linux_tool,
    has_valve_override,
    has_user_mapping,
)


def keep_native_candidates(
    games: Iterable[OwnedGame], state: SteamshareState, library: PurePosixPath
) -> tuple[OwnedGame, ...]:
    """Return the games to list, sorted by name as a person reads it."""
    context = LiveContext(state=state, library=library)
    candidates = [game for game in games if _is_candidate(game, context)]
    return tuple(sorted(candidates, key=lambda game: (game.name.casefold(), game.appid)))


def _is_candidate(game: OwnedGame, context: LiveContext) -> bool:
    if any(rule(game, context) is not None for rule in NOT_STEAMSHARES_RULES):
        return False
    return (
        game.compat_tool.is_native_linux()
        or context.is_steamshares_mapping(game)
        or game.appid in context.state.keep_native
    )


@dataclass(frozen=True)
class KeepNativeChange:
    """What the user's ticks change, in the candidates' order."""

    # Ticked now, not kept native before.
    newly_kept: tuple[OwnedGame, ...] = ()
    # Kept native before, unticked now: Proton may be forced on them again.
    given_back: tuple[OwnedGame, ...] = ()

    def is_empty(self) -> bool:
        return not self.newly_kept and not self.given_back


def plan_keep_native_change(
    candidates: Sequence[OwnedGame], state: SteamshareState, checked: Collection[str]
) -> KeepNativeChange:
    """Compare the ticked appids with the state, for the listed games only."""
    return KeepNativeChange(
        newly_kept=tuple(
            game
            for game in candidates
            if game.appid in checked and game.appid not in state.keep_native
        ),
        given_back=tuple(
            game
            for game in candidates
            if game.appid not in checked and game.appid in state.keep_native
        ),
    )


def with_keep_native_change(state: SteamshareState, change: KeepNativeChange) -> SteamshareState:
    # Kept-native appids the picker did not list (a game no longer owned)
    # are not in the change, so they stay as they were.
    state = mark_keep_native(state, (game.appid for game in change.newly_kept))
    return allow_proton(state, (game.appid for game in change.given_back))
