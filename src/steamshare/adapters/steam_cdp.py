"""Run JavaScript inside the Steam client, over the Chrome DevTools Protocol.

Why this exists: Steam keeps `config.vdf` in memory while it runs and
overwrites it on exit, so a live change has to go through the client itself.
Steam's UI is a Chromium (CEF) app, and its "SharedJSContext" page holds the
`SteamClient` API the UI uses. With remote debugging on, any local program can
evaluate code in that page; Decky Loader works the same way.

Remote debugging is off by default. An empty file named
`.cef-enable-remote-debugging` in the Steam root turns it on at Steam's next
start, and Steam then listens on 127.0.0.1:8080, on loopback only.

The protocol, as used here: `GET /json` lists the pages (targets) with their
WebSocket URLs; a `Runtime.evaluate` message on a page's WebSocket runs an
expression there, and the reply carrying the same `id` holds its value.
Events may arrive in between and are skipped.
"""

import itertools
import json
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Protocol

from steamshare.adapters.safe_writes import ChangeOwner
from steamshare.adapters.websocket_client import (
    WebSocketClosedError,
    WebSocketError,
    WebSocketTimeoutError,
    open_websocket,
)

# The file whose presence makes Steam start its debugger, in the Steam root.
REMOTE_DEBUGGING_FLAG_FILE = ".cef-enable-remote-debugging"

# Where Steam's debugger listens; fixed by Steam, loopback only.
DEBUGGER_ADDRESS = "127.0.0.1:8080"
TARGET_LIST_URL = f"http://{DEBUGGER_ADDRESS}/json"
TARGET_TITLE_KEY = "title"
TARGET_WEBSOCKET_URL_KEY = "webSocketDebuggerUrl"

# The page that holds the `SteamClient` API.
SHARED_JS_CONTEXT_TITLE = "SharedJSContext"

# How long to wait for the target list and the WebSocket handshake; Steam
# answers both at once when its debugger is up.
CONNECT_TIMEOUT_SECONDS = 5.0

EVALUATE_METHOD = "Runtime.evaluate"


class SteamDebuggerError(Exception):
    """Talking to Steam failed; the message says what to do next."""


class SteamDebuggerUnavailableError(SteamDebuggerError):
    """Steam's debugger cannot be reached: Steam is closed, starting, or not set up."""


class SteamScriptError(SteamDebuggerError):
    """The JavaScript ran and threw an exception."""


class TextConnection(Protocol):
    """The part of `WebSocketConnection` a session uses."""

    def send_text(self, text: str) -> None: ...
    def receive_text(self, *, timeout: float) -> str: ...
    def close(self) -> None: ...


# Fetches a URL and parses its JSON body, within a timeout.
FetchJson = Callable[[str, float], object]
OpenConnection = Callable[..., TextConnection]


def remote_debugging_flag_path(steam_root: Path) -> Path:
    return steam_root / REMOTE_DEBUGGING_FLAG_FILE


def turn_on_remote_debugging(
    steam_root: Path,
    *,
    user_id: int,
    group_id: int,
    change_owner: ChangeOwner,
    dry_run: bool,
) -> bool:
    """Create the empty flag file unless it exists; return whether it was (or would be) new.

    Steam reads it only when it starts, so a new file needs one Steam restart.
    `install` creates it as root, so it is then given to the user, who can
    delete it to turn the debugger off again.
    """
    flag_file = remote_debugging_flag_path(steam_root)
    if flag_file.exists():
        return False
    if not dry_run:
        flag_file.touch()
        change_owner(flag_file, user_id, group_id)
    return True


