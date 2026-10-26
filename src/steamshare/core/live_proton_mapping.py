"""Decide which games to map to Proton, from what the running Steam client reports.

Why this exists: Linux Steam downloads the Linux build of any game that has
one, and in the shared NTFS library that breaks the game for Windows. Forcing
Proton for the game *before* it is installed makes Steam download the Windows
build instead. `force-proton` does this through `config.vdf`, which only works
while Steam is closed. `watch` does it live, through the client's own API, so
the user can just click "Install" (see `adapters.steam_client_api`).

The live client tells more than `config.vdf`: which tool Steam itself picked
for each game, and with what priority. The priorities say who made the
choice, and the rules below use them to leave Valve's and the user's choices
alone. The rules are in `LIVE_DECISION_RULES`; to add one, write a function
with the `LiveDecisionRule` signature and add it to the tuple.

The state (`keep_native`, `managed_mappings`) is the same one `force-proton`
uses, so both commands agree on which mappings are steamshare's.

This module is pure: the adapters talk to Steam and the state file.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from steamshare.core.library_layout import (
    COMMON_DIRECTORY,
    LINUX_TOOL_FOLDER_PREFIXES,
    STEAMAPPS_DIRECTORY,
)
from steamshare.core.proton_mapping import (
    STEAMWORKS_COMMON_REDISTRIBUTABLES_APPID,
    MappingAction,
    SteamshareState,
)

# --- What Steam's compatibility tool settings mean -----------------------------
#
# Observed on 2026-10-26 by reading `nCompatToolPriority` and
# `strCompatToolName` for all 615 owned games of a real account. Steam does
# not document them.

# The tool name Steam reports, and the one to request, for "no tool": the game
# runs natively, without even the Steam Linux Runtime.
NO_COMPAT_TOOL = ""

# Steam's own tool names start with these. A Proton tool runs the Windows build;
# a Steam Linux Runtime tool runs a native Linux build in a container.
PROTON_TOOL_PREFIX = "proton"
STEAM_LINUX_RUNTIME_TOOL_PREFIX = "steamlinuxruntime"

# A native Linux game with no tool at all (LIMBO, Human Fall Flat).
NO_COMPAT_TOOL_PRIORITY = 0
# Steam's default choice: `proton_*` for a Windows-only game, and
# `steamlinuxruntime*` for a native Linux one.
STEAM_DEFAULT_PRIORITY = 75
# The lowest priority seen for Valve's per-title choice (90 to 100: Counter-
# Strike 2 runs native for its anti-cheat; Baldur's Gate 3, Half-Life: Alyx,
# and Hollow Knight are pinned to Proton). Everything from here up to the
# user's priority is Valve's, and is never overridden.
VALVE_OVERRIDE_PRIORITY = 90
# A mapping chosen in Properties > Compatibility, or set by `SpecifyCompatTool`
# (which is what steamshare does): the same as `force-proton`'s `"250"`.
USER_MAPPING_PRIORITY = 250


@dataclass(frozen=True)
class CompatToolSetting:
    """The tool Steam will run a game with, and the priority of that choice."""

    name: str
    priority: int

    def is_mapping(self) -> bool:
        """Whether a user (or steamshare) chose the tool, rather than Steam or Valve."""
        return self.priority >= USER_MAPPING_PRIORITY

    def is_valve_override(self) -> bool:
        return VALVE_OVERRIDE_PRIORITY <= self.priority < USER_MAPPING_PRIORITY

    def is_native_linux(self) -> bool:
        """Whether Steam would run the game's Linux build if left alone."""
        runs_linux_build = self.name == NO_COMPAT_TOOL or self.name.startswith(
            STEAM_LINUX_RUNTIME_TOOL_PREFIX
        )
        return runs_linux_build and self.priority < VALVE_OVERRIDE_PRIORITY

    def is_windows_only_default(self) -> bool:
        # Steam already picked Proton by itself, so it downloads the Windows
        # build with no help.
        return self.name.startswith(PROTON_TOOL_PREFIX) and self.priority == STEAM_DEFAULT_PRIORITY


