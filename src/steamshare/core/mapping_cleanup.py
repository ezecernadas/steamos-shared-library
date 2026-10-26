"""Which of steamshare's Proton mappings `uninstall --purge` clears in Steam.

`watch` (and `force-proton`) map native Linux games to Proton so Steam
downloads their Windows build into the shared library, and remember those
games in the state file (`managed_mappings`). A purge removes that state, so
it first hands the mappings back to Steam, but only where that is safe:

- A game that is installed keeps its mapping. With the mapping gone, Steam
  would switch it to its Linux build and download that over the Windows
  files.
- A mapping the user changed since is theirs (`is_steamshares_mapping`), and
  one already gone needs nothing.

Steam does not fail on a change it ignores, so each clear is read back and
checked with the same rule `watch` uses (`is_change_confirmed`).

Pure: the adapters read the state file and talk to Steam.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from steamshare.core.live_proton_mapping import (
    CompatToolChange,
    CompatToolSetting,
    LiveDecision,
    OwnedGame,
    is_change_confirmed,
    is_steamshares_mapping,
    requested_change,
)
from steamshare.core.proton_mapping import MappingAction, SteamshareState

# The tool name is not used to clear a mapping: the request asks for no tool.
NO_TOOL_FOR_A_CLEAR = ""


@dataclass(frozen=True)
class MappingCleanup:
    """steamshare's mappings that are still in place, split by what happens to them."""

    to_clear: tuple[OwnedGame, ...]
    kept_installed: tuple[OwnedGame, ...]


def plan_mapping_cleanup(games: Iterable[OwnedGame], state: SteamshareState) -> MappingCleanup:
    still_ours = [game for game in games if is_steamshares_mapping(game, state)]
    return MappingCleanup(
        to_clear=tuple(game for game in still_ours if not game.installed),
        kept_installed=tuple(game for game in still_ours if game.installed),
    )


def clear_requests(cleanup: MappingCleanup) -> list[CompatToolChange]:
    return [requested_change(_clear(game), NO_TOOL_FOR_A_CLEAR) for game in cleanup.to_clear]


def unconfirmed_clears(
    cleanup: MappingCleanup, results: Mapping[str, CompatToolSetting | None]
) -> list[OwnedGame]:
    """Return the games Steam did not report as cleared; None means no report came."""
    return [game for game in cleanup.to_clear if not _is_cleared(game, results.get(game.appid))]


def _is_cleared(game: OwnedGame, after: CompatToolSetting | None) -> bool:
    if after is None:
        return False
    return is_change_confirmed(_clear(game), after, NO_TOOL_FOR_A_CLEAR)


def _clear(game: OwnedGame) -> LiveDecision:
    return LiveDecision(game=game, action=MappingAction.UNMAP)
