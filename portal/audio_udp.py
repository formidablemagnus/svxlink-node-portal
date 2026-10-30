"""UDP transport for node audio between the agent and the portal.

Request:  MAGIC | id_len(1) | node_id | ts(4) | seq(4) | mac(16) | body
Reply:    MAGIC | ts(4) | seq(4) | mac(16) | body
The MAC is HMAC-SHA256 keyed with the node's token hash, truncated to 16 bytes.
The agent holds the token and derives the same key. Keep this file in sync with
the matching helpers in agent/audio.py.
"""
from __future__ import annotations


import hashlib
import hmac
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

MAGIC: bytes = b"SVA1"
MAC_BYTES: int = 16
MAX_CLOCK_SKEW_SECONDS: int = 60
MAX_REPLY_MIC_BYTES: int = 1000
MAX_REPLY_DATAGRAMS: int = 4
MAX_REQUEST_BODY_BYTES: int = 48 * 1024
KEY_CACHE_SECONDS: int = 30
NEGATIVE_CACHE_SECONDS: int = 5
SEEN_LIMIT: int = 256


def _mac(key: bytes, direction: bytes, head: bytes, body: bytes) -> bytes:
    return hmac.new(key, direction + head + body, hashlib.sha256).digest()[:MAC_BYTES]


def parse_request(packet: bytes) -> tuple[str, int, int, bytes, bytes, bytes] | None:
    """Return (node_id, ts, seq, mac, body, head) or None for malformed packets."""
    if len(packet) < len(MAGIC) + 1 or not packet.startswith(MAGIC):
        return None
    id_len = packet[4]
    start = 5 + id_len
    if id_len == 0 or len(packet) < start + 8 + MAC_BYTES:
        return None
    if len(packet) - (start + 8 + MAC_BYTES) > MAX_REQUEST_BODY_BYTES:
        return None
    try:
        node_id = packet[5:start].decode("ascii")
    except UnicodeDecodeError:
        return None
    ts, seq = struct.unpack(">II", packet[start:start + 8])
    mac = packet[start + 8:start + 8 + MAC_BYTES]
    return node_id, ts, seq, mac, packet[start + 8 + MAC_BYTES:], packet[4:start + 8]


def build_reply(key: bytes, ts: int, seq: int, body: bytes) -> bytes:
    head = struct.pack(">II", ts, seq)
    return MAGIC + head + _mac(key, b"R", head, body) + body


class AudioUdpServer:
    def __init__(self, host: str, port: int, hub: Any, lookup_key: Callable[[str], bytes | None], touch: Callable[[str], None]) -> None:
        self.hub = hub
        self.lookup_key = lookup_key
        self.touch = touch
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((host, port))
            self.sock.settimeout(1.0)
        except BaseException:
            self.sock.close()
            raise
        self.port: int = self.sock.getsockname()[1]
        self.keys: dict[str, tuple[bytes | None, float]] = {}
        self.seen: dict[str, tuple[int, int]] = {}
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        thread = threading.Thread(target=self.serve, name="audio-udp", daemon=True)
        try:
            thread.start()
        except BaseException:
            self.sock.close()
            raise
        self.thread = thread

    def close(self) -> None:
        self.sock.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.5)

    def _key(self, node_id: str) -> bytes | None:
        now = time.monotonic()
        cached = self.keys.get(node_id)
        if cached and cached[1] > now:
            return cached[0]
        key = self.lookup_key(node_id)
        ttl = KEY_CACHE_SECONDS if key else NEGATIVE_CACHE_SECONDS
        if node_id not in self.keys and len(self.keys) >= SEEN_LIMIT:
            self.keys.pop(next(iter(self.keys)))
        self.keys[node_id] = (key, now + ttl)
        return key

    def handle(self, packet: bytes) -> tuple[list[bytes], str] | None:
        """Return (reply, node_id) or None when the packet must be ignored."""
        parsed = parse_request(packet)
        if parsed is None:
            return None
        node_id, ts, seq, mac, body, head = parsed
        key = self._key(node_id)
        if not key or not hmac.compare_digest(mac, _mac(key, b"Q", head, body)):
            return None
        if abs(time.time() - ts) > MAX_CLOCK_SKEW_SECONDS:
            return None
        if (ts, seq) <= self.seen.get(node_id, (-1, -1)):
            return None
        if node_id not in self.seen and len(self.seen) >= SEEN_LIMIT:
            self.seen.pop(next(iter(self.seen)))
        self.seen[node_id] = (ts, seq)
        if body:
            self.hub.add_audio(node_id, body)
        self.touch(node_id)
        flags, mic = self.hub.take_for_agent(node_id, MAX_REPLY_MIC_BYTES * MAX_REPLY_DATAGRAMS)
        pieces = [mic[i:i + MAX_REPLY_MIC_BYTES] for i in range(0, len(mic), MAX_REPLY_MIC_BYTES)]
        pieces = pieces or [b""]
        # Flag bit 2 means another datagram with more microphone audio follows.
        replies = [
            build_reply(key, ts, seq, bytes([flags | (4 if index < len(pieces) - 1 else 0)]) + piece)
            for index, piece in enumerate(pieces)
        ]
        return replies, node_id

    def serve(self) -> None:
        while True:
            try:
                packet, address = self.sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                result = self.handle(packet)
                if result:
                    for reply in result[0]:
                        self.sock.sendto(reply, address)
            except Exception:  # one bad packet must never stop the audio relay
                logging.warning("Fel vid hantering av UDP-ljudpaket.")
