"""Tests for choosing which games the `keep-native` picker lists, and what a choice changes.

The picker lists the native Linux games steamshare can decide about: those
Steam would run natively, those steamshare mapped to Proton, and those
already kept native. Valve's per-title choices, the user's own mappings and
the tools are not steamshare's to change, so they are never listed.
"""

from pathlib import PurePosixPath

from steamshare.core.keep_native_choice import (
    KeepNativeChange,
    keep_native_candidates,
    plan_keep_native_change,
    with_keep_native_change,
)
from steamshare.core.live_proton_mapping import CompatToolSetting, OwnedGame
from steamshare.core.proton_mapping import SteamshareState

TOOL = "proton_experimental"
LIBRARY = PurePosixPath("/mnt/steamlib/SteamLibrary")

UNMAPPED_NATIVE = CompatToolSetting("", 0)
STEAM_LINUX_RUNTIME = CompatToolSetting("steamlinuxruntime_sniper", 75)
STEAMSHARE_MAPPING = CompatToolSetting(TOOL, 250)
WINDOWS_ONLY = CompatToolSetting(TOOL, 75)
VALVE_CHOICE = CompatToolSetting("steamlinuxruntime_sniper", 100)
USER_MAPPING = CompatToolSetting("GE-Proton9-20", 250)


def game(appid: str, name: str, tool: CompatToolSetting = UNMAPPED_NATIVE) -> OwnedGame:
    return OwnedGame(appid, name, installed=False, install_folder=None, compat_tool=tool)


LIMBO = game("48000", "LIMBO")
BIGFOOT = game("509980", "BIGFOOT", STEAM_LINUX_RUNTIME)
PORTAL = game("400", "Portal")


NO_STATE = SteamshareState()


def candidate_ids(games: list[OwnedGame], state: SteamshareState = NO_STATE) -> list[str]:
    return [candidate.appid for candidate in keep_native_candidates(games, state, LIBRARY)]


# --- Which games are listed -------------------------------------------------------


def test_lists_native_linux_games_sorted_by_name_ignoring_case() -> None:
    assert candidate_ids([LIMBO, PORTAL, BIGFOOT]) == ["509980", "48000", "400"]


def test_lists_a_game_steamshare_mapped_to_proton() -> None:
    mapped = game("48000", "LIMBO", STEAMSHARE_MAPPING)
    state = SteamshareState(managed_mappings=frozenset({"48000"}))

    assert candidate_ids([mapped], state) == ["48000"]


def test_lists_a_game_already_kept_native_whatever_steam_reports() -> None:
    kept = game("620", "Portal 2", WINDOWS_ONLY)
    state = SteamshareState(keep_native=frozenset({"620"}))

    assert candidate_ids([kept], state) == ["620"]


def test_leaves_out_windows_only_games() -> None:
    assert candidate_ids([game("620", "Portal 2", WINDOWS_ONLY)]) == []


def test_leaves_out_what_is_not_steamshares_to_decide() -> None:
    games = [
        game("730", "Counter-Strike 2", VALVE_CHOICE),
        game("48000", "LIMBO", USER_MAPPING),
        game("228980", "Steamworks Common Redistributables"),
    ]
    # Even when kept native: the user's mapping wins anyway.
    state = SteamshareState(keep_native=frozenset({"48000"}))

    assert candidate_ids(games, state) == []


def test_leaves_out_a_mapping_the_user_took_over() -> None:
    # Managed once, but the user switched it to another tool in Steam.
    taken_over = game("48000", "LIMBO", USER_MAPPING)
    state = SteamshareState(managed_mappings=frozenset({"48000"}))

    assert candidate_ids([taken_over], state) == []


# --- What a choice changes -------------------------------------------------------


def test_a_choice_names_the_games_newly_kept_and_those_given_back() -> None:
    state = SteamshareState(keep_native=frozenset({"400"}))

    change = plan_keep_native_change([BIGFOOT, LIMBO, PORTAL], state, checked={"48000"})

    assert change == KeepNativeChange(newly_kept=(LIMBO,), given_back=(PORTAL,))


def test_a_choice_that_matches_the_state_changes_nothing() -> None:
    state = SteamshareState(keep_native=frozenset({"400"}))

    change = plan_keep_native_change([LIMBO, PORTAL], state, checked={"400"})

    assert change.is_empty()


def test_the_new_state_changes_only_the_listed_games() -> None:
    # 999 is kept native but not listed (no longer owned): the picker could
    # not show it, so the choice must not drop it.
    state = SteamshareState(
        keep_native=frozenset({"400", "999"}), managed_mappings=frozenset({"48000"})
    )
    change = KeepNativeChange(newly_kept=(LIMBO,), given_back=(PORTAL,))

    assert with_keep_native_change(state, change) == SteamshareState(
        keep_native=frozenset({"48000", "999"}), managed_mappings=frozenset({"48000"})
    )
