"""Decide which apps in the shared library get a forced Proton mapping.

Why this matters: the shared NTFS library holds each game's Windows build.
When Linux Steam sees a game that also has a native Linux build, it queues
the Linux depot over the Windows files. Windows can then no longer run the
game, and both systems keep downloading it again. Forcing a Proton
compatibility tool for the app makes Linux Steam use the Windows depot
instead. Phase 0 verified this by hand (docs/PLAN.md, "Phase 0 results").

The policy is to map every game in the library. Forcing Proton on a
Windows-only game is harmless; it only pins the tool. The exceptions are the
rules in `DECISION_RULES`. To add one, write a function with the
`DecisionRule` signature and add it to the tuple.

steamshare remembers which mappings it wrote (`managed_mappings`), and only
ever removes those. A mapping the user made in Steam stays untouched.

This module is pure: the adapters read the manifests, `config.vdf`, and the
state file, and write the results back.
"""

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields, replace
from enum import Enum

from steamshare.core.library_layout import LINUX_TOOL_FOLDER_PREFIXES
from steamshare.core.vdf import KeyValues

# Where Steam keeps the per-app compatibility tool, inside `config.vdf`.
COMPAT_TOOL_MAPPING_PATH = ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping")

# Keys of a mapping entry, and the values Steam writes when a user forces a
# tool in a game's Properties > Compatibility. `config` holds extra tool
# options, which a plain Proton mapping does not need.
MAPPING_TOOL_KEY = "name"
MAPPING_CONFIG_KEY = "config"
MAPPING_PRIORITY_KEY = "priority"
MAPPING_CONFIG = ""
MAPPING_PRIORITY = "250"

# The root section of an `appmanifest_<appid>.acf`, and the keys read from it.
APP_MANIFEST_ROOT = "AppState"
MANIFEST_APPID_KEY = "appid"
MANIFEST_NAME_KEY = "name"
MANIFEST_INSTALLDIR_KEY = "installdir"

# The tool forced unless the user picks another. Proton Experimental runs the
# widest range of games, and it is the one Phase 0 verified.
DEFAULT_PROTON_TOOL = "proton_experimental"

# Steamworks Common Redistributables (DirectX, VC++ runtimes and the like).
# Steam installs it into any library that holds a game needing it. It is not
# a game anyone launches, so there is nothing to force.
STEAMWORKS_COMMON_REDISTRIBUTABLES_APPID = "228980"


@dataclass(frozen=True)
class LibraryApp:
    """One app installed in the library, as its manifest describes it."""

    appid: str
    name: str
    installdir: str  # folder name under steamapps/common


@dataclass(frozen=True)
class SteamshareState:
    """What steamshare remembers between runs."""

    # Apps the user wants Linux Steam to run natively, so they are never mapped.
    keep_native: frozenset[str] = field(default_factory=frozenset)
    # Apps whose mapping steamshare wrote, and therefore may remove.
    managed_mappings: frozenset[str] = field(default_factory=frozenset)


class MappingAction(Enum):
    """What happens to one app; the value is the reason printed next to it."""

    MAP = "map"
    ALREADY_MAPPED = "already mapped (managed)"
    UNMAP = "unmap (keep native)"
    SKIPPED_USER_MAPPING = "skipped (user mapping)"
    SKIPPED_TOOL = "skipped (tool)"
    SKIPPED_KEEP_NATIVE = "skipped (keep native)"
    # Only `watch` decides these: it sees what the running Steam client
    # reports (`core.live_proton_mapping`), which config.vdf does not hold.
    SKIPPED_VALVE_OVERRIDE = "skipped (Valve's choice)"
    WINDOWS_ONLY = "nothing to do (Windows-only)"
    SKIPPED_UNRECOGNIZED_TOOL = "skipped (unrecognized tool)"
    SKIPPED_OUTSIDE_LIBRARY = "skipped (installed elsewhere)"


@dataclass(frozen=True)
class AppDecision:
    app: LibraryApp
    action: MappingAction