def fetch_json(url: str, timeout: float) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def find_shared_js_context(steam_root: Path, *, fetch_json: FetchJson = fetch_json) -> str:
    """Return the WebSocket URL of Steam's SharedJSContext page."""
    try:
        targets = fetch_json(TARGET_LIST_URL, CONNECT_TIMEOUT_SECONDS)
    except (OSError, ValueError) as error:
        raise SteamDebuggerUnavailableError(
            f"Steam's debugger is not answering on {DEBUGGER_ADDRESS} ({error}). Either "
            f"Steam is closed, or {remote_debugging_flag_path(steam_root)} is missing, or "
            "Steam was not restarted after it was created. Run `sudo ./steamshare "
            "install` from the repository folder to create the file, then restart Steam once."
        ) from error
    for target in targets if isinstance(targets, list) else []:
        if isinstance(target, dict) and target.get(TARGET_TITLE_KEY) == SHARED_JS_CONTEXT_TITLE:
            return str(target[TARGET_WEBSOCKET_URL_KEY])
    raise SteamDebuggerUnavailableError(
        f"Steam's debugger is up, but its {SHARED_JS_CONTEXT_TITLE} page is not, so Steam is "
        "still starting. Wait until the Steam window shows the library, then try again."
    )


def open_steam_debugger(
    steam_root: Path,
    *,
    fetch_json: FetchJson = fetch_json,
    open_connection: OpenConnection = open_websocket,
) -> "SteamDebuggerSession":
    url = find_shared_js_context(steam_root, fetch_json=fetch_json)
    try:
        connection = open_connection(url, timeout=CONNECT_TIMEOUT_SECONDS)
    except WebSocketError as error:
        raise SteamDebuggerUnavailableError(
            f"Could not connect to Steam's {SHARED_JS_CONTEXT_TITLE} page ({error}). If Steam "
            "is just starting or closing, try again in a moment."
        ) from error
    return SteamDebuggerSession(connection)


class SteamDebuggerSession:
    """An open connection to the SharedJSContext page; use it as a context manager."""

    def __init__(self, connection: TextConnection) -> None:
        self._connection = connection
        self._message_ids = itertools.count(1)

    def __enter__(self) -> "SteamDebuggerSession":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def evaluate(self, expression: str, *, timeout: float) -> object:
        """Run the expression, wait for its promise if it returns one, and return the value.

        The value comes back as JSON, so the expression should produce plain
        data. `timeout` limits each wait for a message from Steam.
        """
        message_id = next(self._message_ids)
        request = {
            "id": message_id,
            "method": EVALUATE_METHOD,
            # returnByValue: send the value itself, not a handle to it.
            # awaitPromise: wait for an async expression to finish.
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }
        try:
            self._connection.send_text(json.dumps(request))
            reply = self._receive_reply(message_id, timeout)
        except (WebSocketClosedError, WebSocketTimeoutError) as error:
            raise _connection_error(error, timeout) from error
        except WebSocketError as error:
            raise SteamDebuggerError(
                f"Steam's debugger broke the WebSocket protocol ({error}). Restart Steam, "
                "then try again."
            ) from error
        return _read_evaluate_result(reply)

    def _receive_reply(self, message_id: int, timeout: float) -> dict:
        while True:
            message = json.loads(self._connection.receive_text(timeout=timeout))
            if isinstance(message, dict) and message.get("id") == message_id:
                return message


def _connection_error(error: WebSocketError, timeout: float) -> SteamDebuggerError:
    if isinstance(error, WebSocketTimeoutError):
        return SteamDebuggerError(
            f"Steam did not answer within {timeout:g} s. It may be busy (updating, or "
            "starting a game); try again in a moment."
        )
    return SteamDebuggerUnavailableError(
        "The connection to Steam closed, so Steam has probably exited. Start Steam, then try again."
    )


def _read_evaluate_result(reply: dict) -> object:
    if "error" in reply:
        error = reply["error"]
        raise SteamDebuggerError(
            f"Steam rejected the request: {error.get('message', error)}. A Steam update may "
            "have changed its debugger; check for a steamshare update."
        )
    result = reply.get("result", {})
    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        description = details.get("exception", {}).get("description") or details.get("text")
        # The description is the error message followed by its stack; the
        # message alone is what helps the user.
        raise SteamScriptError(str(description).splitlines()[0])
    # An `undefined` result has no value.
    return result.get("result", {}).get("value")
