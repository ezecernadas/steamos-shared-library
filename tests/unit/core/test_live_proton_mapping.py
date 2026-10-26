"""Tests for deciding, from what the running Steam client reports, which games to map.

The policy: every native Linux game that is not installed, or is installed in
the shared library, is mapped to Proton, so Steam downloads its Windows build.
Valve's per-title choices, the user's own mappings, kept-native games, and
working native installs elsewhere are left alone. The examples are the games
observed on the real machine (docs/PLAN.md).
"""

from pathlib import PurePosixPath

import pytest

from steamshare.core.live_proton_mapping import (
    NO_COMPAT_TOOL,
    NO_COMPAT_TOOL_PRIORITY,
    STEAM_DEFAULT_PRIORITY,
    USER_MAPPING_PRIORITY,
    VALVE_OVERRIDE_PRIORITY,
    CompatToolSetting,
    LiveDecision,
    OwnedGame,
    is_change_confirmed,
    plan_live_mappings,
    record_decision,
    requested_compat_tool,
)
from steamshare.core.proton_mapping import MappingAction, SteamshareState

TOOL = "proton_experimental"
LIBRARY = PurePosixPath("/mnt/steamlib/SteamLibrary")
LIBRARY_GAMES = LIBRARY / "steamapps" / "common"

UNMAPPED_NATIVE = CompatToolSetting(NO_COMPAT_TOOL, NO_COMPAT_TOOL_PRIORITY)
STEAMSHARE_MAPPING = CompatToolSetting(TOOL, USER_MAPPING_PRIORITY)


def game(
    appid: str = "48000",
    name: str = "LIMBO",
    *,
    tool: CompatToolSetting = UNMAPPED_NATIVE,
    install_folder: PurePosixPath | None = None,
) -> OwnedGame:
    return OwnedGame(
        appid=appid,
        name=name,
        installed=install_folder is not None,
        install_folder=install_folder,
        compat_tool=tool,
    )


NO_STATE = SteamshareState()


def action_for(owned_game: OwnedGame, state: SteamshareState = NO_STATE) -> MappingAction:
    (decision,) = plan_live_mappings([owned_game], state, LIBRARY)
    return decision.action


# --- Games that get mapped -----------------------------------------------------


def test_maps_a_native_game_without_a_runtime_that_is_not_installed() -> None:
    # LIMBO and Human Fall Flat: no tool at all, priority 0.
    assert action_for(game()) is MappingAction.MAP


def test_maps_a_native_game_that_steam_runs_in_the_linux_runtime() -> None:
    runtime = CompatToolSetting("steamlinuxruntime_sniper", STEAM_DEFAULT_PRIORITY)

    assert action_for(game(tool=runtime)) is MappingAction.MAP


def test_maps_a_native_game_installed_in_the_shared_library() -> None:
    limbo = game(install_folder=LIBRARY_GAMES / "Limbo")

    assert action_for(limbo) is MappingAction.MAP


def test_a_decision_carries_the_game_for_printing() -> None:
    limbo = game()

    assert plan_live_mappings([limbo], SteamshareState(), LIBRARY) == (
        LiveDecision(limbo, MappingAction.MAP),
    )


# --- Games left alone ----------------------------------------------------------


def test_skips_the_steamworks_redistributables() -> None:
    assert action_for(game("228980", "Steamworks Common Redistributables")) is (
        MappingAction.SKIPPED_TOOL
    )


def test_skips_a_linux_tool_installed_in_the_library() -> None:
    proton = game("1493710", "Proton", install_folder=LIBRARY_GAMES / "Proton - Experimental")

    assert action_for(proton) is MappingAction.SKIPPED_TOOL


@pytest.mark.parametrize(
    "valve_choice",
    [
        # Counter-Strike 2: anti-cheat needs the native build.
        CompatToolSetting("steamlinuxruntime_sniper", 100),
        # Baldur's Gate 3, Half-Life: Alyx, Hollow Knight.
        CompatToolSetting("proton_9", 100),
        CompatToolSetting(NO_COMPAT_TOOL, VALVE_OVERRIDE_PRIORITY),
    ],
)
def test_never_overrides_valves_per_title_choice(valve_choice: CompatToolSetting) -> None:
    assert action_for(game("730", "Counter-Strike 2", tool=valve_choice)) is (
        MappingAction.SKIPPED_VALVE_OVERRIDE
    )


def test_skips_a_mapping_the_user_made() -> None:
    # HYPERFIGHT was mapped by hand in Properties > Compatibility.
    hyperfight = game("1005870", "HYPERFIGHT", tool=STEAMSHARE_MAPPING)

    assert action_for(hyperfight) is MappingAction.SKIPPED_USER_MAPPING


def test_a_managed_game_the_user_moved_to_another_tool_counts_as_the_users() -> None:
    state = SteamshareState(managed_mappings=frozenset({"48000"}))
    custom = CompatToolSetting("GE-Proton9-20", USER_MAPPING_PRIORITY)

    assert action_for(game(tool=custom), state) is MappingAction.SKIPPED_USER_MAPPING


