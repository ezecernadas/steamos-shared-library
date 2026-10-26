"""Tests for evaluating JavaScript in Steam's client over the DevTools protocol.

The target list and the WebSocket are faked, so the tests check the protocol
messages and that every failure explains what to do next.
"""

import json
from pathlib import Path

import pytest

from steamshare.adapters.steam_cdp import (
    SteamDebuggerError,
    SteamDebuggerSession,
    SteamDebuggerUnavailableError,
    SteamScriptError,
    find_shared_js_context,
    open_steam_debugger,
    remote_debugging_flag_path,
    turn_on_remote_debugging,
)
from steamshare.adapters.websocket_client import (
    WebSocketClosedError,
    WebSocketError,
    WebSocketTimeoutError,
)

STEAM_ROOT = Path("/home/u/.steam/steam")
SHARED_URL = "ws://127.0.0.1:8080/devtools/page/SHARED"
TARGETS = [
    {"title": "Steam", "webSocketDebuggerUrl": "ws://127.0.0.1:8080/devtools/page/MAIN"},
    {"title": "SharedJSContext", "webSocketDebuggerUrl": SHARED_URL},
]


class FakeConnection:
    """Replies to each evaluate with the scripted messages, in order."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.sent: list[dict] = []
        self.closed = False

    def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def receive_text(self, *, timeout: float) -> str:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    def close(self) -> None:
        self.closed = True


def evaluate_reply(message_id: int, value: object) -> dict:
    return {"id": message_id, "result": {"result": {"type": "object", "value": value}}}


def test_the_flag_file_lives_in_the_steam_root() -> None:
    assert remote_debugging_flag_path(STEAM_ROOT) == STEAM_ROOT / ".cef-enable-remote-debugging"


# --- Finding the target ----------------------------------------------------------


def test_finds_the_shared_js_context_among_the_targets() -> None:
    assert find_shared_js_context(STEAM_ROOT, fetch_json=lambda url, timeout: TARGETS) == SHARED_URL


def test_a_closed_port_points_to_the_flag_file_and_a_restart() -> None:
    def refuse(url: str, timeout: float) -> object:
        raise ConnectionRefusedError("refused")

    with pytest.raises(SteamDebuggerUnavailableError) as raised:
        find_shared_js_context(STEAM_ROOT, fetch_json=refuse)

    message = str(raised.value)
    assert str(remote_debugging_flag_path(STEAM_ROOT)) in message
    assert "restart Steam" in message


def test_a_missing_shared_context_means_steam_is_still_starting() -> None:
    with pytest.raises(SteamDebuggerUnavailableError, match="still starting"):
        find_shared_js_context(STEAM_ROOT, fetch_json=lambda url, timeout: TARGETS[:1])


def test_opening_connects_to_the_shared_context() -> None:
    opened: list[str] = []

    def open_connection(url: str, *, timeout: float) -> FakeConnection:
        opened.append(url)
        return FakeConnection()

    open_steam_debugger(
        STEAM_ROOT, fetch_json=lambda url, timeout: TARGETS, open_connection=open_connection
    )

    assert opened == [SHARED_URL]


def test_a_failed_connection_counts_as_steam_unavailable() -> None:
    def open_connection(url: str, *, timeout: float) -> FakeConnection:
        raise WebSocketError("could not connect")

    with pytest.raises(SteamDebuggerUnavailableError):
        open_steam_debugger(
            STEAM_ROOT, fetch_json=lambda url, timeout: TARGETS, open_connection=open_connection
        )


# --- Evaluating ----------------------------------------------------------------


def test_evaluate_sends_runtime_evaluate_and_returns_the_value() -> None:
    connection = FakeConnection(evaluate_reply(1, [1, 2]))

    value = SteamDebuggerSession(connection).evaluate("[1, 2]", timeout=5)

    assert value == [1, 2]
    assert connection.sent == [
        {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": "[1, 2]", "returnByValue": True, "awaitPromise": True},
        }
    ]


def test_evaluate_skips_events_and_other_replies_until_its_own() -> None:
    connection = FakeConnection(
        {"method": "Runtime.consoleAPICalled", "params": {}},
        evaluate_reply(99, "not mine"),
        evaluate_reply(1, "mine"),
    )

    assert SteamDebuggerSession(connection).evaluate("x", timeout=5) == "mine"


def test_each_evaluate_uses_a_new_id() -> None:
    connection = FakeConnection(evaluate_reply(1, "a"), evaluate_reply(2, "b"))
    session = SteamDebuggerSession(connection)

    session.evaluate("a", timeout=5)
    session.evaluate("b", timeout=5)

    assert [message["id"] for message in connection.sent] == [1, 2]


def test_an_undefined_result_is_none() -> None:
    connection = FakeConnection({"id": 1, "result": {"result": {"type": "undefined"}}})

    assert SteamDebuggerSession(connection).evaluate("undefined", timeout=5) is None


def test_a_javascript_exception_is_reported_with_its_message() -> None:
    thrown = {
        "id": 1,
        "result": {
            "result": {"type": "object", "subtype": "error"},
            "exceptionDetails": {
                "text": "Uncaught",
                "exception": {"description": "Error: SpecifyCompatTool is missing"},
            },
        },
    }
    connection = FakeConnection(thrown)

    with pytest.raises(SteamScriptError, match="SpecifyCompatTool is missing"):
        SteamDebuggerSession(connection).evaluate("x", timeout=5)


def test_a_protocol_error_is_reported() -> None:
    connection = FakeConnection({"id": 1, "error": {"code": -32000, "message": "bad params"}})

    with pytest.raises(SteamDebuggerError, match="bad params"):
        SteamDebuggerSession(connection).evaluate("x", timeout=5)


def test_a_closed_connection_means_steam_went_away() -> None:
    connection = FakeConnection(WebSocketClosedError("closed"))

    with pytest.raises(SteamDebuggerUnavailableError):
        SteamDebuggerSession(connection).evaluate("x", timeout=5)


def test_a_timeout_is_an_error_that_names_the_wait() -> None:
    connection = FakeConnection(WebSocketTimeoutError("silent"))

    with pytest.raises(SteamDebuggerError, match="5 s"):
        SteamDebuggerSession(connection).evaluate("x", timeout=5)


def test_the_session_closes_its_connection_as_a_context_manager() -> None:
    connection = FakeConnection()

    with SteamDebuggerSession(connection):
        pass

    assert connection.closed


# --- Turning the debugger on -----------------------------------------------------


def test_turning_the_debugger_on_creates_the_empty_flag_file(tmp_path: Path) -> None:
    assert turn_on_remote_debugging(tmp_path, dry_run=False) is True
    assert remote_debugging_flag_path(tmp_path).read_text() == ""


def test_turning_the_debugger_on_again_changes_nothing(tmp_path: Path) -> None:
    turn_on_remote_debugging(tmp_path, dry_run=False)

    assert turn_on_remote_debugging(tmp_path, dry_run=False) is False


def test_a_dry_run_only_reports_that_the_flag_file_is_missing(tmp_path: Path) -> None:
    assert turn_on_remote_debugging(tmp_path, dry_run=True) is True
    assert not remote_debugging_flag_path(tmp_path).exists()
