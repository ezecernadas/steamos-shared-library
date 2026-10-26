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
    GAME_APP_TYPE,
    OwnedGames,
    SteamClientApi,
    SteamClientApiError,
)
from steamshare.core.live_proton_mapping import CompatToolSetting, OwnedGame


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


def test_asks_only_for_games() -> None:
    session = FakeSession([])

    SteamClientApi(session).owned_games()

    assert session.expressions[0].endswith(f"({GAME_APP_TYPE}, 5000)")


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


def test_specifying_a_tool_passes_the_appid_and_tool_and_returns_the_new_setting() -> None:
    session = FakeSession({"compatToolName": "proton_experimental", "compatToolPriority": 250})

    after = SteamClientApi(session).specify_compat_tool("48000", "proton_experimental")

    assert after == CompatToolSetting("proton_experimental", 250)
    arguments = session.expressions[0].rsplit("(", 1)[1].rstrip(")")
    assert json.loads(f"[{arguments}]")[:2] == [48000, "proton_experimental"]
