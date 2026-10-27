"""Tests for the Steam part of `uninstall --purge`, with a fake Steam client."""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from steamshare.adapters.launcher_location import LAUNCHER_PATH
from steamshare.adapters.steam_cdp import SteamDebuggerUnavailableError
from steamshare.adapters.steam_client_api import OwnedGames
from steamshare.cli.shared import CommandError
from steamshare.cli.steam_settings_cleanup import check_steam_mappings, clear_steam_mappings
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.mapping_cleanup import MappingCleanup
from steamshare.core.uninstall_plan import UncheckedMappings

STEAMSHARE_MAPPING = CompatToolSetting("proton_experimental", 250)
CLEARED = CompatToolSetting("steamlinuxruntime_sniper", 75)
STEAM_ROOT = Path("/home/player/.steam/steam")


def owned(appid: str, *, installed: bool = False) -> OwnedGame:
    return OwnedGame(appid, f"Game {appid}", installed, None, STEAMSHARE_MAPPING)


class FakeSteam:
    def __init__(self, games: Sequence[OwnedGame], *, running: bool = True) -> None:
        self.games = list(games)
        self.running = running
        self.asked_for: frozenset[str] | None = None
        self.changes: list[CompatToolChange] = []
        self.ignored: set[str] = set()
        self.without_details: tuple[str, ...] = ()

    @contextmanager
    def connect(self, steam_root: Path) -> Iterator["FakeSteam"]:
        if not self.running:
            raise SteamDebuggerUnavailableError("Steam's debugger is not answering")
        yield self

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames:
        self.asked_for = appids
        games = tuple(game for game in self.games if appids is None or game.appid in appids)
        return OwnedGames(games=games, without_details=self.without_details)

    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]:
        self.changes += changes
        return {
            change.appid: STEAMSHARE_MAPPING if change.appid in self.ignored else CLEARED
            for change in changes
        }


def write_state(tmp_path: Path, managed: Sequence[str]) -> Path:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"keep_native": [], "managed_mappings": list(managed)}))
    return state


def check(
    steam: FakeSteam, state: Path | None, *, dry_run: bool = False
) -> MappingCleanup | UncheckedMappings:
    return check_steam_mappings(state, STEAM_ROOT, steam.connect, dry_run=dry_run)


def test_asks_steam_only_about_the_games_steamshare_mapped(tmp_path: Path) -> None:
    steam = FakeSteam([owned("1"), owned("2", installed=True), owned("9")])

    cleanup = check(steam, write_state(tmp_path, ["1", "2"]))

    assert steam.asked_for == frozenset({"1", "2"})
    assert isinstance(cleanup, MappingCleanup)
    assert [game.appid for game in cleanup.to_clear] == ["1"]
    assert [game.appid for game in cleanup.kept_installed] == ["2"]


def test_without_managed_mappings_steam_is_not_needed(tmp_path: Path) -> None:
    steam = FakeSteam([], running=False)

    assert check(steam, None) == MappingCleanup(to_clear=(), kept_installed=())
    assert check(steam, write_state(tmp_path, [])) == MappingCleanup((), ())


def test_a_real_purge_refuses_when_steam_is_not_reachable(tmp_path: Path) -> None:
    steam = FakeSteam([owned("1")], running=False)

    with pytest.raises(CommandError) as raised:
        check(steam, write_state(tmp_path, ["1"]))

    message = str(raised.value)
    assert "Start Steam" in message
    assert f"sudo {LAUNCHER_PATH} uninstall --purge" in message
    assert "--keep-steam-settings" in message
    assert "Nothing was changed" in message


def test_a_dry_run_without_steam_counts_the_games_it_would_check(tmp_path: Path) -> None:
    steam = FakeSteam([], running=False)

    assert check(steam, write_state(tmp_path, ["1", "2", "3"]), dry_run=True) == (
        UncheckedMappings(managed_count=3)
    )


def test_games_steam_did_not_report_stop_a_real_purge_before_it_starts(tmp_path: Path) -> None:
    steam = FakeSteam([owned("1")])
    steam.without_details = ("2",)

    with pytest.raises(CommandError, match="did not report"):
        check(steam, write_state(tmp_path, ["1", "2"]))


def test_clearing_asks_for_no_tool_and_returns_the_unconfirmed_games() -> None:
    steam = FakeSteam([])
    steam.ignored = {"3"}
    cleanup = MappingCleanup(to_clear=(owned("1"), owned("3")), kept_installed=())

    unconfirmed = clear_steam_mappings(cleanup, STEAM_ROOT, steam.connect)

    assert [(change.appid, change.tool) for change in steam.changes] == [("1", ""), ("3", "")]
    assert [game.appid for game in unconfirmed] == ["3"]


def test_steam_closing_before_the_clear_says_how_to_continue() -> None:
    steam = FakeSteam([], running=False)
    cleanup = MappingCleanup(to_clear=(owned("1"),), kept_installed=())

    with pytest.raises(CommandError, match="--purge"):
        clear_steam_mappings(cleanup, STEAM_ROOT, steam.connect)
