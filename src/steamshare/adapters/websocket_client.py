"""A minimal WebSocket client (RFC 6455), just enough to talk to Steam's debugger.

Why it exists: Steam's Chrome DevTools endpoint speaks WebSocket, and
steamshare may only use the standard library at runtime (CONTRIBUTING.md,
rule 1), which has no WebSocket client. This one covers what that endpoint
needs and nothing more:

- plain `ws://` to this machine only; no TLS, no proxies, no extensions;
- the HTTP/1.1 upgrade handshake, with its `Sec-WebSocket-Accept` check;
- masked text frames out (the RFC requires clients to mask);
- frames in with 7-, 16-, or 64-bit lengths, fragmented messages, ping
  (answered with pong), and close.

The socket and the random source are parameters, so the tests replay
scripted bytes instead of opening a connection.
"""

import base64
import contextlib
import hashlib
import os
import socket
import struct
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

WEBSOCKET_SCHEME = "ws"
# The debugger listens on loopback only; refusing anything else keeps this
# client from ever sending Steam's commands across the network.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_PORT = 80

# RFC 6455, section 1.3: the server proves it understood the upgrade by
# hashing the client's key with this fixed GUID.
HANDSHAKE_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HANDSHAKE_KEY_SIZE = 16
WEBSOCKET_VERSION = "13"
SWITCHING_PROTOCOLS_STATUS = "101"
HEADER_END = b"\r\n\r\n"
ACCEPT_HEADER = "sec-websocket-accept"

# Frame layout (RFC 6455, section 5.2).
FINAL_FRAGMENT_BIT = 0x80
OPCODE_BITS = 0x0F
MASKED_BIT = 0x80
LENGTH_BITS = 0x7F
MASK_SIZE = 4
# A 7-bit length up to this; above it, the marker says which longer form follows.
LONGEST_SHORT_LENGTH = 125
SIXTEEN_BIT_LENGTH_MARKER = 126
SIXTY_FOUR_BIT_LENGTH_MARKER = 127
LONGEST_SIXTEEN_BIT_LENGTH = 0xFFFF

CONTINUATION_OPCODE = 0x0
TEXT_OPCODE = 0x1
CLOSE_OPCODE = 0x8
PING_OPCODE = 0x9
PONG_OPCODE = 0xA

RECEIVE_CHUNK_SIZE = 65536


class WebSocketError(Exception):
    """The connection failed or the server broke the protocol."""


class WebSocketClosedError(WebSocketError):
    """The server closed the connection, or it dropped."""


class WebSocketTimeoutError(WebSocketError):
    """The server sent nothing within the timeout."""


