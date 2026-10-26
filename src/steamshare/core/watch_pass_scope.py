"""Choose which games a `watch` pass reads the details of.

Why this exists: a game's compatibility tool is only in its details, and
reading the details of every owned game costs Steam a few seconds per pass.
Most passes find nothing new, so each pass first takes a cheap snapshot (each
game's appid and installed flag, from the app overviews Steam already holds)
and compares it with the previous pass's:

- nothing changed: the pass reads no details and changes nothing;
- some games are new, or were installed or uninstalled: only those are read.

The snapshot cannot see everything, so some passes are still full (every
game's details). The triggers are in `FULL_PASS_TRIGGERS`; to add one, write a
function with the `FullPassTrigger` signature and add it to the tuple.

This module is pure: `cli.watch` keeps the memory between passes and the
adapters take the snapshot.
"""

from collections.abc import Callable
from dataclasses import dataclass

from steamshare.core.proton_mapping import SteamshareState


@dataclass(frozen=True)
class GameOverview:
    """What Steam's app overviews say about one game, without its details."""

    appid: str
    installed: bool


@dataclass(frozen=True)
class LibrarySnapshot:
    """Every owned game's overview, taken at the start of a pass."""

    # When Steam's UI page started (`performance.timeOrigin`); a new value
    # means Steam restarted since the last pass.
    client_session: float
    games: frozenset[GameOverview]

    def without(self, appids: frozenset[str]) -> "LibrarySnapshot":
        kept = frozenset(game for game in self.games if game.appid not in appids)
        return LibrarySnapshot(client_session=self.client_session, games=kept)


@dataclass(frozen=True)
class PassMemory:
    """What `watch` remembers from its last finished pass."""

    snapshot: LibrarySnapshot
    # The state file as that pass left it.
    state: SteamshareState
    # When the last full pass ran, on the same clock as `now`.
    last_full_pass_at: float


@dataclass(frozen=True)
class PassScope:
    """Which games a pass reads the details of: all of them, or only `appids`."""

    full: bool
    appids: frozenset[str] = frozenset()

    @classmethod
    def everything(cls) -> "PassScope":
        return cls(full=True)

    def is_empty(self) -> bool:
        return not self.full and not self.appids


@dataclass(frozen=True)
class PassContext:
    """What the full-pass triggers look at."""

    memory: PassMemory
    snapshot: LibrarySnapshot
    state: SteamshareState
    now: float
    full_interval: float


# --- When a pass must read every game ------------------------------------------

# A trigger returns True when the snapshot alone cannot be trusted this pass.
FullPassTrigger = Callable[[PassContext], bool]


def steam_restarted(context: PassContext) -> bool:
    # A restart reloads Steam's settings, and the user may have changed a tool
    # while Steam was closed (`force-proton`, or Windows Steam on the same
    # account), so every game is read again.
    return context.snapshot.client_session != context.memory.snapshot.client_session


def state_file_changed(context: PassContext) -> bool:
    # Someone edited the state since the last pass (such as
    # `force-proton --keep-native`); the decisions it affects may be on any
    # game, not only the ones whose overview changed.
    return context.state != context.memory.state


def full_interval_elapsed(context: PassContext) -> bool:
    # The user can change a game's tool in Properties > Compatibility without
    # changing its overview, and rules such as "managed, but taken over by the
    # user" must see that eventually. Reading everything now and then catches
    # it, at the cost of one full pass per interval.
    return context.now - context.memory.last_full_pass_at >= context.full_interval


FULL_PASS_TRIGGERS: tuple[FullPassTrigger, ...] = (
    steam_restarted,
    state_file_changed,
    full_interval_elapsed,
)


# --- Planning and remembering a pass -------------------------------------------


def plan_pass_scope(
    memory: PassMemory | None,
    snapshot: LibrarySnapshot,
    state: SteamshareState,
    *,
    now: float,
    full_interval: float,
) -> PassScope:
    """Read every game on the first pass or on a trigger; otherwise only the changed ones."""
    if memory is None:
        return PassScope.everything()
    context = PassContext(memory, snapshot, state, now, full_interval)
    if any(trigger(context) for trigger in FULL_PASS_TRIGGERS):
        return PassScope.everything()
    # A game no longer owned is simply gone from the snapshot; it needs nothing.
    changed = snapshot.games - memory.snapshot.games
    return PassScope(full=False, appids=frozenset(game.appid for game in changed))


def remember_pass(
    memory: PassMemory | None,
    snapshot: LibrarySnapshot,
    state: SteamshareState,
    scope: PassScope,
    *,
    retry_appids: frozenset[str],
    now: float,
) -> PassMemory:
    """Return the memory after a finished pass.

    `retry_appids` are the games the pass could not finish (details that did
    not arrive, changes Steam did not confirm). They are left out of the
    remembered snapshot, so the next pass sees them as changed and tries again.
    """
    last_full_pass_at = now if scope.full or memory is None else memory.last_full_pass_at
    return PassMemory(
        snapshot=snapshot.without(retry_appids),
        state=state,
        last_full_pass_at=last_full_pass_at,
    )