@dataclass(frozen=True)
class OwnedGame:
    """One game the account owns, as the running Steam client reports it."""

    appid: str  # a string, as in config.vdf and the state file
    name: str
    installed: bool
    # The game's own folder, such as <library>/steamapps/common/Limbo; None
    # when Steam reports none, which it does for games that are not installed.
    install_folder: PurePosixPath | None
    compat_tool: CompatToolSetting


@dataclass(frozen=True)
class LiveDecision:
    """What to do with one game; `action.value` says why, for printing."""

    game: OwnedGame
    action: MappingAction


def is_steamshares_mapping(game: OwnedGame, state: SteamshareState) -> bool:
    """Tell whether the game's current mapping is still the one steamshare made."""
    # A managed appid whose mapping now names a non-Proton tool was changed
    # by the user in Steam, so it is theirs now.
    tool = game.compat_tool
    return (
        game.appid in state.managed_mappings
        and tool.is_mapping()
        and tool.name.startswith(PROTON_TOOL_PREFIX)
    )


@dataclass(frozen=True)
class LiveContext:
    """What the rules need to know besides the game itself."""

    state: SteamshareState
    library: PurePosixPath  # the shared library folder, which contains steamapps

    def is_steamshares_mapping(self, game: OwnedGame) -> bool:
        return is_steamshares_mapping(game, self.state)

    def is_in_library(self, folder: PurePosixPath) -> bool:
        return folder.is_relative_to(self.library / STEAMAPPS_DIRECTORY / COMMON_DIRECTORY)


# --- Decision rules ------------------------------------------------------------

# A rule returns the action for the game when it applies, or None to let the
# next rule decide.
LiveDecisionRule = Callable[[OwnedGame, LiveContext], MappingAction | None]