@dataclass(frozen=True)
class MappingPlan:
    """The decision for each app, and the mappings and state that result."""

    decisions: tuple[AppDecision, ...]
    mappings: KeyValues
    state: SteamshareState


# --- Manifests -----------------------------------------------------------------


def read_library_app(manifest: KeyValues) -> LibraryApp:
    """Build the app record from a parsed `appmanifest_<appid>.acf`."""
    app_state = manifest.get(APP_MANIFEST_ROOT)
    if not isinstance(app_state, dict):
        raise ValueError(f"the manifest has no {APP_MANIFEST_ROOT} section")
    return LibraryApp(
        appid=_read_manifest_value(app_state, MANIFEST_APPID_KEY),
        name=_read_manifest_value(app_state, MANIFEST_NAME_KEY),
        installdir=_read_manifest_value(app_state, MANIFEST_INSTALLDIR_KEY),
    )


def _read_manifest_value(app_state: KeyValues, key: str) -> str:
    value = app_state.get(key)
    if not isinstance(value, str):
        raise ValueError(f"the manifest has no {key!r} value")
    return value


# --- Decision rules ------------------------------------------------------------


@dataclass(frozen=True)
class MappingContext:
    """What the rules need to know besides the app itself."""

    mappings: KeyValues
    state: SteamshareState

    def is_mapped(self, appid: str) -> bool:
        return appid in self.mappings

    def is_managed(self, appid: str) -> bool:
        return self.is_mapped(appid) and appid in self.state.managed_mappings


# A rule returns the action for the app when it applies, or None to let the
# next rule decide.
DecisionRule = Callable[[LibraryApp, MappingContext], MappingAction | None]


def is_linux_tool(app: LibraryApp, context: MappingContext) -> MappingAction | None:
    # Proton and the Steam Linux Runtime are Linux programs; forcing Proton
    # on Proton itself would make no sense.
    if app.installdir.startswith(LINUX_TOOL_FOLDER_PREFIXES):
        return MappingAction.SKIPPED_TOOL
    return None


def is_steamworks_redistributable(app: LibraryApp, context: MappingContext) -> MappingAction | None:
    if app.appid == STEAMWORKS_COMMON_REDISTRIBUTABLES_APPID:
        return MappingAction.SKIPPED_TOOL
    return None


def is_kept_native(app: LibraryApp, context: MappingContext) -> MappingAction | None:
    if app.appid not in context.state.keep_native:
        return None
    # Only steamshare's own mapping is removed; a user mapping stays.
    if context.is_managed(app.appid):
        return MappingAction.UNMAP
    return MappingAction.SKIPPED_KEEP_NATIVE


def has_user_mapping(app: LibraryApp, context: MappingContext) -> MappingAction | None:
    if context.is_mapped(app.appid) and not context.is_managed(app.appid):
        return MappingAction.SKIPPED_USER_MAPPING
    return None


def has_managed_mapping(app: LibraryApp, context: MappingContext) -> MappingAction | None:
    if context.is_managed(app.appid):
        return MappingAction.ALREADY_MAPPED
    return None


# Checked in order; the first rule that returns an action decides. Keep
# native comes before the mapping rules, so it can remove a managed mapping.
DECISION_RULES: tuple[DecisionRule, ...] = (
    is_linux_tool,
    is_steamworks_redistributable,
    is_kept_native,
    has_user_mapping,
    has_managed_mapping,
)


def decide_action(app: LibraryApp, context: MappingContext) -> MappingAction:
    """Return the first rule's action, or MAP when no rule claims the app."""
    for rule in DECISION_RULES:
        action = rule(app, context)
        if action is not None:
            return action
    return MappingAction.MAP


# --- Planning ------------------------------------------------------------------


