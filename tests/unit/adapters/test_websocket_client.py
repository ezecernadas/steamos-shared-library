"""Tests for the minimal WebSocket client, against a fake socket.

The fake socket replays bytes a server would send and records what the client
writes, so the handshake and every frame shape are checked byte by byte.
"""

import base64
import hashlib
import struct

import pytest

from steamshare.adapters.websocket_client import (
    WebSocketClosedError,
    WebSocketConnection,
    WebSocketError,
    WebSocketTimeoutError,
    accept_key_for,
    encode_client_frame,
    open_websocket,
    parse_websocket_url,
)

# The handshake example from RFC 6455, section 1.3.
RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

RFC_KEY_BYTES = base64.b64decode(RFC_KEY)
MASK = b"\x01\x02\x03\x04"

TEXT = 0x1
CONTINUATION = 0x0
CLOSE = 0x8
PING = 0x9
PONG = 0xA


class FakeSocket:
    """Hands out the scripted bytes a few at a time, and records what is sent."""

    def __init__(self, incoming: bytes, chunk_size: int = 7) -> None:
        self.incoming = incoming
        self.chunk_size = chunk_size
        self.sent = b""
        self.timeouts: list[float | None] = []
        self.closed = False

    def recv(self, size: int) -> bytes:
        if self.incoming == b"TIMEOUT":
            raise TimeoutError
        chunk = self.incoming[: min(size, self.chunk_size)]
        self.incoming = self.incoming[len(chunk) :]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout: float | None) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


def server_frame(opcode: int, payload: bytes, *, final: bool = True) -> bytes:
    first = (0x80 if final else 0) | opcode
    if len(payload) < 126:
        header = bytes([first, len(payload)])
    elif len(payload) < 2**16:
        header = bytes([first, 126]) + struct.pack("!H", len(payload))
    else:
        header = bytes([first, 127]) + struct.pack("!Q", len(payload))
    return header + payload


def handshake_reply(accept: str = RFC_ACCEPT, status: str = "101 Switching Protocols") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode()


def connection_receiving(data: bytes) -> tuple[WebSocketConnection, FakeSocket]:
    fake = FakeSocket(data)
    return WebSocketConnection(fake, random_bytes=lambda size: MASK), fake


def unmask_sent_frame(frame: bytes) -> tuple[int, bytes]:
    """Decode one client frame, checking it is masked as RFC 6455 requires."""
    opcode = frame[0] & 0x0F
    assert frame[1] & 0x80, "client frames must be masked"
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        (length,) = struct.unpack("!H", frame[2:4])
        offset = 4
    elif length == 127:
        (length,) = struct.unpack("!Q", frame[2:10])
        offset = 10
    mask = frame[offset : offset + 4]
    masked = frame[offset + 4 : offset + 4 + length]
    return opcode, bytes(byte ^ mask[index % 4] for index, byte in enumerate(masked))


# --- URL -----------------------------------------------------------------------


def test_parses_a_local_devtools_url() -> None:
    assert parse_websocket_url("ws://127.0.0.1:8080/devtools/page/AB12") == (
        "127.0.0.1",
        8080,
        "/devtools/page/AB12",
    )


@pytest.mark.parametrize(
    "url", ["wss://127.0.0.1:8080/x", "ws://example.com:8080/x", "http://127.0.0.1/x"]
)
def test_refuses_anything_but_plain_websocket_to_this_machine(url: str) -> None:
    with pytest.raises(WebSocketError):
        parse_websocket_url(url)


# --- Handshake -----------------------------------------------------------------


def test_the_accept_key_matches_the_rfc_example() -> None:
    assert accept_key_for(RFC_KEY) == RFC_ACCEPT


def test_the_handshake_sends_an_upgrade_request_and_checks_the_accept_key() -> None:
    fake = FakeSocket(handshake_reply())

    open_websocket(
        "ws://127.0.0.1:8080/devtools/page/AB12",
        timeout=5,
        connect=lambda host, port, timeout: fake,
        random_bytes=lambda size: RFC_KEY_BYTES,
    )

    request = fake.sent.decode()
    assert request.startswith("GET /devtools/page/AB12 HTTP/1.1\r\n")
    assert "Host: 127.0.0.1:8080\r\n" in request
    assert "Upgrade: websocket\r\n" in request
    assert f"Sec-WebSocket-Key: {RFC_KEY}\r\n" in request
    assert "Sec-WebSocket-Version: 13\r\n" in request
    assert request.endswith("\r\n\r\n")


