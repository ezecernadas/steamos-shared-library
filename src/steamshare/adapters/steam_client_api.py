"""Read the owned games and set compatibility tools through Steam's `SteamClient` API.

The JavaScript runs inside the Steam client (see `steam_cdp`). It is kept
as small as possible and only gathers data or carries out a change; every
decision is made in Python (`core.live_proton_mapping`,
`core.watch_pass_scope`). There are three scripts:

- the library snapshot: each game's appid and installed flag, with no
  details, which is instant;
- the gather: the details of every game, or of the games asked for;
- the batch change: one `SpecifyCompatTool` per game, one settle wait for
  the whole batch, then each game's details read back.

Details are read through a small worker pool, `DETAILS_READS_IN_FLIGHT` at a
time, because Steam answers each request separately.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, TypeVar

from steamshare.adapters.steam_cdp import SteamDebuggerError, SteamScriptError
from steamshare.core.live_proton_mapping import CompatToolChange, CompatToolSetting, OwnedGame
from steamshare.core.watch_pass_scope import GameOverview, LibrarySnapshot

# `app_type` of a game in Steam's app overviews (tools, soundtracks, and
# videos have other values).
GAME_APP_TYPE = 1

# Steam usually answers a details request at once; a game that has not
# answered by then is retried on the next pass.
DETAILS_TIMEOUT_MILLISECONDS = 5000
# How many details requests are open at a time. One after the other, 615
# games took 34 s; 20 at a time took under 3 s with no timeouts. Bounded, not
# all at once, so a large library does not open hundreds of registrations.
DETAILS_READS_IN_FLIGHT = 20
# A change shows in the details about a second after SpecifyCompatTool; a
# batch waits this once, after asking for all of its changes.
SETTLE_AFTER_CHANGE_MILLISECONDS = 1000

# Limits for each wait on Steam. Gathering may read every game's details in
# one script, so it gets the larger budget.
SNAPSHOT_TIMEOUT_SECONDS = 10.0
GATHER_TIMEOUT_SECONDS = 600.0
CHANGE_TIMEOUT_SECONDS = 60.0

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

# Runs `work` on every item with at most `limit` running at once, and
# resolves with the results in the items' order: a small worker pool.
MAP_WITH_LIMIT_FUNCTION = """\
async (items, limit, work) => {
  const results = new Array(items.length);
  let nextIndex = 0;
  const worker = async () => {
    while (nextIndex < items.length) {
      const index = nextIndex++;
      results[index] = await work(items[index]);
    }
  };
  const workerCount = Math.min(limit, items.length);
  await Promise.all(Array.from({ length: workerCount }, worker));
  return results;
}"""

# The fields Python reads from an app's details, or null when they did not arrive.
SUMMARIZE_DETAILS_FUNCTION = """\
(details) => details && {
  installFolder: details.strInstallFolder,
  compatToolName: details.strCompatToolName,
  compatToolPriority: details.nCompatToolPriority,
}"""

# Every owned game's overview, which Steam already holds in memory: instant.
LIST_GAMES_FUNCTION = """\
(gameAppType) => {
  const apps = globalThis.collectionStore?.allAppsCollection?.allApps;
  if (!Array.isArray(apps)) throw new Error('collectionStore.allAppsCollection.allApps is missing');
  return apps.filter((app) => app.app_type === gameAppType).map((app) => ({
    appid: app.appid,
    name: app.display_name,
    installed: Boolean(app.per_client_data?.[0]?.installed),
  }));
}"""

REQUIRE_FUNCTION = """\
(owner, name, path) => {
  if (typeof owner?.[name] !== 'function') throw new Error(`${path} is missing`);
}"""

# `performance.timeOrigin` is when this page started, so it changes when
# Steam restarts.
LIBRARY_SNAPSHOT_SCRIPT = f"""\
(gameAppType) => {{
  const listGames = {LIST_GAMES_FUNCTION};
  return {{
    clientSession: performance.timeOrigin,
    games: listGames(gameAppType).map(({{ appid, installed }}) => ({{ appid, installed }})),
  }};
}}"""

# `appids` is null for every game, or the list of the games to read.
GATHER_OWNED_GAMES_SCRIPT = f"""\
async (gameAppType, appids, detailsTimeoutMilliseconds, readsInFlight) => {{
  const require = {REQUIRE_FUNCTION};
  const readAppDetails = {READ_APP_DETAILS_FUNCTION};
  const mapWithLimit = {MAP_WITH_LIMIT_FUNCTION};
  const summarizeDetails = {SUMMARIZE_DETAILS_FUNCTION};
  const listGames = {LIST_GAMES_FUNCTION};
  require(globalThis.SteamClient?.Apps, 'RegisterForAppDetails',
    'SteamClient.Apps.RegisterForAppDetails');
  const wanted = appids && new Set(appids);
  const games = listGames(gameAppType).filter((game) => !wanted || wanted.has(game.appid));
  const details = await mapWithLimit(games, readsInFlight,
    (game) => readAppDetails(game.appid, detailsTimeoutMilliseconds));
  return games.map((game, index) => ({{ ...game, details: summarizeDetails(details[index]) }}));
}}"""

# Asks for every change first and waits once, so a batch costs one settle
# delay instead of one per game.
SPECIFY_COMPAT_TOOLS_SCRIPT = f"""\
async (changes, settleMilliseconds, detailsTimeoutMilliseconds, readsInFlight) => {{
  const require = {REQUIRE_FUNCTION};
  const readAppDetails = {READ_APP_DETAILS_FUNCTION};
  const mapWithLimit = {MAP_WITH_LIMIT_FUNCTION};
  const summarizeDetails = {SUMMARIZE_DETAILS_FUNCTION};
  require(globalThis.SteamClient?.Apps, 'SpecifyCompatTool', 'SteamClient.Apps.SpecifyCompatTool');
  require(globalThis.SteamClient?.Apps, 'RegisterForAppDetails',
    'SteamClient.Apps.RegisterForAppDetails');
  for (const change of changes) SteamClient.Apps.SpecifyCompatTool(change.appid, change.tool);
  await new Promise((resolve) => setTimeout(resolve, settleMilliseconds));
  const details = await mapWithLimit(changes, readsInFlight,
    (change) => readAppDetails(change.appid, detailsTimeoutMilliseconds));
  return changes.map((change, index) => ({{
    appid: change.appid,
    details: summarizeDetails(details[index]),
  }}));
}}"""

# What a reply reader returns.
Read = TypeVar("Read")

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

    def library_snapshot(self) -> LibrarySnapshot:
        """Every owned game's appid and installed flag, without reading any details."""
        reply = self._run(LIBRARY_SNAPSHOT_SCRIPT, [GAME_APP_TYPE], SNAPSHOT_TIMEOUT_SECONDS)
        return self._read(_read_library_snapshot, reply, "listed the owned games")

    def owned_games(self, appids: frozenset[str] | None = None) -> OwnedGames:
        """Every owned game with its details, or only the games in `appids`."""
        wanted = None if appids is None else sorted(int(appid) for appid in appids)
        reports = self._run(
            GATHER_OWNED_GAMES_SCRIPT,
            [GAME_APP_TYPE, wanted, DETAILS_TIMEOUT_MILLISECONDS, DETAILS_READS_IN_FLIGHT],
            GATHER_TIMEOUT_SECONDS,
        )
        return self._read(_read_owned_games, reports, "listed the owned games")

    def specify_compat_tools(
        self, changes: Sequence[CompatToolChange]
    ) -> dict[str, CompatToolSetting | None]:
        """Make the changes and return what Steam then reports for each game.

        A game maps to None when its details did not arrive after the change.
        """
        requests = [{"appid": int(change.appid), "tool": change.tool} for change in changes]
        reply = self._run(
            SPECIFY_COMPAT_TOOLS_SCRIPT,
            [
                requests,
                SETTLE_AFTER_CHANGE_MILLISECONDS,
                DETAILS_TIMEOUT_MILLISECONDS,
                DETAILS_READS_IN_FLIGHT,
            ],
            CHANGE_TIMEOUT_SECONDS,
        )
        return self._read(_read_change_results, reply, "reported the changed tools")

    def _run(self, script: str, arguments: list[object], timeout: float) -> object:
        # The arguments go in as JSON literals, never spliced into the code.
        call_arguments = ", ".join(json.dumps(argument) for argument in arguments)
        try:
            return self._session.evaluate(f"({script})({call_arguments})", timeout=timeout)
        except SteamScriptError as error:
            raise SteamClientApiError(f"{error}. {API_CHANGED_HINT}") from error

    @staticmethod
    def _read(read: Callable[[object], Read], reply: object, what_steam_did: str) -> Read:
        try:
            return read(reply)
        except (KeyError, TypeError, ValueError) as error:
            raise SteamClientApiError(
                f"Steam {what_steam_did} in an unexpected shape ({error!r}). {API_CHANGED_HINT}"
            ) from error


def _read_library_snapshot(reply: object) -> LibrarySnapshot:
    if not isinstance(reply, dict):
        raise TypeError(f"expected an object, got {type(reply).__name__}")
    games = frozenset(
        GameOverview(
            appid=str(_require(game["appid"], int)),
            installed=_require(game["installed"], bool),
        )
        for game in reply["games"]
    )
    return LibrarySnapshot(client_session=_require_number(reply["clientSession"]), games=games)


def _read_change_results(reply: object) -> dict[str, CompatToolSetting | None]:
    if not isinstance(reply, list):
        raise TypeError(f"expected a list, got {type(reply).__name__}")
    results: dict[str, CompatToolSetting | None] = {}
    for result in reply:
        details = result["details"]
        appid = str(_require(result["appid"], int))
        results[appid] = None if details is None else _read_compat_tool(details)
    return results


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


def _require_number(value: object) -> float:
    # JSON has one number type, so a whole number arrives as an int. A bool is
    # an int in Python, but never a number Steam meant.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {value!r}")
    return float(value)