def plan_proton_mappings(
    apps: Iterable[LibraryApp],
    mappings: KeyValues,
    state: SteamshareState,
    tool_name: str,
) -> MappingPlan:
    """Decide each app's action and compute the resulting mappings and state.

    The inputs are not modified. Running the plan again on its own result
    changes nothing, which is what makes `force-proton` idempotent.
    """
    context = MappingContext(mappings=mappings, state=state)
    decisions = tuple(AppDecision(app, decide_action(app, context)) for app in apps)

    new_mappings = copy.deepcopy(mappings)
    managed = set(state.managed_mappings)
    for decision in decisions:
        appid = decision.app.appid
        if decision.action is MappingAction.MAP:
            new_mappings[appid] = build_mapping_entry(tool_name)
            managed.add(appid)
        elif decision.action is MappingAction.UNMAP:
            del new_mappings[appid]
            managed.discard(appid)

    new_state = replace(state, managed_mappings=frozenset(managed))
    return MappingPlan(decisions=decisions, mappings=new_mappings, state=new_state)


def build_mapping_entry(tool_name: str) -> KeyValues:
    return {
        MAPPING_TOOL_KEY: tool_name,
        MAPPING_CONFIG_KEY: MAPPING_CONFIG,
        MAPPING_PRIORITY_KEY: MAPPING_PRIORITY,
    }


# --- State changes requested on the command line -------------------------------


def mark_keep_native(state: SteamshareState, appids: Iterable[str]) -> SteamshareState:
    return replace(state, keep_native=state.keep_native | frozenset(appids))


def allow_proton(state: SteamshareState, appids: Iterable[str]) -> SteamshareState:
    return replace(state, keep_native=state.keep_native - frozenset(appids))


# --- Saving a command's changes ------------------------------------------------


def apply_state_changes(
    saved: SteamshareState, *, before: SteamshareState, after: SteamshareState
) -> SteamshareState:
    """Apply what a command changed (`before` to `after`) to the state saved since.

    Why: `watch` runs for hours and holds the state it read at the start of a
    pass, while `keep-native` or `force-proton` may save theirs meanwhile.
    Writing the whole stale copy back would undo their changes. So each
    command saves only the appids it added or removed, in every list, on top
    of what is in the file now; an appid it did not touch keeps the file's value.
    """
    merged = {}
    for state_field in fields(SteamshareState):
        old: frozenset[str] = getattr(before, state_field.name)
        new: frozenset[str] = getattr(after, state_field.name)
        current: frozenset[str] = getattr(saved, state_field.name)
        merged[state_field.name] = (current - (old - new)) | (new - old)
    return SteamshareState(**merged)


# --- The CompatToolMapping node in config.vdf ----------------------------------


def read_compat_tool_mappings(config: KeyValues) -> KeyValues:
    """Return a copy of the mappings, or an empty section if there are none yet."""
    node: KeyValues = config
    for key in COMPAT_TOOL_MAPPING_PATH:
        child = _find_section(node, key)
        if child is None:
            return {}
        node = child
    return copy.deepcopy(node)


def with_compat_tool_mappings(config: KeyValues, mappings: KeyValues) -> KeyValues:
    """Return a copy of the config with its mappings replaced.

    Missing sections on the way are created at the end of their parent. An
    existing node keeps its position, so the rewritten file differs from
    Steam's only in the mapping entries.
    """
    updated = copy.deepcopy(config)
    node = updated
    *parent_keys, mapping_key = COMPAT_TOOL_MAPPING_PATH
    for key in parent_keys:
        child = _find_section(node, key)
        if child is None:
            child = {}
            node[key] = child
        node = child
    existing_key = _find_key(node, mapping_key)
    node[existing_key or mapping_key] = copy.deepcopy(mappings)
    return updated


def _find_section(node: KeyValues, key: str) -> KeyValues | None:
    existing_key = _find_key(node, key)
    if existing_key is None:
        return None
    child = node[existing_key]
    if not isinstance(child, dict):
        raise ValueError(f"{existing_key!r} in config.vdf is a value, not a section")
    return child


def _find_key(node: KeyValues, key: str) -> str | None:
    # Steam matches KeyValues keys case-insensitively, and the case it writes
    # varies between installs ("Valve" or "valve"). Reusing the existing key
    # keeps Steam's spelling in the file.
    wanted = key.casefold()
    return next((existing for existing in node if existing.casefold() == wanted), None)
