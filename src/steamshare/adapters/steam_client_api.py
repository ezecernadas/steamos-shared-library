"""Read the owned games and set compatibility tools through Steam's `SteamClient` API.

The JavaScript runs inside the Steam client (see `steam_cdp`). It is kept
as small as possible: one script gathers data, the other performs a single
change and reads the result back. Every decision is made in Python
(`core.live_proton_mapping`).

What the scripts rely on was observed in the real client on 2026-10-26;
Steam does not document it:

- `collectionStore.allAppsCollection.allApps` lists every owned app with
  `appid`, `display_name`, `app_type`, and `per_client_data[0].installed`.
- `SteamClient.Apps.RegisterForAppDetails(appid, callback)` calls back with
  the app's details (`strInstallFolder`, `strCompatToolName`,
  `nCompatToolPriority`) and returns a handle to `.unregister()`.
- `SteamClient.Apps.SpecifyCompatTool(appid, name)` sets the tool, as
  Properties > Compatibility does; an empty name clears it. It returns
  nothing, and the details show the change about a second later.

Steam changes this API without notice (its 13 Oct 2026 update removed
`GetAvailableCompatTools`), so every script checks that what it calls exists
and throws a clear error otherwise.
"""

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, TypeVar

from steamshare.adapters.steam_cdp import SteamDebuggerError, SteamScriptError
from steamshare.core.live_proton_mapping import CompatToolSetting, OwnedGame

# `app_type` of a game in Steam's app overviews (tools, soundtracks, and
# videos have other values).
GAME_APP_TYPE = 1

# Steam usually answers a details request at once (615 games in about 28 s,
# one after the other); a game that has not answered by then is retried on
# the next pass.
DETAILS_TIMEOUT_MILLISECONDS = 5000
# A change shows in the details about a second after SpecifyCompatTool.
SETTLE_AFTER_CHANGE_MILLISECONDS = 1000

# Limits for each wait on Steam. Gathering asks for every game's details in
# one script, so it gets the larger budget.
GATHER_TIMEOUT_SECONDS = 600.0
CHANGE_TIMEOUT_SECONDS = 30.0

# Reads one app's details: resolves with them, or with null after the
# timeout. The registration is dropped as soon as the first callback (or the
# timeout) comes, including when Steam calls back before returning the handle.
READ_APP_DETAILS_FUNCTION = """\
(appid, timeoutMilliseconds) => new Promise((resolve) => {
  let finished = false;
  let registration = null;
  const finish = (details) => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    resolve(details);
    if (registration) registration.unregister();
  };
  const timer = setTimeout(() => finish(null), timeoutMilliseconds);
  registration = SteamClient.Apps.RegisterForAppDetails(appid, finish);
  if (finished) registration.unregister();
})"""

REQUIRE_FUNCTION = """\
(owner, name, path) => {
  if (typeof owner?.[name] !== 'function') throw new Error(`${path} is missing`);
}"""

GATHER_OWNED_GAMES_SCRIPT = f"""\
async (gameAppType, detailsTimeoutMilliseconds) => {{
  const require = {REQUIRE_FUNCTION};
  const readAppDetails = {READ_APP_DETAILS_FUNCTION};
  const apps = globalThis.collectionStore?.allAppsCollection?.allApps;
  if (!Array.isArray(apps)) throw new Error('collectionStore.allAppsCollection.allApps is missing');
  require(globalThis.SteamClient?.Apps, 'RegisterForAppDetails',
    'SteamClient.Apps.RegisterForAppDetails');
  const games = [];
  for (const app of apps.filter((app) => app.app_type === gameAppType)) {{
    const details = await readAppDetails(app.appid, detailsTimeoutMilliseconds);
    games.push({{
      appid: app.appid,
      name: app.display_name,
      installed: Boolean(app.per_client_data?.[0]?.installed),
      details: details && {{
        installFolder: details.strInstallFolder,
        compatToolName: details.strCompatToolName,
        compatToolPriority: details.nCompatToolPriority,
      }},
    }});
  }}
  return games;
}}"""

