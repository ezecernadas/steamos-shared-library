"""Ask the running Steam client for tool changes, and keep only the ones it confirms.

Shared by `watch`, which maps games in batches all day, and `keep-native`,
which applies the user's choice at once. Both plan with
`core.live_proton_mapping`; this module sends a planned batch to Steam,
checks what Steam reports afterwards, prints a line per game, and returns
the state with only the confirmed changes recorded.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from steamshare.core.live_proton_mapping import (
    CompatToolChange,
    CompatToolSetting,
    LiveDecision,
    is_change_confirmed,
    record_decision,
    requested_change,
)
from steamshare.core.proton_mapping import MappingAction, SteamshareState

APPID_COLUMN_WIDTH = 8
CHANGE_COLUMN_WIDTH = 11

# How each change reads once it happened.
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


class CompatToolClient(Protocol):
    """What applying changes needs from Steam; `SteamClientApi` in production."""

    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]: ...


@dataclass(frozen=True)
class AppliedBatch:
    # The state with every confirmed change recorded.
    state: SteamshareState
    # Games whose change Steam did not confirm.
    unconfirmed: frozenset[str] = frozenset()


def apply_batch(
    batch: Sequence[LiveDecision], state: SteamshareState, steam: CompatToolClient, tool: str
) -> AppliedBatch:
    """Ask Steam for the batch's changes; record each one Steam now reports."""
    requests = {decision.game.appid: requested_change(decision, tool) for decision in batch}
    reported = steam.specify_compat_tools(list(requests.values()))
    unconfirmed = set()
    for decision in batch:
        appid = decision.game.appid
        after = reported.get(appid)
        if after is not None and is_change_confirmed(decision, after, tool):
            print(format_game_line(DONE_CHANGE_LABELS[decision.action], decision))
            state = record_decision(state, decision)
        else:
            print_unconfirmed_change(decision, requests[appid], after)
            unconfirmed.add(appid)
    return AppliedBatch(state, unconfirmed=frozenset(unconfirmed))


def print_unconfirmed_change(
    decision: LiveDecision, requested: CompatToolChange, after: CompatToolSetting | None
) -> None:
    if after is None:
        reported = "did not report its details afterwards"
    else:
        reported = f"now reports {after.name or 'no tool'} (priority {after.priority})"
    print(
        f"Error: asked Steam to set {decision.game.appid} {decision.game.name} to "
        f"{requested.tool or 'no tool'}, but it {reported}. "
        f"{UNCONFIRMED_CHANGE_HINTS[decision.action]}",
        file=sys.stderr,
    )


def format_game_line(
    label: str, decision: LiveDecision, label_width: int = CHANGE_COLUMN_WIDTH
) -> str:
    game = decision.game
    return f"  {label:<{label_width}} {game.appid:>{APPID_COLUMN_WIDTH}}  {game.name}"