class ByteSocket(Protocol):
    """The part of `socket.socket` the client uses."""

    def recv(self, size: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...
    def settimeout(self, timeout: float | None) -> None: ...
    def close(self) -> None: ...


# Returns `size` random bytes; `os.urandom` outside tests.
RandomBytes = Callable[[int], bytes]
# Opens a TCP connection to (host, port) with a timeout.
Connector = Callable[[str, int, float], ByteSocket]


def connect_tcp(host: str, port: int, timeout: float) -> ByteSocket:
    return socket.create_connection((host, port), timeout=timeout)


# --- URL and handshake ---------------------------------------------------------


def parse_websocket_url(url: str) -> tuple[str, int, str]:
    """Return the host, port, and request path of a local `ws://` URL."""
    parts = urlsplit(url)
    if parts.scheme != WEBSOCKET_SCHEME or parts.hostname not in LOOPBACK_HOSTS:
        raise WebSocketError(f"{url} is not a ws:// URL on this machine")
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    return parts.hostname, parts.port or DEFAULT_PORT, path


def accept_key_for(key: str) -> str:
    digest = hashlib.sha1((key + HANDSHAKE_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def open_websocket(
    url: str,
    *,
    timeout: float,
    connect: Connector = connect_tcp,
    random_bytes: RandomBytes = os.urandom,
) -> "WebSocketConnection":
    """Connect and complete the upgrade handshake; raise WebSocketError if it fails."""
    host, port, path = parse_websocket_url(url)
    try:
        raw_socket = connect(host, port, timeout)
    except OSError as error:
        raise WebSocketError(f"could not connect to {host}:{port}: {error}") from error
    try:
        leftover = _handshake(raw_socket, host, port, path, timeout, random_bytes)
    except BaseException:
        raw_socket.close()
        raise
    return WebSocketConnection(raw_socket, random_bytes=random_bytes, buffered=leftover)


def _handshake(
    raw_socket: ByteSocket,
    host: str,
    port: int,
    path: str,
    timeout: float,
    random_bytes: RandomBytes,
) -> bytes:
    """Send the upgrade request, check the reply, and return any bytes read past it."""
    key = base64.b64encode(random_bytes(HANDSHAKE_KEY_SIZE)).decode("ascii")
    # No Origin header: Chrome's DevTools endpoint refuses origins it was not
    # told to allow, and accepts a client that sends none.
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: {WEBSOCKET_VERSION}\r\n\r\n"
    )
    raw_socket.settimeout(timeout)
    _send(raw_socket, request.encode("ascii"))
    reply = b""
    while HEADER_END not in reply:
        reply += _receive_chunk(raw_socket)
    head, leftover = reply.split(HEADER_END, 1)
    status_line, *header_lines = head.decode("iso-8859-1").split("\r\n")
    if status_line.split(" ")[1:2] != [SWITCHING_PROTOCOLS_STATUS]:
        raise WebSocketError(f"the server refused the WebSocket upgrade: {status_line}")
    headers = _parse_headers(header_lines)
    if headers.get(ACCEPT_HEADER) != accept_key_for(key):
        raise WebSocketError("the server's Sec-WebSocket-Accept does not match the key sent")
    return leftover


def _parse_headers(lines: list[str]) -> dict[str, str]:
    # HTTP header names are case-insensitive.
    headers = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


# --- Frames --------------------------------------------------------------------


def encode_client_frame(opcode: int, payload: bytes, mask: bytes) -> bytes:
    """Build one final, masked frame, as a client must send it."""
    header = bytes([FINAL_FRAGMENT_BIT | opcode])
    length = len(payload)
    if length <= LONGEST_SHORT_LENGTH:
        header += bytes([MASKED_BIT | length])
    elif length <= LONGEST_SIXTEEN_BIT_LENGTH:
        header += bytes([MASKED_BIT | SIXTEEN_BIT_LENGTH_MARKER]) + struct.pack("!H", length)
    else:
        header += bytes([MASKED_BIT | SIXTY_FOUR_BIT_LENGTH_MARKER]) + struct.pack("!Q", length)
    return header + mask + _apply_mask(payload, mask)


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    # Masking XORs each byte with the key's bytes in turn; applying it twice
    # restores the payload.
    return bytes(byte ^ mask[index % MASK_SIZE] for index, byte in enumerate(payload))


class WebSocketConnection:
    """An open connection: send text, receive whole text messages, close."""

    def __init__(
        self,
        raw_socket: ByteSocket,
        *,
        random_bytes: RandomBytes = os.urandom,
        buffered: bytes = b"",
    ) -> None:
        self._socket = raw_socket
        self._random_bytes = random_bytes
        # Bytes already read from the socket but not yet parsed.
        self._buffer = buffered

    def send_text(self, text: str) -> None:
        self._send_frame(TEXT_OPCODE, text.encode("utf-8"))

    def receive_text(self, *, timeout: float) -> str:
        """Return the next whole message, answering pings while waiting.

        `timeout` limits each wait for data from the server.
        """
        self._socket.settimeout(timeout)
        fragments: list[bytes] = []
        while True:
            final, opcode, payload = self._receive_frame()
            if opcode == PING_OPCODE:
                self._send_frame(PONG_OPCODE, payload)
            elif opcode == CLOSE_OPCODE:
                # The RFC asks for a close frame in reply before dropping.
                self.close()
                raise WebSocketClosedError("the server closed the connection")
            elif opcode in (TEXT_OPCODE, CONTINUATION_OPCODE):
                fragments.append(payload)
                if final:
                    return b"".join(fragments).decode("utf-8")
            elif opcode != PONG_OPCODE:
                raise WebSocketError(f"unexpected frame type {opcode:#x}")

    def close(self) -> None:
        # Best effort: the server may already be gone, and there is nothing
        # left to do about it.
        with contextlib.suppress(WebSocketError):
            self._send_frame(CLOSE_OPCODE, b"")
        self._socket.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = self._random_bytes(MASK_SIZE)
        _send(self._socket, encode_client_frame(opcode, payload, mask))

    def _receive_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exactly(2)
        if second & MASKED_BIT:
            raise WebSocketError("the server sent a masked frame, which RFC 6455 forbids")
        length = second & LENGTH_BITS
        if length == SIXTEEN_BIT_LENGTH_MARKER:
            (length,) = struct.unpack("!H", self._read_exactly(2))
        elif length == SIXTY_FOUR_BIT_LENGTH_MARKER:
            (length,) = struct.unpack("!Q", self._read_exactly(8))
        payload = self._read_exactly(length)
        return bool(first & FINAL_FRAGMENT_BIT), first & OPCODE_BITS, payload

    def _read_exactly(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._buffer += _receive_chunk(self._socket)
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


# --- Socket calls, with errors translated ---------------------------------------


def _send(raw_socket: ByteSocket, data: bytes) -> None:
    try:
        raw_socket.sendall(data)
    except OSError as error:
        raise WebSocketClosedError(f"the connection dropped: {error}") from error


def _receive_chunk(raw_socket: ByteSocket) -> bytes:
    try:
        chunk = raw_socket.recv(RECEIVE_CHUNK_SIZE)
    except TimeoutError as error:
        raise WebSocketTimeoutError("the server sent nothing in time") from error
    except OSError as error:
        raise WebSocketClosedError(f"the connection dropped: {error}") from error
    if not chunk:
        raise WebSocketClosedError("the server closed the connection")
    return chunk