SPECIFY_COMPAT_TOOL_SCRIPT = f"""\
async (appid, toolName, settleMilliseconds, detailsTimeoutMilliseconds) => {{
  const require = {REQUIRE_FUNCTION};
  const readAppDetails = {READ_APP_DETAILS_FUNCTION};
  require(globalThis.SteamClient?.Apps, 'SpecifyCompatTool', 'SteamClient.Apps.SpecifyCompatTool');
  require(globalThis.SteamClient?.Apps, 'RegisterForAppDetails',
    'SteamClient.Apps.RegisterForAppDetails');
  SteamClient.Apps.SpecifyCompatTool(appid, toolName);
  await new Promise((resolve) => setTimeout(resolve, settleMilliseconds));
  const details = await readAppDetails(appid, detailsTimeoutMilliseconds);
  if (!details) throw new Error(`Steam did not report app ${{appid}}'s details after the change`);
  return {{
    compatToolName: details.strCompatToolName,
    compatToolPriority: details.nCompatToolPriority,
  }};
}}"""

API_CHANGED_HINT = "A Steam update may have changed its client API; check for a steamshare update."


class SteamClientApiError(SteamDebuggerError):
    """Steam's client API is missing something, or answered in an unexpected shape."""


class ScriptEvaluator(Protocol):
    """The part of `SteamDebuggerSession` this module uses."""

    def evaluate(self, expression: str, *, timeout: float) -> object: ...


@dataclass(frozen=True)
class OwnedGames:
    games: tuple[OwnedGame, ...]
    # Appids whose details did not arrive in time; they are left for the next pass.
    without_details: tuple[str, ...]


class SteamClientApi:
    def __init__(self, session: ScriptEvaluator) -> None:
        self._session = session

    def owned_games(self) -> OwnedGames:
        reports = self._run(
            GATHER_OWNED_GAMES_SCRIPT,
            [GAME_APP_TYPE, DETAILS_TIMEOUT_MILLISECONDS],
            GATHER_TIMEOUT_SECONDS,
        )
        try:
            return _read_owned_games(reports)
        except (KeyError, TypeError, ValueError) as error:
            raise SteamClientApiError(
                f"Steam listed the owned games in an unexpected shape ({error!r}). "
                f"{API_CHANGED_HINT}"
            ) from error

    def specify_compat_tool(self, appid: str, tool: str) -> CompatToolSetting:
        """Set the game's tool (an empty name clears it) and return what Steam now reports."""
        reply = self._run(
            SPECIFY_COMPAT_TOOL_SCRIPT,
            [int(appid), tool, SETTLE_AFTER_CHANGE_MILLISECONDS, DETAILS_TIMEOUT_MILLISECONDS],
            CHANGE_TIMEOUT_SECONDS,
        )
        try:
            return _read_compat_tool(reply)
        except (KeyError, TypeError, ValueError) as error:
            raise SteamClientApiError(
                f"Steam reported app {appid}'s tool in an unexpected shape ({error!r}). "
                f"{API_CHANGED_HINT}"
            ) from error

    def _run(self, script: str, arguments: list[object], timeout: float) -> object:
        # The arguments go in as JSON literals, never spliced into the code.
        call_arguments = ", ".join(json.dumps(argument) for argument in arguments)
        try:
            return self._session.evaluate(f"({script})({call_arguments})", timeout=timeout)
        except SteamScriptError as error:
            raise SteamClientApiError(f"{error}. {API_CHANGED_HINT}") from error


def _read_owned_games(reports: object) -> OwnedGames:
    if not isinstance(reports, list):
        raise TypeError(f"expected a list, got {type(reports).__name__}")
    games = []
    without_details = []
    for report in reports:
        if report["details"] is None:
            without_details.append(str(report["appid"]))
        else:
            games.append(_read_owned_game(report))
    return OwnedGames(games=tuple(games), without_details=tuple(without_details))


def _read_owned_game(report: dict) -> OwnedGame:
    details = report["details"]
    install_folder = _require(details["installFolder"], str)
    return OwnedGame(
        appid=str(_require(report["appid"], int)),
        name=_require(report["name"], str),
        installed=_require(report["installed"], bool),
        # Steam reports "" for a game that is not installed.
        install_folder=PurePosixPath(install_folder) if install_folder else None,
        compat_tool=_read_compat_tool(details),
    )


def _read_compat_tool(details: object) -> CompatToolSetting:
    if not isinstance(details, dict):
        raise TypeError(f"expected an object, got {type(details).__name__}")
    return CompatToolSetting(
        name=_require(details["compatToolName"], str),
        priority=_require(details["compatToolPriority"], int),
    )


Expected = TypeVar("Expected")


def _require(value: object, expected: type[Expected]) -> Expected:
    if not isinstance(value, expected):
        raise TypeError(f"expected {expected.__name__}, got {value!r}")
    return value