def is_steamworks_redistributable(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    if game.appid == STEAMWORKS_COMMON_REDISTRIBUTABLES_APPID:
        return MappingAction.SKIPPED_TOOL
    return None


def is_linux_tool(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    # Only games are gathered, so Proton and the runtimes should never get
    # here; the check guards against Steam ever listing one as a game.
    folder = game.install_folder
    if folder is not None and folder.name.startswith(LINUX_TOOL_FOLDER_PREFIXES):
        return MappingAction.SKIPPED_TOOL
    return None


def has_valve_override(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    if game.compat_tool.is_valve_override():
        return MappingAction.SKIPPED_VALVE_OVERRIDE
    return None


def has_user_mapping(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    if game.compat_tool.is_mapping() and not context.is_steamshares_mapping(game):
        return MappingAction.SKIPPED_USER_MAPPING
    return None


def is_kept_native(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    if game.appid not in context.state.keep_native:
        return None
    if context.is_steamshares_mapping(game):
        return MappingAction.UNMAP
    return MappingAction.SKIPPED_KEEP_NATIVE


def has_steamshare_mapping(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    if context.is_steamshares_mapping(game):
        return MappingAction.ALREADY_MAPPED
    return None


def is_windows_only(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    if game.compat_tool.is_windows_only_default():
        return MappingAction.WINDOWS_ONLY
    return None


def has_unrecognized_tool(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    # Safe by default: a setting that matches none of the observed meanings
    # above is left alone rather than guessed at.
    if not game.compat_tool.is_native_linux():
        return MappingAction.SKIPPED_UNRECOGNIZED_TOOL
    return None


def is_installed_outside_library(game: OwnedGame, context: LiveContext) -> MappingAction | None:
    # IMPORTANT: a native game already installed somewhere else (such as an
    # ext4 library) works. Forcing Proton on it would make Steam throw that
    # install away and download the Windows build in its place. Only installs
    # in the shared library, where the Linux build breaks Windows, are mapped.
    # An installed game whose folder Steam does not report is treated the same.
    if not game.installed:
        return None
    folder = game.install_folder
    if folder is None or not context.is_in_library(folder):
        return MappingAction.SKIPPED_OUTSIDE_LIBRARY
    return None


# Checked in order; the first rule that returns an action decides. Valve's
# and the user's choices come first, so nothing below can override them; keep
# native comes before "already mapped", so it can remove steamshare's mapping.
LIVE_DECISION_RULES: tuple[LiveDecisionRule, ...] = (
    is_steamworks_redistributable,
    is_linux_tool,
    has_valve_override,
    has_user_mapping,
    is_kept_native,
    has_steamshare_mapping,
    is_windows_only,
    has_unrecognized_tool,
    is_installed_outside_library,
)


def decide_live_action(game: OwnedGame, context: LiveContext) -> MappingAction:
    """Return the first rule's action, or MAP: a native game Steam may still download."""
    for rule in LIVE_DECISION_RULES:
        action = rule(game, context)
        if action is not None:
            return action
    return MappingAction.MAP


def plan_live_mappings(
    games: Iterable[OwnedGame], state: SteamshareState, library: PurePosixPath
) -> tuple[LiveDecision, ...]:
    context = LiveContext(state=state, library=library)
    return tuple(LiveDecision(game, decide_live_action(game, context)) for game in games)


# --- Changes asked of Steam ----------------------------------------------------

# The decisions that need a call to Steam; every other one changes nothing.
CHANGE_ACTIONS = frozenset({MappingAction.MAP, MappingAction.UNMAP})


def is_change(decision: LiveDecision) -> bool:
    return decision.action in CHANGE_ACTIONS


@dataclass(frozen=True)
class CompatToolChange:
    """One tool to hand Steam for one game; no tool clears the mapping."""

    appid: str
    tool: str


def requested_change(decision: LiveDecision, tool: str) -> CompatToolChange:
    requested = tool if decision.action is MappingAction.MAP else NO_COMPAT_TOOL
    return CompatToolChange(appid=decision.game.appid, tool=requested)


def _map_confirmed(after: CompatToolSetting, tool: str) -> bool:
    return after.name == tool and after.is_mapping()


def _unmap_confirmed(after: CompatToolSetting, tool: str) -> bool:
    return not after.is_mapping()


# How to tell, from the setting Steam reports afterwards, that a change took.
# This check is needed because Steam does not fail on a tool name it did not
# find at startup: it silently falls back to its default.
CHANGE_CONFIRMATIONS: dict[MappingAction, Callable[[CompatToolSetting, str], bool]] = {
    MappingAction.MAP: _map_confirmed,
    MappingAction.UNMAP: _unmap_confirmed,
}


def is_change_confirmed(decision: LiveDecision, after: CompatToolSetting, tool: str) -> bool:
    return CHANGE_CONFIRMATIONS[decision.action](after, tool)


# --- State ---------------------------------------------------------------------


def _manage(managed: frozenset[str], appid: str) -> frozenset[str]:
    return managed | {appid}


def _forget(managed: frozenset[str], appid: str) -> frozenset[str]:
    return managed - {appid}


# How each decision changes `managed_mappings`; a decision not listed leaves
# it as it was. After an unmap there is nothing left to manage, and a mapping
# the user took over must never be removed by steamshare later.
MANAGED_MAPPING_CHANGES: dict[MappingAction, Callable[[frozenset[str], str], frozenset[str]]] = {
    MappingAction.MAP: _manage,
    MappingAction.UNMAP: _forget,
    MappingAction.SKIPPED_USER_MAPPING: _forget,
}


def record_decision(state: SteamshareState, decision: LiveDecision) -> SteamshareState:
    """Return the state after a decision took effect.

    Call it for a change only once Steam confirmed it, so a failed change
    leaves the state as it was.
    """
    change = MANAGED_MAPPING_CHANGES.get(decision.action)
    if change is None:
        return state
    return replace(state, managed_mappings=change(state.managed_mappings, decision.game.appid))
