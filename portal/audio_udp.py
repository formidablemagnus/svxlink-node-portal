"""UDP transport for node audio between the agent and the portal.

Request:  MAGIC | id_len(1) | node_id | ts(4) | seq(4) | mac(16) | body
Reply:    MAGIC | ts(4) | seq(4) | mac(16) | body
The MAC is HMAC-SHA256 keyed with the node's token hash, truncated to 16 bytes.
The agent holds the token and derives the same key. Keep this file in sync with
the matching helpers in agent/audio.py.
"""

import hashlib
import hmac
import logging
import socket
import struct
import threading
import time

MAGIC = b"SVA1"
MAC_BYTES = 16
MAX_CLOCK_SKEW_SECONDS = 60
MAX_REPLY_MIC_BYTES = 1000
KEY_CACHE_SECONDS = 30
NEGATIVE_CACHE_SECONDS = 5
SEEN_LIMIT = 256


def derive_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")


def _mac(key, direction, head, body):
    return hmac.new(key, direction + head + body, hashlib.sha256).digest()[:MAC_BYTES]


def parse_request(packet):
    """Return (node_id, ts, seq, mac, body, head) or None for malformed packets."""
    if len(packet) < len(MAGIC) + 1 or not packet.startswith(MAGIC):
        return None
    id_len = packet[4]
    start = 5 + id_len
    if id_len == 0 or len(packet) < start + 8 + MAC_BYTES:
        return None
    try:
        node_id = packet[5:start].decode("ascii")
    except UnicodeDecodeError:
        return None
    ts, seq = struct.unpack(">II", packet[start:start + 8])
    mac = packet[start + 8:start + 8 + MAC_BYTES]
    return node_id, ts, seq, mac, packet[start + 8 + MAC_BYTES:], packet[4:start + 8]


def build_reply(key, ts, seq, body):
    head = struct.pack(">II", ts, seq)
    return MAGIC + head + _mac(key, b"R", head, body) + body


class AudioUdpServer:
    def __init__(self, host, port, hub, lookup_key, touch):
        self.hub = hub
        self.lookup_key = lookup_key
        self.touch = touch
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.keys = {}
        self.seen = {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.serve, name="audio-udp", daemon=True)
        self.thread.start()

    def close(self):
        self.sock.close()

    def _key(self, node_id):
        now = time.monotonic()
        cached = self.keys.get(node_id)
        if cached and cached[1] > now:
            return cached[0]
        key = self.lookup_key(node_id)
        ttl = KEY_CACHE_SECONDS if key else NEGATIVE_CACHE_SECONDS
        if len(self.keys) > SEEN_LIMIT:
            self.keys.clear()
        self.keys[node_id] = (key, now + ttl)
        return key

    def handle(self, packet):
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
        self.seen[node_id] = (ts, seq)
        if body:
            self.hub.add_audio(node_id, body)
        self.touch(node_id)
        flags, mic = self.hub.take_for_agent(node_id, MAX_REPLY_MIC_BYTES)
        return build_reply(key, ts, seq, bytes([flags]) + mic), node_id

    def serve(self):
        while True:
            try:
                packet, address = self.sock.recvfrom(65535)
            except OSError:
                return
            try:
                result = self.handle(packet)
                if result:
                    self.sock.sendto(result[0], address)
            except Exception:  # one bad packet must never stop the audio relay
                logging.exception("Fel vid hantering av UDP-ljudpaket.")
