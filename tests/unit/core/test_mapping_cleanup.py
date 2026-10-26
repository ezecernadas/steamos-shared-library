"""Tests for which of steamshare's Proton mappings `uninstall --purge` clears."""

from steamshare.core.live_proton_mapping import CompatToolSetting, OwnedGame
from steamshare.core.mapping_cleanup import (
    clear_requests,
    plan_mapping_cleanup,
    unconfirmed_clears,
)
from steamshare.core.proton_mapping import SteamshareState

STEAMSHARE_MAPPING = CompatToolSetting("proton_experimental", 250)


def game(
    appid: str, *, installed: bool = False, tool: CompatToolSetting = STEAMSHARE_MAPPING
) -> OwnedGame:
    return OwnedGame(appid, f"Game {appid}", installed, None, tool)


STATE = SteamshareState(managed_mappings=frozenset({"1", "2", "3", "4"}))


def test_clears_managed_mappings_of_games_that_are_not_installed() -> None:
    cleanup = plan_mapping_cleanup([game("1"), game("2", installed=True)], STATE)

    assert [g.appid for g in cleanup.to_clear] == ["1"]
    # Clearing an installed game would make Steam re-download its Linux build.
    assert [g.appid for g in cleanup.kept_installed] == ["2"]


def test_mappings_that_are_no_longer_steamshares_are_left_alone() -> None:
    games = [
        # The user picked another tool in Properties > Compatibility.
        game("1", tool=CompatToolSetting("steamlinuxruntime_sniper", 250)),
        # The mapping is gone already; Steam is back to its default.
        game("2", tool=CompatToolSetting("steamlinuxruntime_sniper", 75)),
        # Never steamshare's.
        game("9"),
    ]

    cleanup = plan_mapping_cleanup(games, STATE)

    assert cleanup.to_clear == ()
    assert cleanup.kept_installed == ()


def test_each_clear_asks_steam_for_no_tool() -> None:
    cleanup = plan_mapping_cleanup([game("1"), game("3")], STATE)

    assert [(c.appid, c.tool) for c in clear_requests(cleanup)] == [("1", ""), ("3", "")]


def test_a_clear_is_unconfirmed_while_steam_still_reports_a_mapping_or_nothing() -> None:
    cleanup = plan_mapping_cleanup([game("1"), game("3"), game("4")], STATE)

    unconfirmed = unconfirmed_clears(
        cleanup,
        {
            "1": CompatToolSetting("steamlinuxruntime_sniper", 75),
            "3": STEAMSHARE_MAPPING,
            "4": None,
        },
    )

    assert [g.appid for g in unconfirmed] == ["3", "4"]
