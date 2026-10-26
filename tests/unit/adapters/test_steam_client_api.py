"""Tests for the calls into Steam's `SteamClient` API.

The JavaScript itself only runs inside Steam (checked by hand against the
real client). These tests check what Python sends and how it reads what
comes back, with a fake session in place of the debugger.
"""

import json
from pathlib import PurePosixPath

import pytest

from steamshare.adapters.steam_cdp import SteamScriptError
from steamshare.adapters.steam_client_api import (
    DETAILS_READS_IN_FLIGHT,
    GAME_APP_TYPE,
    OwnedGames,
    SteamClientApi,
    SteamClientApiError,
)
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.watch_pass_scope import GameOverview, LibrarySnapshot


def call_arguments(expression: str) -> list[object]:
    """The JSON arguments the script was called with, read back from the expression."""
    return json.loads(f"[{expression.rsplit('})(', 1)[1].removesuffix(')')}]")


class FakeSession:
    def __init__(self, result: object) -> None:
        self.result = result
        self.expressions: list[str] = []

    def evaluate(self, expression: str, *, timeout: float) -> object:
        self.expressions.append(expression)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


LIMBO_REPORT = {
    "appid": 48000,
    "name": "LIMBO",
    "installed": True,
    "details": {
        "installFolder": "/mnt/steamlib/SteamLibrary/steamapps/common/Limbo",
        "compatToolName": "proton_experimental",
        "compatToolPriority": 250,
    },
}
TOOL = "proton_experimental"

BIGFOOT_REPORT = {
    "appid": 509980,
    "name": "BIGFOOT",
    "installed": False,
    "details": {"installFolder": "", "compatToolName": "", "compatToolPriority": 0},
}


def test_reads_each_owned_game_with_its_install_folder_and_tool() -> None:
    api = SteamClientApi(FakeSession([LIMBO_REPORT, BIGFOOT_REPORT]))

    assert api.owned_games() == OwnedGames(
        games=(
            OwnedGame(
                appid="48000",
                name="LIMBO",
                installed=True,
                install_folder=PurePosixPath("/mnt/steamlib/SteamLibrary/steamapps/common/Limbo"),
                compat_tool=CompatToolSetting("proton_experimental", 250),
            ),
            OwnedGame(
                appid="509980",
                name="BIGFOOT",
                installed=False,
                install_folder=None,
                compat_tool=CompatToolSetting("", 0),
            ),
        ),
        without_details=(),
    )


def test_asks_for_every_games_details_with_a_bounded_number_in_flight() -> None:
    session = FakeSession([])

    SteamClientApi(session).owned_games()

    assert call_arguments(session.expressions[0]) == [
        GAME_APP_TYPE,
        None,
        5000,
        DETAILS_READS_IN_FLIGHT,
    ]


def test_can_ask_for_only_some_games_details() -> None:
    session = FakeSession([])

    SteamClientApi(session).owned_games(appids=frozenset({"509980", "48000"}))

    # Sorted, so the same request always produces the same script.
    assert call_arguments(session.expressions[0])[1] == [48000, 509980]


def test_a_game_whose_details_did_not_arrive_is_listed_apart() -> None:
    late = {"appid": 730, "name": "Counter-Strike 2", "installed": False, "details": None}

    owned = SteamClientApi(FakeSession([late])).owned_games()

    assert owned.games == ()
    assert owned.without_details == ("730",)


def test_an_unexpected_reply_shape_is_an_api_error() -> None:
    with pytest.raises(SteamClientApiError, match="steamshare update"):
        SteamClientApi(FakeSession([{"appid": 1}])).owned_games()


def test_a_missing_client_function_is_reported_as_a_steam_api_change() -> None:
    missing = SteamScriptError("Error: SteamClient.Apps.RegisterForAppDetails is missing")

    with pytest.raises(SteamClientApiError, match="RegisterForAppDetails is missing"):
        SteamClientApi(FakeSession(missing)).owned_games()


def test_the_library_snapshot_lists_each_game_and_the_steam_session() -> None:
    reply = {
        "clientSession": 1793032431065.8,
        "games": [
            {"appid": 48000, "installed": True},
            {"appid": 509980, "installed": False},
        ],
    }
    session = FakeSession(reply)

    snapshot = SteamClientApi(session).library_snapshot()

    assert snapshot == LibrarySnapshot(
        client_session=1793032431065.8,
        games=frozenset(
            {
                GameOverview(appid="48000", installed=True),
                GameOverview(appid="509980", installed=False),
            }
        ),
    )
    assert call_arguments(session.expressions[0]) == [GAME_APP_TYPE]


@pytest.mark.parametrize(
    "reply",
    [
        {"games": []},
        {"clientSession": True, "games": []},
        {"clientSession": 1.5, "games": [{"appid": "48000", "installed": True}]},
    ],
)
def test_an_unexpected_snapshot_shape_is_an_api_error(reply: object) -> None:
    with pytest.raises(SteamClientApiError, match="steamshare update"):
        SteamClientApi(FakeSession(reply)).library_snapshot()


def test_a_batch_of_changes_returns_what_steam_now_reports_for_each_game() -> None:
    session = FakeSession(
        [
            {"appid": 48000, "details": {"compatToolName": TOOL, "compatToolPriority": 250}},
            {"appid": 509980, "details": None},
        ]
    )
    changes = [CompatToolChange("48000", TOOL), CompatToolChange("509980", "")]

    reported = SteamClientApi(session).specify_compat_tools(changes)

    # None: the game's details did not arrive after the change.
    assert reported == {"48000": CompatToolSetting(TOOL, 250), "509980": None}
    sent = call_arguments(session.expressions[0])[0]
    assert sent == [{"appid": 48000, "tool": TOOL}, {"appid": 509980, "tool": ""}]


def test_an_unexpected_batch_reply_shape_is_an_api_error() -> None:
    session = FakeSession([{"appid": 48000, "details": {"compatToolName": TOOL}}])

    with pytest.raises(SteamClientApiError, match="steamshare update"):
        SteamClientApi(session).specify_compat_tools([CompatToolChange("48000", TOOL)])