def test_a_wrong_accept_key_is_refused_and_the_socket_closed() -> None:
    fake = FakeSocket(handshake_reply(accept=base64.b64encode(hashlib.sha1().digest()).decode()))

    with pytest.raises(WebSocketError, match="Sec-WebSocket-Accept"):
        open_websocket(
            "ws://127.0.0.1:8080/x",
            timeout=5,
            connect=lambda host, port, timeout: fake,
            random_bytes=lambda size: RFC_KEY_BYTES,
        )
    assert fake.closed


def test_a_refused_upgrade_reports_the_status() -> None:
    fake = FakeSocket(handshake_reply(status="403 Forbidden"))

    with pytest.raises(WebSocketError, match="403"):
        open_websocket(
            "ws://127.0.0.1:8080/x",
            timeout=5,
            connect=lambda host, port, timeout: fake,
            random_bytes=lambda size: RFC_KEY_BYTES,
        )


def test_a_frame_that_arrives_with_the_handshake_reply_is_not_lost() -> None:
    fake = FakeSocket(handshake_reply() + server_frame(TEXT, b"early"), chunk_size=4096)

    connection = open_websocket(
        "ws://127.0.0.1:8080/x",
        timeout=5,
        connect=lambda host, port, timeout: fake,
        random_bytes=lambda size: RFC_KEY_BYTES,
    )

    assert connection.receive_text(timeout=5) == "early"


# --- Sending -------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 125, 126, 65535, 65536])
def test_sent_frames_are_masked_and_use_the_right_length_encoding(length: int) -> None:
    payload = b"x" * length

    frame = encode_client_frame(TEXT, payload, MASK)

    assert frame[0] == 0x80 | TEXT
    assert unmask_sent_frame(frame) == (TEXT, payload)


def test_send_text_writes_one_masked_text_frame() -> None:
    connection, fake = connection_receiving(b"")

    connection.send_text('{"id":1}')

    assert unmask_sent_frame(fake.sent) == (TEXT, b'{"id":1}')


# --- Receiving -----------------------------------------------------------------


@pytest.mark.parametrize("length", [5, 126, 70000])
def test_receives_text_with_each_length_encoding(length: int) -> None:
    text = "a" * length
    connection, _ = connection_receiving(server_frame(TEXT, text.encode()))

    assert connection.receive_text(timeout=5) == text


def test_joins_a_fragmented_message() -> None:
    data = (
        server_frame(TEXT, b"Hel", final=False)
        + server_frame(CONTINUATION, b"lo ", final=False)
        + server_frame(CONTINUATION, b"there")
    )
    connection, _ = connection_receiving(data)

    assert connection.receive_text(timeout=5) == "Hello there"


def test_answers_a_ping_with_a_pong_even_between_fragments() -> None:
    data = (
        server_frame(TEXT, b"a", final=False)
        + server_frame(PING, b"are you there")
        + server_frame(CONTINUATION, b"b")
    )
    connection, fake = connection_receiving(data)

    assert connection.receive_text(timeout=5) == "ab"
    assert unmask_sent_frame(fake.sent) == (PONG, b"are you there")


def test_ignores_an_unsolicited_pong() -> None:
    connection, _ = connection_receiving(server_frame(PONG, b"") + server_frame(TEXT, b"ok"))

    assert connection.receive_text(timeout=5) == "ok"


def test_a_close_frame_is_answered_and_reported() -> None:
    connection, fake = connection_receiving(server_frame(CLOSE, struct.pack("!H", 1001)))

    with pytest.raises(WebSocketClosedError):
        connection.receive_text(timeout=5)
    assert unmask_sent_frame(fake.sent)[0] == CLOSE


def test_a_dropped_connection_is_reported_as_closed() -> None:
    connection, _ = connection_receiving(server_frame(TEXT, b"cut")[:3])

    with pytest.raises(WebSocketClosedError):
        connection.receive_text(timeout=5)


def test_a_silent_server_times_out_with_the_given_timeout() -> None:
    connection, fake = connection_receiving(b"TIMEOUT")

    with pytest.raises(WebSocketTimeoutError):
        connection.receive_text(timeout=2.5)
    assert fake.timeouts[-1] == 2.5


def test_a_masked_server_frame_is_a_protocol_error() -> None:
    masked = bytes([0x80 | TEXT, 0x80 | 2]) + MASK + b"hi"
    connection, _ = connection_receiving(masked)

    with pytest.raises(WebSocketError, match="masked"):
        connection.receive_text(timeout=5)


def test_close_sends_a_close_frame_and_closes_the_socket() -> None:
    connection, fake = connection_receiving(b"")

    connection.close()

    assert unmask_sent_frame(fake.sent)[0] == CLOSE
    assert fake.closed