def test_leaves_a_kept_native_game_without_a_mapping_alone() -> None:
    state = SteamshareState(keep_native=frozenset({"48000"}))

    assert action_for(game(), state) is MappingAction.SKIPPED_KEEP_NATIVE


def test_removes_steamshares_mapping_from_a_game_the_user_now_keeps_native() -> None:
    state = SteamshareState(keep_native=frozenset({"48000"}), managed_mappings=frozenset({"48000"}))

    assert action_for(game(tool=STEAMSHARE_MAPPING), state) is MappingAction.UNMAP


def test_a_game_steamshare_already_mapped_needs_nothing() -> None:
    state = SteamshareState(managed_mappings=frozenset({"48000"}))

    assert action_for(game(tool=STEAMSHARE_MAPPING), state) is MappingAction.ALREADY_MAPPED


def test_a_windows_only_game_already_gets_the_windows_build() -> None:
    windows_only = CompatToolSetting("proton_experimental", STEAM_DEFAULT_PRIORITY)

    assert action_for(game(tool=windows_only)) is MappingAction.WINDOWS_ONLY


@pytest.mark.parametrize(
    "unknown",
    [
        CompatToolSetting("some_future_tool", STEAM_DEFAULT_PRIORITY),
        CompatToolSetting("proton_experimental", NO_COMPAT_TOOL_PRIORITY),
    ],
)
def test_skips_a_tool_setting_steamshare_does_not_recognize(unknown: CompatToolSetting) -> None:
    assert action_for(game(tool=unknown)) is MappingAction.SKIPPED_UNRECOGNIZED_TOOL


def test_never_maps_a_native_install_outside_the_shared_library() -> None:
    elsewhere = game(install_folder=PurePosixPath("/home/u/.steam/steam/steamapps/common/Limbo"))

    assert action_for(elsewhere) is MappingAction.SKIPPED_OUTSIDE_LIBRARY


def test_an_installed_game_with_no_known_folder_counts_as_outside_the_library() -> None:
    unknown_place = OwnedGame(
        appid="48000",
        name="LIMBO",
        installed=True,
        install_folder=None,
        compat_tool=UNMAPPED_NATIVE,
    )

    assert action_for(unknown_place) is MappingAction.SKIPPED_OUTSIDE_LIBRARY


def test_a_folder_that_only_shares_the_librarys_name_prefix_is_outside_it() -> None:
    lookalike = game(install_folder=PurePosixPath("/mnt/steamlib/SteamLibrary2/steamapps/common/x"))

    assert action_for(lookalike) is MappingAction.SKIPPED_OUTSIDE_LIBRARY


# --- Changes asked of Steam, and their confirmation ----------------------------


def test_a_map_asks_steam_for_the_tool_and_an_unmap_for_no_tool() -> None:
    assert requested_compat_tool(LiveDecision(game(), MappingAction.MAP), TOOL) == TOOL
    assert requested_compat_tool(LiveDecision(game(), MappingAction.UNMAP), TOOL) == NO_COMPAT_TOOL


def test_a_map_is_confirmed_when_steam_reports_the_tool_as_a_mapping() -> None:
    decision = LiveDecision(game(), MappingAction.MAP)

    assert is_change_confirmed(decision, STEAMSHARE_MAPPING, TOOL)


def test_a_map_is_not_confirmed_when_steam_fell_back_to_its_default() -> None:
    # Steam silently resets a tool name it did not find at startup.
    decision = LiveDecision(game(), MappingAction.MAP)

    assert not is_change_confirmed(decision, UNMAPPED_NATIVE, "proton_typo")


def test_an_unmap_is_confirmed_when_the_mapping_is_gone() -> None:
    decision = LiveDecision(game(), MappingAction.UNMAP)

    assert is_change_confirmed(decision, UNMAPPED_NATIVE, TOOL)
    assert not is_change_confirmed(decision, STEAMSHARE_MAPPING, TOOL)


# --- State ---------------------------------------------------------------------


def test_a_done_map_makes_the_mapping_steamshares() -> None:
    state = record_decision(SteamshareState(), LiveDecision(game(), MappingAction.MAP))

    assert state.managed_mappings == frozenset({"48000"})


def test_a_done_unmap_forgets_the_mapping() -> None:
    state = SteamshareState(keep_native=frozenset({"48000"}), managed_mappings=frozenset({"48000"}))

    state = record_decision(state, LiveDecision(game(), MappingAction.UNMAP))

    assert state.managed_mappings == frozenset()
    assert state.keep_native == frozenset({"48000"})


def test_a_mapping_the_user_took_over_is_no_longer_steamshares() -> None:
    state = SteamshareState(managed_mappings=frozenset({"48000"}))

    state = record_decision(state, LiveDecision(game(), MappingAction.SKIPPED_USER_MAPPING))

    assert state.managed_mappings == frozenset()


def test_other_decisions_leave_the_state_as_it_was() -> None:
    state = SteamshareState(managed_mappings=frozenset({"48000"}))

    assert record_decision(state, LiveDecision(game(), MappingAction.ALREADY_MAPPED)) == state
