#!/usr/bin/env python3
"""Small standard-library HTTP server for the SvxLink node portal."""
import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from portal.audio_udp import AudioUdpServer
from portal.reflector import REFLECTOR_HUB
from portal.version import read_version

ROOT = Path(__file__).resolve().parent.parent
PORTAL_VERSION = read_version()
SVXLINK_RELEASE_URL = "https://api.github.com/repos/sm0svx/svxlink/releases/latest"
SVXLINK_VERSIONS_URL = "https://raw.githubusercontent.com/sm0svx/svxlink/%s/src/versions"
_latest_svxlink = {"version": "", "next": 0.0, "lock": threading.Lock(), "busy": False}


def _fetch_latest_svxlink_version():
    version = ""
    try:
        request = urllib.request.Request(SVXLINK_RELEASE_URL, headers={"User-Agent": "svxlink-node-portal"})
        with urllib.request.urlopen(request, timeout=10) as response:
            release = json.loads(response.read(262144).decode("utf-8", "replace"))
        tag = str(release.get("tag_name", ""))
        if re.fullmatch(r"v?\d+(?:\.\d+)+", tag) and not release.get("prerelease"):
            request = urllib.request.Request(SVXLINK_VERSIONS_URL % tag, headers={"User-Agent": "svxlink-node-portal"})
            with urllib.request.urlopen(request, timeout=10) as response:
                text = response.read(65536).decode("utf-8", "replace")
            match = re.search(r"^SVXLINK=(\d+(?:\.\d+)+)\s*$", text, re.MULTILINE)
            version = match.group(1) if match else ""
    except Exception as exc:
        logging.warning("Kunde inte hämta senaste SvxLink-version: %s", exc)
    with _latest_svxlink["lock"]:
        if version:
            _latest_svxlink["version"] = version
        _latest_svxlink["next"] = time.monotonic() + (6 * 3600 if version else 600)
        _latest_svxlink["busy"] = False


_stable_releases = {"versions": [], "next": 0.0, "lock": threading.Lock(), "busy": False}
SVXLINK_RELEASES_URL = "https://api.github.com/repos/sm0svx/svxlink/releases?per_page=30"
STABLE_RELEASES_SHOWN = 4


def _fetch_stable_releases():
    versions = []
    try:
        request = urllib.request.Request(SVXLINK_RELEASES_URL, headers={"User-Agent": "svxlink-node-portal"})
        with urllib.request.urlopen(request, timeout=10) as response:
            releases = json.loads(response.read(1024 * 1024).decode("utf-8", "replace"))
        for release in releases if isinstance(releases, list) else []:
            tag = str(release.get("tag_name", "")) if isinstance(release, dict) else ""
            if (
                re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", tag)
                and release.get("prerelease") is False
                and release.get("draft") is not True
                and tag not in versions
            ):
                versions.append(tag)
        versions = versions[:STABLE_RELEASES_SHOWN]
    except Exception as exc:
        logging.warning("Kunde inte hämta SvxLink-releaser: %s", exc)
    with _stable_releases["lock"]:
        if versions:
            _stable_releases["versions"] = versions
        _stable_releases["next"] = time.monotonic() + (6 * 3600 if versions else 600)
        _stable_releases["busy"] = False


def stable_svxlink_releases():
    """Return the newest stable release tags (newest first); refresh in the background."""
    with _stable_releases["lock"]:
        if time.monotonic() >= _stable_releases["next"] and not _stable_releases["busy"]:
            _stable_releases["busy"] = True
            threading.Thread(target=_fetch_stable_releases, daemon=True).start()
        return list(_stable_releases["versions"])


def latest_svxlink_version():
    """Return the cached latest stable release version; refresh in the background when stale."""
    with _latest_svxlink["lock"]:
        if time.monotonic() >= _latest_svxlink["next"] and not _latest_svxlink["busy"]:
            _latest_svxlink["busy"] = True
            threading.Thread(target=_fetch_latest_svxlink_version, daemon=True).start()
        return _latest_svxlink["version"]
WEB_ROOT = ROOT / "web"
MAX_REQUEST_BYTES = 64 * 1024
MAX_LOG_LINES = 50
MAX_LOG_LINE_BYTES = 512
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_COMMAND_REQUEST_BYTES = MAX_CONFIG_BYTES * 6 + 65536
MAX_COMMAND_RESULT_BYTES = MAX_CONFIG_BYTES * 6 + 65536
MAX_DTMF_SEQUENCE_LENGTH = 64
MAX_DTMF_NODES = 100
MAX_DTMF_BUTTONS = 30
MAX_AUDIT_ROWS = 5000
AUDIT_PAGE_SIZE = 200
USER_ROLES = ("admin", "operator")
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,40}")
MIN_USER_PASSWORD_LENGTH = 8
PASSWORD_ITERATIONS = 200_000
MAX_SCHEDULES = 100
MAX_NODE_INFO_BYTES = 64 * 1024
AGENT_COMMAND_TTL_SECONDS = 300
AGENT_UPDATE_TIMEOUT_SECONDS = 4 * 60 * 60
AGENT_UPDATE_COMMAND_TTL_SECONDS = AGENT_UPDATE_TIMEOUT_SECONDS + 60 * 60
UPDATE_HEARTBEAT_TIMEOUT_SECONDS = 180
UPDATE_PENDING_TIMEOUT_SECONDS = 600
UPDATE_PROGRESS_MESSAGES = {
    "starting": "Uppdateringen har startat.",
    "working": "SvxLink byggs och installeras. Uppdateringen pågår.",
}
ONLINE_WINDOW_SECONDS = 30
MAX_LIVE_LOG_LINES = 200
MAX_LOG_LINES_PER_REQUEST = 100
TOUCH_INTERVAL_SECONDS = 5
CONFIG_DOCUMENTS = {"svxlink.conf", "node_info.json"}
CSS = "text/css; charset=utf-8"
JS = "application/javascript; charset=utf-8"
STATIC_ASSETS = {
    "style.css": CSS,
    "sidebar.js": JS,
    "index.js": JS,
    "node.js": JS,
    "config.js": JS,
    "node-info-form.js": JS,
    "schedules.js": JS,
    "service-log.js": JS,
    "dtmf.js": JS,
    "audio.js": JS,
    "admin.js": JS,
    "login.js": JS,
}
PUBLIC_ASSETS = {"style.css", "login.js"}
SESSION_COOKIE = "svx_session"
SESSION_TTL_SECONDS = 12 * 3600
SESSION_REFRESH_SECONDS = 300
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 300
_login_failures = {}
_login_lock = threading.Lock()
ADMIN_NODE_PAGES = {
    "config": "config.html",
    "nodeinfo": "nodeinfo.html",
    "schedules": "schedules.html",
}
NODE_PAGES = {"service-log": "service-log.html"}
VALID_RADIO_MODES = {"tx", "rx", "idle", "unknown"}
MAX_RADIO_EVENTS_PER_REQUEST = 100
MAX_RADIO_VIEWERS_PER_NODE = 20
RADIO_STREAM_KEEPALIVE_SECONDS = 15
MAX_AUDIO_LISTENERS_PER_NODE = 10
AUDIO_CHUNK_BYTES = 3200
AUDIO_BACKLOG_CHUNKS = 100
MAX_MIC_BUFFER_BYTES = 64 * 1024
MAX_AUDIO_REQUEST_BYTES = 48 * 1024
PTT_TIMEOUT_SECONDS = 1.5


class RadioHub:
    """In-memory fan-out of live radio events from agents to browser viewers."""

    def __init__(self):
        self.condition = threading.Condition()
        self.nodes = {}

    def _node(self, node_id):
        return self.nodes.setdefault(
            node_id, {"seq": 0, "events": deque(maxlen=500), "viewers": 0, "latest": None}
        )

    def add_events(self, node_id, events):
        with self.condition:
            node = self._node(node_id)
            for event in events:
                node["seq"] += 1
                node["events"].append((node["seq"], event))
                node["latest"] = event
            self.condition.notify_all()

    def watched(self, node_id):
        with self.condition:
            node = self.nodes.get(node_id)
            return bool(node and node["viewers"] > 0)

    def subscribe(self, node_id):
        with self.condition:
            node = self._node(node_id)
            if node["viewers"] >= MAX_RADIO_VIEWERS_PER_NODE:
                return None
            node["viewers"] += 1
            return node["seq"], node["latest"]

    def unsubscribe(self, node_id):
        with self.condition:
            node = self._node(node_id)
            node["viewers"] = max(0, node["viewers"] - 1)

    def wait(self, node_id, after, timeout):
        with self.condition:
            node = self._node(node_id)
            if node["seq"] <= after:
                self.condition.wait(timeout)
            return node["seq"], [(seq, event) for seq, event in node["events"] if seq > after]


RADIO_HUB = RadioHub()


class AudioHub:
    """In-memory relay of live node audio to browsers and browser microphone audio to nodes."""

    def __init__(self):
        self.condition = threading.Condition()
        self.nodes = {}

    def _node(self, node_id):
        return self.nodes.setdefault(node_id, {
            "seq": 0, "chunks": deque(maxlen=AUDIO_BACKLOG_CHUNKS), "listeners": 0,
            "mic": bytearray(), "ptt_user": None, "ptt_seen": 0.0,
        })

    def add_audio(self, node_id, pcm):
        pcm = bytes(pcm[: len(pcm) // 2 * 2])
        with self.condition:
            node = self._node(node_id)
            for offset in range(0, len(pcm), AUDIO_CHUNK_BYTES):
                node["seq"] += 1
                node["chunks"].append((node["seq"], pcm[offset:offset + AUDIO_CHUNK_BYTES]))
            self.condition.notify_all()

    def subscribe(self, node_id):
        with self.condition:
            node = self._node(node_id)
            if node["listeners"] >= MAX_AUDIO_LISTENERS_PER_NODE:
                return None
            node["listeners"] += 1
            return node["seq"]

    def unsubscribe(self, node_id):
        with self.condition:
            node = self._node(node_id)
            node["listeners"] = max(0, node["listeners"] - 1)

    def wait(self, node_id, after, timeout):
        with self.condition:
            node = self._node(node_id)
            if node["seq"] <= after:
                self.condition.wait(timeout)
            return node["seq"], [data for seq, data in node["chunks"] if seq > after]

    def _owner(self, node, now):
        if node["ptt_user"] and now - node["ptt_seen"] > PTT_TIMEOUT_SECONDS:
            node["ptt_user"] = None
        return node["ptt_user"]

    def wanted(self, node_id):
        with self.condition:
            node = self.nodes.get(node_id)
            if node is None:
                return False
            return node["listeners"] > 0 or self._owner(node, time.monotonic()) is not None

    def push_mic(self, node_id, user, ptt, pcm):
        """Return (accepted, started, owner). ``started`` is true on a new transmission."""
        now = time.monotonic()
        with self.condition:
            node = self._node(node_id)
            owner = self._owner(node, now)
            if owner not in (None, user):
                return False, False, owner
            if not ptt:
                if owner == user:
                    node["ptt_user"] = None
                return True, False, None
            node["ptt_user"] = user
            node["ptt_seen"] = now
            node["mic"].extend(pcm[: len(pcm) // 2 * 2])
            del node["mic"][:-MAX_MIC_BUFFER_BYTES]
            return True, owner is None, user

    def take_for_agent(self, node_id, max_mic=None):
        """Return (flags, mic_pcm) for an agent audio exchange."""
        now = time.monotonic()
        with self.condition:
            node = self.nodes.get(node_id)
            if node is None:
                return 0, b""
            ptt = self._owner(node, now) is not None
            limit = len(node["mic"]) if max_mic is None else max_mic // 2 * 2
            mic = bytes(node["mic"][:limit])
            del node["mic"][:limit]
            wanted = node["listeners"] > 0 or ptt or bool(mic)
            return (1 if wanted else 0) | (2 if ptt or mic else 0), mic


AUDIO_HUB = AudioHub()
AUDIO_UDP = None


def node_audio_key(node_id):
    with database() as connection:
        row = connection.execute(
            "SELECT token_hash FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
    return row["token_hash"].encode("ascii") if row else None


def start_audio_udp():
    """Start the optional UDP audio endpoint when PORTAL_AUDIO_UDP_PORT is set."""
    global AUDIO_UDP
    raw_port = os.environ.get("PORTAL_AUDIO_UDP_PORT", "").strip()
    if not raw_port or raw_port == "0":
        return
    host = os.environ.get("PORTAL_AUDIO_UDP_HOST", "0.0.0.0")
    AUDIO_UDP = AudioUdpServer(host, int(raw_port), AUDIO_HUB, node_audio_key, touch_node)
    AUDIO_UDP.start()
    logging.info("UDP-ljud lyssnar på %s:%s.", host, AUDIO_UDP.port)


class LiveLogStore:
    """In-memory ring buffer of streamed log lines per node."""

    def __init__(self):
        self.lock = threading.Lock()
        self.nodes = {}

    def add(self, node_id, lines):
        with self.lock:
            buffer = self.nodes.setdefault(node_id, deque(maxlen=MAX_LIVE_LOG_LINES))
            buffer.extend(lines)

    def seed(self, node_id, lines):
        with self.lock:
            if not self.nodes.get(node_id):
                self.nodes[node_id] = deque(lines, maxlen=MAX_LIVE_LOG_LINES)

    def get(self, node_id):
        with self.lock:
            return list(self.nodes.get(node_id, ()))


LIVE_LOGS = LiveLogStore()
_touch_lock = threading.Lock()
_last_touch = {}


def touch_node(node_id):
    """Refresh last_seen from agent traffic, writing to the database at most every few seconds."""
    monotonic = time.monotonic()
    with _touch_lock:
        if monotonic - _last_touch.get(node_id, -TOUCH_INTERVAL_SECONDS) < TOUCH_INTERVAL_SECONDS:
            return
        _last_touch[node_id] = monotonic
    with database() as connection:
        connection.execute(
            "UPDATE nodes SET last_seen = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), node_id),
        )


def validate_log_lines(payload):
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LOG_LINES_PER_REQUEST:
        raise ValueError("'lines' måste vara en lista med 1–100 loggrader.")
    clean = []
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("Varje loggrad måste vara text.")
        clean.append(line.encode("utf-8")[:MAX_LOG_LINE_BYTES].decode("utf-8", errors="ignore"))
    return clean
COMMAND_CLEANUP_INTERVAL_SECONDS = 60
_command_cleanup_lock = threading.Lock()
_last_command_cleanup = {}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def database_path():
    return Path(os.environ.get("PORTAL_DATABASE", "/var/lib/svxlink-node-portal/portal.sqlite3"))


def agent_command_ttl(payload):
    if isinstance(payload, dict) and payload.get("action") == "update":
        return AGENT_UPDATE_COMMAND_TTL_SECONDS
    return AGENT_COMMAND_TTL_SECONDS


def cleanup_agent_commands(connection, now):
    candidates = connection.execute(
        "SELECT id, payload_json, created_at, status FROM agent_commands WHERE created_at < ?",
        ((now - timedelta(seconds=AGENT_COMMAND_TTL_SECONDS)).isoformat(),),
    ).fetchall()
    for row in candidates:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        ttl = agent_command_ttl(payload)
        age = now - datetime.fromisoformat(row["created_at"])
        if age > timedelta(seconds=ttl):
            if row["status"] != "expired":
                expired_payload = (
                    json.dumps({"action": "update"})
                    if isinstance(payload, dict) and payload.get("action") == "update"
                    else "{}"
                )
                connection.execute(
                    "UPDATE agent_commands SET status = 'expired', payload_json = ?, "
                    "result_json = NULL, updated_at = ? WHERE id = ?",
                    (expired_payload, now.isoformat(), row["id"]),
                )
            if age > timedelta(seconds=ttl + 60):
                connection.execute("DELETE FROM agent_commands WHERE id = ?", (row["id"],))


def connect_database():
    global _last_command_cleanup
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            status_json TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS service_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            entry TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_commands (
            id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS portal_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS dtmf_buttons (
            position INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            sequence TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS portal_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )"""
    )
    try:
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS portal_users_username_nocase "
            "ON portal_users (username COLLATE NOCASE)"
        )
    except sqlite3.IntegrityError:
        # Äldre databaser kan innehålla namn som bara skiljer sig i skiftläge;
        # create_user kontrollerar dubbletter även utan indexet.
        pass
    connection.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            node_id TEXT,
            node_name TEXT,
            detail TEXT,
            outcome TEXT NOT NULL,
            command_id TEXT
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS audit_log_command ON audit_log (command_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS service_log_node ON service_log (node_id, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS agent_commands_node_status "
        "ON agent_commands (node_id, status, created_at)"
    )
    with _command_cleanup_lock:
        current_time = time.monotonic()
        cleanup_key = str(path)
        last_cleanup = _last_command_cleanup.get(cleanup_key, 0)
        if current_time - last_cleanup >= COMMAND_CLEANUP_INTERVAL_SECONDS:
            now = datetime.now(timezone.utc)
            cleanup_agent_commands(connection, now)
            _last_command_cleanup[cleanup_key] = current_time
    connection.commit()
    return connection


@contextmanager
def database():
    connection = connect_database()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def provision_node(name):
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 100:
        raise ValueError("Nodnamnet måste innehålla 1–100 tecken.")
    try:
        clean_name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Nodnamnet måste innehålla giltig Unicode-text.") from exc
    node_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        connection.execute(
            "INSERT INTO nodes (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (node_id, clean_name, hashlib.sha256(token.encode("utf-8")).hexdigest(), now),
        )
    return {"node_id": node_id, "name": clean_name, "token": token}


NODE_CONFIG_DEFAULTS = {
    "portal_url": "",
    "repo_url": "https://github.com/formidablemagnus/svxlink-node-portal",
    "repo_ref": "main",
    "svxlink_config": "/etc/svxlink/svxlink.conf",
    "svxlink_logfile": "/var/log/svxlink",
    "interval_seconds": 5,
}
PORTAL_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")
GITHUB_REPO_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
GIT_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
ABSOLUTE_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,200}")


def validate_node_config(payload):
    if not isinstance(payload, dict) or set(payload) != set(NODE_CONFIG_DEFAULTS):
        raise ValueError("Nodkonfigurationen har fel fält.")
    for key in NODE_CONFIG_DEFAULTS:
        if key != "interval_seconds" and not isinstance(payload[key], str):
            raise ValueError("Alla fält utom intervallet måste vara text.")
    portal_url = payload["portal_url"].strip().rstrip("/")
    if portal_url and (not PORTAL_URL_RE.fullmatch(portal_url) or "//" in portal_url[8:]):
        raise ValueError(
            "Portaladressen måste vara en HTTP- eller HTTPS-adress utan inloggningsuppgifter, "
            "query eller fragment, till exempel https://portal.example.net."
        )
    repo = GITHUB_REPO_RE.fullmatch(payload["repo_url"].strip())
    if not repo:
        raise ValueError("Repository måste vara en GitHub-adress, till exempel https://github.com/ägare/repo.")
    repo_ref = payload["repo_ref"].strip()
    if (
        not GIT_REF_RE.fullmatch(repo_ref)
        or ".." in repo_ref
        or repo_ref.endswith(("/", ".lock"))
        or "//" in repo_ref
    ):
        raise ValueError("Branch eller tagg innehåller ogiltiga tecken.")
    paths = {}
    for key, label in (("svxlink_config", "Konfigurationsfilen"), ("svxlink_logfile", "Loggfilen")):
        value = payload[key].strip()
        if not ABSOLUTE_PATH_RE.fullmatch(value) or ".." in value.split("/"):
            raise ValueError(f"{label} måste vara en absolut sökväg utan särtecken.")
        paths[key] = value
    interval = payload["interval_seconds"]
    if type(interval) is not int or not 1 <= interval <= 3600:
        raise ValueError("Rapporteringsintervallet måste vara 1–3600 sekunder.")
    return {
        "portal_url": portal_url,
        "repo_url": f"https://github.com/{repo.group(1)}/{repo.group(2)}",
        "repo_ref": repo_ref,
        "svxlink_config": paths["svxlink_config"],
        "svxlink_logfile": paths["svxlink_logfile"],
        "interval_seconds": interval,
    }


def get_node_config(connection=None):
    def read(conn):
        return {row["key"]: row["value"] for row in conn.execute(
            "SELECT key, value FROM portal_settings WHERE key LIKE 'node_config.%'"
        ).fetchall()}

    if connection is None:
        with database() as conn:
            stored = read(conn)
    else:
        stored = read(connection)
    config = dict(NODE_CONFIG_DEFAULTS)
    for key in config:
        value = stored.get(f"node_config.{key}")
        if value is not None:
            config[key] = int(value) if key == "interval_seconds" else value
    return config


def save_node_config(actor, payload):
    config = validate_node_config(payload)
    with database() as connection:
        for key, value in config.items():
            connection.execute(
                "INSERT INTO portal_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"node_config.{key}", str(value)),
            )
        record_audit(connection, actor, "node-config.update", config["repo_url"])
    return config


def build_install_command(config, node_id, token):
    """Return a one-line agent installer command, or None while no portal URL is set."""
    if not config["portal_url"]:
        return None
    repo = config["repo_url"]
    ref = config["repo_ref"]
    raw_url = (
        repo.replace("https://github.com/", "https://raw.githubusercontent.com/", 1)
        + f"/{ref}/install.sh"
    )
    environment = [
        ("AGENT_PORTAL_URL", config["portal_url"]),
        ("AGENT_NODE_ID", node_id),
        ("AGENT_TOKEN", token),
        ("AGENT_INTERVAL_SECONDS", str(config["interval_seconds"])),
        ("SVXLINK_CONFIG", config["svxlink_config"]),
        ("SVXLINK_LOGFILE", config["svxlink_logfile"]),
        ("SVX_INSTALL_REPO", repo + ".git"),
        ("SVX_INSTALL_REF", ref),
    ]
    assignments = " ".join(f"{name}={shlex.quote(value)}" for name, value in environment)
    return f"curl -fsSL {shlex.quote(raw_url)} | sudo env {assignments} bash -s -- agent"


def validate_status(payload):
    if not isinstance(payload, dict):
        raise ValueError("Statusrapporten måste vara ett JSON-objekt.")
    active = payload.get("active")
    if type(active) is not bool:
        raise ValueError("'active' måste vara boolean.")
    service_status = payload.get("service_status")
    if not isinstance(service_status, str) or len(service_status) > 64:
        raise ValueError("'service_status' måste vara text på högst 64 tecken.")
    current_tg = payload.get("current_tg")
    if current_tg is not None and (
        not isinstance(current_tg, (str, int)) or len(str(current_tg)) > 32
    ):
        raise ValueError("'current_tg' måste vara text, ett heltal eller null.")
    logic = payload.get("logic", "")
    callsign = payload.get("callsign", "")
    node_version = optional_text(payload, "node_version", 64) or ""
    svxlink_version = optional_text(payload, "svxlink_version", 64) or ""
    if not isinstance(logic, str) or len(logic) > 128:
        raise ValueError("'logic' måste vara text på högst 128 tecken.")
    if not isinstance(callsign, str) or len(callsign) > 64:
        raise ValueError("'callsign' måste vara text på högst 64 tecken.")
    radio = validate_radio(payload.get("radio", {}))
    node_info = payload.get("node_info")
    if node_info is not None and (
        not isinstance(node_info, dict)
        or len(json.dumps(node_info, ensure_ascii=False).encode("utf-8")) > MAX_NODE_INFO_BYTES
    ):
        raise ValueError("'node_info' måste vara ett JSON-objekt på högst 64 kB.")
    logs = payload.get("logs", [])
    if not isinstance(logs, list) or len(logs) > MAX_LOG_LINES:
        raise ValueError(f"'logs' måste vara en lista med högst {MAX_LOG_LINES} loggrader.")
    for line in logs:
        if not isinstance(line, str):
            raise ValueError("Varje loggrad måste vara text.")
        try:
            line_size = len(line.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("Loggraderna måste innehålla giltig Unicode-text.") from exc
        if line_size > MAX_LOG_LINE_BYTES:
            raise ValueError(f"Varje loggrad måste vara text på högst {MAX_LOG_LINE_BYTES} byte.")
    return {
        "active": active,
        "service_status": service_status,
        "current_tg": str(current_tg) if current_tg is not None else None,
        "logic": logic,
        "callsign": callsign,
        "node_version": node_version,
        "svxlink_version": svxlink_version,
        "radio": radio,
        "logs": logs,
        "node_info": node_info,
        **validate_status_details(payload),
    }


def optional_text(payload, key, limit):
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"'{key}' måste vara text på högst {limit} tecken.")
    return value


def validate_udp_audio(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("'udp_audio' måste vara ett JSON-objekt.")
    flags = {}
    for key in ("listen", "talk", "ptt"):
        flag = value.get(key, False)
        if type(flag) is not bool:
            raise ValueError(f"'udp_audio.{key}' måste vara boolean.")
        flags[key] = flag
    issues = value.get("issues", [])
    if (
        not isinstance(issues, list)
        or len(issues) > 5
        or any(not isinstance(issue, str) or len(issue) > 256 for issue in issues)
    ):
        raise ValueError("'udp_audio.issues' måste vara en lista med högst 5 texter.")
    mic_frames = value.get("mic_frames", 0)
    if type(mic_frames) is not int or not 0 <= mic_frames <= 10**12:
        raise ValueError("'udp_audio.mic_frames' måste vara ett heltal.")
    return {**flags, "issues": issues, "mic_frames": mic_frames,
            "error": optional_text(value, "error", 256) or ""}


def validate_status_details(payload):
    monitor_tgs = payload.get("monitor_tgs", [])
    if (
        not isinstance(monitor_tgs, list)
        or len(monitor_tgs) > 64
        or any(not isinstance(tg, str) or len(tg) > 16 for tg in monitor_tgs)
    ):
        raise ValueError("'monitor_tgs' måste vara en lista med talgrupper.")
    pty_checks = payload.get("pty_checks", [])
    if not isinstance(pty_checks, list) or len(pty_checks) > 10:
        raise ValueError("'pty_checks' måste vara en lista med högst 10 poster.")
    clean_checks = []
    for check in pty_checks:
        if not isinstance(check, dict):
            raise ValueError("Varje PTY-kontroll måste vara ett JSON-objekt.")
        clean_checks.append({
            key: optional_text(check, key, 256) for key in ("variable", "logic", "path", "reason")
        })
    counters = {}
    for key in ("errors", "warnings", "events"):
        value = payload.get(key, 0)
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise ValueError(f"'{key}' måste vara ett heltal.")
        counters[key] = value
    return {
        "tg_event": optional_text(payload, "tg_event", 128) or "",
        "tg_time": optional_text(payload, "tg_time", 32) or "",
        "monitor_tgs": monitor_tgs,
        "monitor_tgs_error": optional_text(payload, "monitor_tgs_error", 256),
        "pty_checks": clean_checks,
        "udp_audio": validate_udp_audio(payload.get("udp_audio")),
        **counters,
    }


def validate_radio(radio):
    if not isinstance(radio, dict):
        raise ValueError("'radio' måste vara ett JSON-objekt.")
    mode = radio.get("mode", "unknown")
    if not isinstance(mode, str) or mode not in VALID_RADIO_MODES:
        raise ValueError("'radio.mode' har ett ogiltigt värde.")
    signal_level = radio.get("signal_level")
    if signal_level is not None and (
        type(signal_level) is not int or not 0 <= signal_level <= 100
    ):
        raise ValueError("'radio.signal_level' måste vara ett heltal mellan 0 och 100.")
    receivers = radio.get("receivers", [])
    transmitters = radio.get("transmitters", [])
    if (
        not isinstance(receivers, list) or len(receivers) > 32
        or not isinstance(transmitters, list) or len(transmitters) > 32
    ):
        raise ValueError("'radio' har för många mottagare eller sändare.")
    clean_receivers = []
    for receiver in receivers:
        if (
            not isinstance(receiver, dict)
            or not isinstance(receiver.get("name"), str)
            or len(receiver["name"]) > 64
            or type(receiver.get("receiving")) is not bool
            or type(receiver.get("siglev")) is not int
            or not 0 <= receiver["siglev"] <= 100
        ):
            raise ValueError("Ogiltig mottagarstatus.")
        clean_receivers.append({
            "name": receiver["name"],
            "receiving": receiver["receiving"],
            "siglev": receiver["siglev"],
        })
    clean_transmitters = []
    for transmitter in transmitters:
        if (
            not isinstance(transmitter, dict)
            or not isinstance(transmitter.get("name"), str)
            or len(transmitter["name"]) > 64
            or type(transmitter.get("transmit")) is not bool
        ):
            raise ValueError("Ogiltig sändarstatus.")
        clean_transmitters.append({
            "name": transmitter["name"],
            "transmit": transmitter["transmit"],
        })
    return {
        "mode": mode,
        "signal_level": signal_level,
        "receivers": clean_receivers,
        "transmitters": clean_transmitters,
        "error": optional_text(radio, "error", 256),
    }


def validate_radio_events(payload):
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_RADIO_EVENTS_PER_REQUEST:
        raise ValueError("'events' måste vara en lista med 1–100 radiohändelser.")
    clean = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Varje radiohändelse måste vara ett JSON-objekt.")
        timestamp = event.get("t")
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Radiohändelsen saknar giltig tidsstämpel.")
        clean.append({"t": timestamp, **validate_radio(event)})
    return clean


def node_rows():
    with database() as connection:
        rows = connection.execute(
            "SELECT id, name, status_json, last_seen FROM nodes ORDER BY name COLLATE NOCASE"
        ).fetchall()
        updates = active_update_statuses(connection)
    return [node_record(row, updates.get(row["id"])) for row in rows]


def node_by_id(node_id):
    with database() as connection:
        row = connection.execute(
            "SELECT id, name, status_json, last_seen FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        update = active_update_statuses(connection, {node_id}).get(node_id)
    return node_record(row, update) if row else None


def expire_stalled_updates(connection, now):
    """Expire updates whose agent stopped reporting (heartbeat every 20 s)."""
    rows = connection.execute(
        "SELECT id, payload_json, status, created_at, updated_at FROM agent_commands "
        "WHERE status IN ('pending', 'in_progress')"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            limit = UPDATE_HEARTBEAT_TIMEOUT_SECONDS if row["status"] == "in_progress" \
                else UPDATE_PENDING_TIMEOUT_SECONDS
            stamp = row["updated_at"] if row["status"] == "in_progress" else row["created_at"]
            stale = now - datetime.fromisoformat(stamp) > timedelta(seconds=limit)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("action") == "update" and stale:
            connection.execute(
                "UPDATE agent_commands SET status = 'expired', result_json = NULL, "
                "updated_at = ? WHERE id = ? AND status = ?",
                (now.isoformat(), row["id"], row["status"]),
            )


def active_update_statuses(connection, node_ids=None):
    expire_stalled_updates(connection, datetime.now(timezone.utc))
    rows = connection.execute(
        "SELECT node_id, payload_json, status, result_json FROM agent_commands "
        "WHERE status IN ('pending', 'in_progress') ORDER BY created_at DESC"
    ).fetchall()
    updates = {}
    for row in rows:
        if node_ids is not None and row["node_id"] not in node_ids:
            continue
        if row["node_id"] in updates:
            continue
        try:
            payload = json.loads(row["payload_json"])
            progress = json.loads(row["result_json"]) if row["result_json"] else None
        except (TypeError, json.JSONDecodeError):
            logging.error("Ogiltig uppdateringsstatus för nod %s.", row["node_id"])
            continue
        if isinstance(payload, dict) and payload.get("action") == "update":
            updates[row["node_id"]] = {
                "status": row["status"],
                "progress": progress if isinstance(progress, dict) else None,
            }
    return updates


def node_record(row, update=None):
    now = datetime.now(timezone.utc)
    last_seen = row["last_seen"]
    online = False
    if last_seen:
        try:
            seen_at = datetime.fromisoformat(last_seen)
            online = (now - seen_at).total_seconds() <= ONLINE_WINDOW_SECONDS
        except ValueError:
            logging.error("Ogiltig last_seen-tid för nod %s.", row["id"])
    status = json.loads(row["status_json"]) if row["status_json"] else None
    if status is not None:
        merge_live_data(row["id"], status)
    return {
        "id": row["id"],
        "name": row["name"],
        "online": online,
        "last_seen": last_seen,
        "status": status,
        "update": update,
    }


def merge_live_data(node_id, status):
    """Overlay streamed radio and log data on the last full status snapshot."""
    logs = LIVE_LOGS.get(node_id)
    if logs:
        status["logs"] = logs
    with RADIO_HUB.condition:
        node = RADIO_HUB.nodes.get(node_id)
        latest = node["latest"] if node else None
    if latest:
        status["radio"] = {key: value for key, value in latest.items() if key != "t"}


def validate_config_command(payload):
    if not isinstance(payload, dict):
        raise ValueError("Begäran måste vara ett JSON-objekt.")
    action = payload.get("action")
    document = payload.get("document")
    if not isinstance(document, str) or document not in CONFIG_DOCUMENTS:
        raise ValueError("Endast svxlink.conf och node_info.json kan ändras.")
    if action == "read" and set(payload) == {"action", "document"}:
        return {"action": action, "document": document}
    if action != "write" or set(payload) != {
        "action", "document", "content", "sha256"
    }:
        raise ValueError("Ogiltig konfigurationsåtgärd.")
    content = payload["content"]
    checksum = payload["sha256"]
    if not isinstance(content, str):
        raise ValueError("Konfigurationen måste vara text.")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Konfigurationen måste innehålla giltig Unicode-text.") from exc
    if not encoded or len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError("Konfigurationen måste vara mellan 1 byte och 2 MB.")
    if b"\x00" in encoded:
        raise ValueError("Konfigurationen får inte innehålla NUL-tecken.")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Ogiltig kontrollsumma.")
    if document == "node_info.json":
        try:
            json.loads(content, parse_constant=reject_json_constant)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Ogiltig node_info.json på rad {exc.lineno}, "
                f"kolumn {exc.colno}: {exc.msg}"
            ) from exc
    return payload


def validate_dtmf_command(payload):
    if not isinstance(payload, dict) or set(payload) != {"action", "sequence"}:
        raise ValueError("DTMF-begäran har ogiltigt format.")
    sequence = payload["sequence"]
    if (
        not isinstance(sequence, str)
        or not 1 <= len(sequence) <= MAX_DTMF_SEQUENCE_LENGTH
        or not re.fullmatch(r"[0-9A-D*#]+", sequence)
    ):
        raise ValueError(
            f"DTMF-sekvensen måste innehålla 1–{MAX_DTMF_SEQUENCE_LENGTH} tecken "
            "från 0–9, A–D, * och #."
        )
    return {"action": "dtmf", "sequence": sequence}


def validate_service_command(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"action", "operation"}
        or payload["action"] != "service"
        or payload["operation"] not in         ("start", "stop", "restart")
    ):
        raise ValueError("Ogiltig SvxLink-tjänståtgärd.")
    return {"action": "service", "operation": payload["operation"]}


def is_update_channel(value):
    return isinstance(value, str) and (
        value in ("prerelease", "release") or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value) is not None
    )


def validate_update_command(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"action", "channel"}
        or payload["action"] != "update"
        or not is_update_channel(payload["channel"])
    ):
        raise ValueError("Ogiltig SvxLink-uppdatering.")
    return {"action": "update", "channel": payload["channel"]}


def validate_dtmf_buttons(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"buttons"}
        or not isinstance(payload["buttons"], list)
        or len(payload["buttons"]) > MAX_DTMF_BUTTONS
    ):
        raise ValueError(f"Ange högst {MAX_DTMF_BUTTONS} knappar.")
    buttons = []
    for item in payload["buttons"]:
        if not isinstance(item, dict) or set(item) != {"label", "sequence"}:
            raise ValueError("Ogiltig knappdefinition.")
        label = item["label"]
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40:
            raise ValueError("Knappnamn måste vara 1–40 tecken.")
        command = validate_dtmf_command({"action": "dtmf", "sequence": item["sequence"]})
        buttons.append({"label": label.strip(), "sequence": command["sequence"]})
    return buttons


def list_dtmf_buttons():
    with database() as connection:
        rows = connection.execute(
            "SELECT label, sequence FROM dtmf_buttons ORDER BY position"
        ).fetchall()
    return [{"label": row["label"], "sequence": row["sequence"]} for row in rows]


def replace_dtmf_buttons(buttons):
    with database() as connection:
        connection.execute("DELETE FROM dtmf_buttons")
        connection.executemany(
            "INSERT INTO dtmf_buttons (position, label, sequence) VALUES (?, ?, ?)",
            [(index, b["label"], b["sequence"]) for index, b in enumerate(buttons)],
        )


def validate_schedules_command(payload):
    if not isinstance(payload, dict) or payload.get("action") != "schedules":
        raise ValueError("Ogiltig schemaläggningsåtgärd.")
    if payload.get("operation") == "read" and set(payload) == {"action", "operation"}:
        return {"action": "schedules", "operation": "read"}
    if payload.get("operation") != "write" or set(payload) != {
        "action", "operation", "schedules", "sha256"
    }:
        raise ValueError("Ogiltig schemaläggningsåtgärd.")
    schedules = payload["schedules"]
    if not isinstance(schedules, list) or len(schedules) > MAX_SCHEDULES:
        raise ValueError(f"Ange högst {MAX_SCHEDULES} schemaläggningar.")
    for item in schedules:
        if not isinstance(item, dict) or not {"dtmf", "minute", "hour", "day", "month", "weekday"} <= set(item):
            raise ValueError("Ogiltig schemaläggning.")
    if not isinstance(payload["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]):
        raise ValueError("Ogiltig kontrollsumma.")
    return payload


def validate_agent_command(payload):
    if isinstance(payload, dict) and payload.get("action") == "dtmf":
        return validate_dtmf_command(payload)
    if isinstance(payload, dict) and payload.get("action") == "service":
        return validate_service_command(payload)
    if isinstance(payload, dict) and payload.get("action") == "update":
        return validate_update_command(payload)
    if isinstance(payload, dict) and payload.get("action") == "schedules":
        return validate_schedules_command(payload)
    return validate_config_command(payload)


def reject_json_constant(value):
    raise ValueError(f"Ogiltigt JSON-konstantvärde: {value}.")


def command_audit_summary(command):
    action = command["action"]
    if action == "dtmf":
        return "dtmf", command["sequence"]
    if action == "service":
        return "service", command["operation"]
    if action == "update":
        return "update", command["channel"]
    if action == "schedules":
        return "schedules", command["operation"]
    return f"config.{action}", command["document"]


def record_audit(connection, actor, action, detail="", node_id=None, node_name=None,
                 outcome="ok", command_id=None):
    connection.execute(
        "INSERT INTO audit_log (created_at, actor, action, node_id, node_name, detail, "
        "outcome, command_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), actor, action, node_id, node_name,
         detail[:200], outcome, command_id),
    )
    connection.execute(
        "DELETE FROM audit_log WHERE id <= "
        "(SELECT MAX(id) FROM audit_log) - ?",
        (MAX_AUDIT_ROWS,),
    )


def list_audit(limit=AUDIT_PAGE_SIZE, node_id=None):
    query = (
        "SELECT created_at, actor, action, node_id, node_name, detail, outcome "
        "FROM audit_log"
    )
    params = []
    if node_id:
        query += " WHERE node_id = ?"
        params.append(node_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with database() as connection:
        rows = connection.execute(query, params).fetchall()
    entries = []
    now = datetime.now(timezone.utc)
    for row in rows:
        entry = dict(row)
        if entry["outcome"] == "queued":
            ttl = (
                AGENT_UPDATE_COMMAND_TTL_SECONDS
                if entry["action"] == "update"
                else AGENT_COMMAND_TTL_SECONDS
            )
            if now - datetime.fromisoformat(entry["created_at"]) > timedelta(seconds=ttl + 60):
                entry["outcome"] = "expired"
        entries.append(entry)
    return entries


BACKUP_FORMAT = "svxlink-node-portal-backup"
MAX_BACKUP_BYTES = 50 * 1024 * 1024
BACKUP_TABLES = {
    "nodes": (("id", True), ("name", True), ("token_hash", True), ("status_json", False),
              ("last_seen", False), ("created_at", True)),
    "portal_users": (("username", True), ("password_hash", True), ("role", True),
                     ("created_at", True)),
    "dtmf_buttons": (("position", True), ("label", True), ("sequence", True)),
    "audit_log": (("id", True), ("created_at", True), ("actor", True), ("action", True),
                  ("node_id", False), ("node_name", False), ("detail", False),
                  ("outcome", True), ("command_id", False)),
    "service_log": (("id", True), ("node_id", True), ("created_at", True),
                    ("actor", True), ("entry", True)),
}


def create_backup(actor):
    with database() as connection:
        tables = {}
        for table, columns in BACKUP_TABLES.items():
            names = ", ".join(name for name, _ in columns)
            rows = connection.execute(f"SELECT {names} FROM {table}").fetchall()
            tables[table] = [dict(row) for row in rows]
        record_audit(connection, actor, "backup.create", "Säkerhetskopia skapad")
    return {
        "format": BACKUP_FORMAT,
        "version": 1,
        "portal_version": PORTAL_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


def validate_backup(backup):
    if (
        not isinstance(backup, dict)
        or backup.get("format") != BACKUP_FORMAT
        or backup.get("version") != 1
        or not isinstance(backup.get("tables"), dict)
    ):
        raise ValueError("Filen är ingen giltig säkerhetskopia från portalen.")
    tables = backup["tables"]
    clean = {}
    for table, columns in BACKUP_TABLES.items():
        rows = tables.get(table)
        if table == "service_log" and table not in tables:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"Säkerhetskopian saknar tabellen {table}.")
        clean_rows = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {name for name, _ in columns}:
                raise ValueError(f"Ogiltig rad i tabellen {table}.")
            for name, required in columns:
                value = row[name]
                numeric = name in ("id", "position") and table in (
                    "audit_log", "dtmf_buttons", "service_log"
                )
                if numeric:
                    valid = isinstance(value, int) and not isinstance(value, bool)
                else:
                    valid = isinstance(value, str) or (value is None and not required)
                if not valid:
                    raise ValueError(f"Ogiltigt värde för {name} i tabellen {table}.")
            clean_rows.append(row)
        clean[table] = clean_rows
    node_ids = {row["id"] for row in clean["nodes"]}
    service_log_ids = set()
    for row in clean["service_log"]:
        if row["node_id"] not in node_ids:
            raise ValueError("Säkerhetskopian innehåller en serviceloggrad för en okänd nod.")
        if row["id"] in service_log_ids or len(row["entry"]) > 2000:
            raise ValueError("Säkerhetskopian innehåller en ogiltig serviceloggrad.")
        service_log_ids.add(row["id"])
    for user in clean["portal_users"]:
        if user["role"] not in USER_ROLES or not USERNAME_PATTERN.fullmatch(user["username"]):
            raise ValueError("Säkerhetskopian innehåller en ogiltig användare.")
    if len(clean["dtmf_buttons"]) > MAX_DTMF_BUTTONS:
        raise ValueError("Säkerhetskopian innehåller för många DTMF-knappar.")
    for table, key in (("nodes", "id"), ("portal_users", "username")):
        keys = {
            row[key].lower() if table == "portal_users" else row[key]
            for row in clean[table]
        }
        if len(keys) != len(clean[table]):
            raise ValueError(f"Säkerhetskopian innehåller dubbletter i {table}.")
    if len({row["token_hash"] for row in clean["nodes"]}) != len(clean["nodes"]):
        raise ValueError("Säkerhetskopian innehåller dubbletter i nodernas token.")
    return clean


def restore_backup(actor, backup):
    tables = validate_backup(backup)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM agent_commands")
        for table, columns in BACKUP_TABLES.items():
            connection.execute(f"DELETE FROM {table}")
            names = [name for name, _ in columns]
            connection.executemany(
                f"INSERT INTO {table} ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [tuple(row[name] for name in names) for row in tables[table]],
            )
        record_audit(
            connection, actor, "backup.restore",
            f"Återställde {len(tables['nodes'])} noder och {len(tables['portal_users'])} användare",
        )
    return {table: len(rows) for table, rows in tables.items()}


def hash_password(password):
    salt =  secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(candidate.hex(), digest)
    except ValueError:
        return False


def validate_user_fields(username, password, role):
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Användarnamnet måste vara 1–40 tecken: A–Z, 0–9, punkt, understreck eller bindestreck.")
    if not isinstance(password, str) or not (
        MIN_USER_PASSWORD_LENGTH <= len(password) <= 200
        and re.search(r"[a-z]", password)
        and re.search(r"[A-Z]", password)
        and re.search(r"[0-9]", password)
    ):
        raise ValueError(
            f"Lösenordet måste innehålla {MIN_USER_PASSWORD_LENGTH}–200 tecken "
            "med minst en stor bokstav, en liten bokstav och en siffra."
        )
    if role not in USER_ROLES:
        raise ValueError("Rollen måste vara admin eller operator.")


def list_users():
    users = [{"username": admin_credentials()[0], "role": "admin", "builtin": True}]
    with database() as connection:
        rows = connection.execute(
            "SELECT username, role, created_at FROM portal_users ORDER BY username"
        ).fetchall()
    users.extend({**dict(row), "builtin": False} for row in rows)
    return users


def create_user(actor, username, password, role):
    validate_user_fields(username, password, role)
    if username.lower() == admin_credentials()[0].lower():
        raise ValueError("Användarnamnet är reserverat för huvudadministratören.")
    with database() as connection:
        if connection.execute(
            "SELECT 1 FROM portal_users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone():
            raise ValueError("Användaren finns redan.")
        try:
            connection.execute(
                "INSERT INTO portal_users (username, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role,
                 datetime.now(timezone.utc).isoformat()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("Användaren finns redan.") from None
        record_audit(connection, actor, "user.create", f"{username} ({role})")


def update_user(actor, username, password=None, role=None):
    with database() as connection:
        row = connection.execute(
            "SELECT username, role FROM portal_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if row is None:
            return False
        username = row["username"]
        new_role = role if role is not None else row["role"]
        if password is None:
            password_hash = None
            validate_user_fields(username, "Aa1" + "x" * MIN_USER_PASSWORD_LENGTH, new_role)
        else:
            validate_user_fields(username, password, new_role)
            password_hash = hash_password(password)
        if password_hash:
            connection.execute(
                "UPDATE portal_users SET password_hash = ?, role = ? WHERE username = ?",
                (password_hash, new_role, username),
            )
            connection.execute(
                "DELETE FROM sessions WHERE username = ? COLLATE NOCASE", (username,)
            )
        else:
            connection.execute(
                "UPDATE portal_users SET role = ? WHERE username = ?", (new_role, username)
            )
        record_audit(connection, actor, "user.update", f"{username} ({new_role})")
    return True


def delete_user(actor, username):
    with database() as connection:
        row = connection.execute(
            "SELECT username FROM portal_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if row is None:
            return False
        username = row["username"]
        cursor = connection.execute(
            "DELETE FROM portal_users WHERE username = ?", (username,)
        )
        if cursor.rowcount != 1:
            return False
        connection.execute(
            "DELETE FROM sessions WHERE username = ? COLLATE NOCASE", (username,)
        )
        record_audit(connection, actor, "user.delete", username)
    return True


def delete_node(actor, node_id):
    with database() as connection:
        node = connection.execute(
            "SELECT name FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if node is None:
            return False
        cursor = connection.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        if cursor.rowcount != 1:
            return False
        connection.execute("DELETE FROM agent_commands WHERE node_id = ?", (node_id,))
        connection.execute("DELETE FROM service_log WHERE node_id = ?", (node_id,))
        record_audit(
            connection, actor, "node.delete", node["name"], node_id, node["name"]
        )
    return True


def _same_text(first, second):
    return hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def authenticate(username, password):
    """Return (canonical username, role) for valid credentials, otherwise None.

    Usernames are matched case-insensitively."""
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    expected_user, expected_password = admin_credentials()
    user_ok = _same_text(username.lower(), expected_user.lower())
    password_ok = _same_text(password, expected_password)
    if user_ok and password_ok:
        return expected_user, "admin"
    with database() as connection:
        row = connection.execute(
            "SELECT username, password_hash, role FROM portal_users "
            "WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    if row is not None and verify_password(password, row["password_hash"]):
        return row["username"], row["role"]
    return None


def authenticate_user(username, password):
    """Return the role for valid credentials, otherwise None."""
    result = authenticate(username, password)
    return result[1] if result else None


def resolve_user(username):
    """Return (canonical username, role) for an existing account."""
    expected_user = admin_credentials()[0]
    if username.lower() == expected_user.lower():
        return expected_user, "admin"
    with database() as connection:
        row = connection.execute(
            "SELECT username, role FROM portal_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    return (row["username"], row["role"]) if row else None


def create_session(username):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with database() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),)
        )
        connection.execute(
            "INSERT INTO sessions (token_hash, username, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                username,
                now.isoformat(),
                (now + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(),
            ),
        )
    return token


def session_user(token):
    """Return (username, role) for a valid session token and extend it."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    with database() as connection:
        row = connection.execute(
            "SELECT username, expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    expires_at = datetime.fromisoformat(row["expires_at"])
    user = resolve_user(row["username"]) if expires_at > now else None
    with database() as connection:
        if user is None:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        elif (expires_at - now).total_seconds() < SESSION_TTL_SECONDS - SESSION_REFRESH_SECONDS:
            connection.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                ((now + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(), token_hash),
            )
    return user


def destroy_session(token):
    if token:
        with database() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
            )


def login_throttled(key):
    now = time.monotonic()
    with _login_lock:
        attempts = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        _login_failures[key] = attempts
        return len(attempts) >= LOGIN_MAX_FAILURES


def record_login_failure(key):
    with _login_lock:
        if len(_login_failures) > 10000:
            _login_failures.clear()
        _login_failures.setdefault(key, []).append(time.monotonic())


def clear_login_failures(key):
    with _login_lock:
        _login_failures.pop(key, None)


def enqueue_agent_command(node_id, payload, actor="system"):
    command = validate_agent_command(payload)
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cleanup_agent_commands(connection, now)
        expire_stalled_updates(connection, now)
        node = connection.execute(
            "SELECT id, last_seen, status_json FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if node is None:
            return None
        if not node["last_seen"]:
            raise RuntimeError("Noden har ännu inte skickat någon statusrapport.")
        try:
            last_seen = datetime.fromisoformat(node["last_seen"])
        except ValueError:
            logging.error("Ogiltig last_seen-tid för nod %s.", node_id)
            raise RuntimeError("Nodens senaste kontakttid är ogiltig.") from None
        if (now - last_seen).total_seconds() > ONLINE_WINDOW_SECONDS:
            raise RuntimeError("Noden är offline. Anslut den innan du skickar kommandon.")
        active_commands = connection.execute(
            "SELECT payload_json FROM agent_commands "
            "WHERE node_id = ? AND status IN ('pending', 'in_progress')",
            (node_id,),
        ).fetchall()
        active_actions = []
        for row in active_commands:
            try:
                active_payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                logging.error("Ogiltigt kommando i kön för nod %s.", node_id)
                raise RuntimeError("Nodens åtgärdskö innehåller ett ogiltigt kommando.") from None
            active_actions.append(
                active_payload.get("action") if isinstance(active_payload, dict) else None
            )
        if "update" in active_actions:
            raise RuntimeError("Noden uppdateras. Vänta tills uppdateringen är klar.")
        if command.get("action") == "update" and active_actions:
            raise RuntimeError("Noden har andra väntande åtgärder. Vänta tills de är klara.")
        if command.get("action") == "dtmf":
            status = json.loads(node["status_json"]) if node["status_json"] else None
            if not isinstance(status, dict) or status.get("active") is not True:
                raise RuntimeError(
                    "SvxLink-tjänsten är inte igång. Starta tjänsten innan du skickar DTMF."
                )
        count = connection.execute(
            "SELECT COUNT(*) FROM agent_commands "
            "WHERE node_id = ? AND status IN ('pending', 'in_progress')",
            (node_id,),
        ).fetchone()[0]
        if count >= 5:
            raise ValueError("För många väntande åtgärder för noden.")
        command_id = uuid.uuid4().hex
        connection.execute(
            """INSERT INTO agent_commands
                (id, node_id, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)""",
            (command_id, node_id, json.dumps(command, ensure_ascii=False), created_at, created_at),
        )
        node_name = connection.execute(
            "SELECT name FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()["name"]
        audit_action, audit_detail = command_audit_summary(command)
        record_audit(connection, actor, audit_action, audit_detail, node_id, node_name,
                     "queued", command_id)
    return command_id


def claim_agent_commands(node_id):
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    retry_before = (now - timedelta(seconds=45)).isoformat()
    with database() as connection:
        cleanup_agent_commands(connection, now)
        expire_stalled_updates(connection, now)
        stale_commands = connection.execute(
            "SELECT id, payload_json FROM agent_commands "
            "WHERE node_id = ? AND status = 'in_progress' AND updated_at < ?",
            (node_id, retry_before),
        ).fetchall()
        for row in stale_commands:
            payload = json.loads(row["payload_json"])
            if payload.get("action") == "update":
                continue
            status = "expired" if payload.get("action") == "dtmf" else "pending"
            connection.execute(
                "UPDATE agent_commands SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'in_progress'",
                (status, now_text, row["id"]),
            )
        active_updates = connection.execute(
            "SELECT payload_json FROM agent_commands "
            "WHERE node_id = ? AND status = 'in_progress'",
            (node_id,),
        ).fetchall()
        if any(
            json.loads(row["payload_json"]).get("action") == "update"
            for row in active_updates
        ):
            return []
        rows = connection.execute(
            "SELECT id, payload_json FROM agent_commands "
            "WHERE node_id = ? AND status = 'pending' ORDER BY created_at LIMIT 1",
            (node_id,),
        ).fetchall()
        commands = []
        for row in rows:
            connection.execute(
                "UPDATE agent_commands SET status = 'in_progress', updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now_text, row["id"]),
            )
            commands.append({"id": row["id"], **json.loads(row["payload_json"])})
    return commands


def complete_agent_command(node_id, command_id, result):
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("Resultatet måste innehålla en boolean 'ok'.")
    with database() as connection:
        row = connection.execute(
            "SELECT payload_json FROM agent_commands "
            "WHERE id = ? AND node_id = ? AND status = 'in_progress'",
            (command_id, node_id),
        ).fetchone()
    if row is None:
        return False
    command = json.loads(row["payload_json"])
    if not result["ok"]:
        if set(result) != {"ok", "error"} or not isinstance(result["error"], str):
            raise ValueError("Felresultatet har ogiltigt format.")
        if len(result["error"]) > 1024:
            result = {"ok": False, "error": result["error"][:1024]}
    elif command["action"] == "read":
        if set(result) != {"ok", "document", "content", "sha256"}:
            raise ValueError("Läsresultatet har ogiltigt format.")
        if result["document"] != command["document"] or not isinstance(
            result["content"], str
        ):
            raise ValueError("Läsresultatet innehåller fel dokument.")
        try:
            content_bytes = result["content"].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Läsresultatet måste innehålla giltig Unicode-text.") from exc
        if len(content_bytes) > MAX_CONFIG_BYTES:
            raise ValueError("Konfigurationsfilen är större än 2 MB.")
        if not isinstance(result["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", result["sha256"]
        ) or not hmac.compare_digest(
            hashlib.sha256(content_bytes).hexdigest(), result["sha256"]
        ):
            raise ValueError("Läsresultatets kontrollsumma stämmer inte.")
    elif command["action"] == "write":
        if set(result) != {"ok", "document", "backup", "sha256"}:
            raise ValueError("Skrivresultatet har ogiltigt format.")
        if (
            result["document"] != command["document"]
            or not isinstance(result["backup"], str)
            or not isinstance(result["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["sha256"])
        ):
            raise ValueError("Skrivresultatet innehåller ogiltiga uppgifter.")
    elif command["action"] == "dtmf":
        if set(result) != {"ok"}:
            raise ValueError("DTMF-resultatet har ogiltigt format.")
    elif command["action"] == "service":
        if set(result) != {"ok"}:
            raise ValueError("Tjänstresultatet har ogiltigt format.")
    elif command["action"] == "update":
        if (
            set(result) != {"ok", "channel", "version"}
            or result["channel"] != command["channel"]
            or not isinstance(result["version"], str)
            or not re.fullmatch(r"[A-Za-z0-9.+:~_-]{1,128}", result["version"])
        ):
            raise ValueError("Uppdateringsresultatet har ogiltigt format.")
    elif command["action"] == "schedules":
        if set(result) != {"ok", "schedules", "other_lines", "cron_target", "sha256"} or not (
            isinstance(result["schedules"], list)
            and isinstance(result["other_lines"], list)
            and all(isinstance(line, str) for line in result["other_lines"])
            and isinstance(result["cron_target"], str)
            and isinstance(result["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", result["sha256"])
        ):
            raise ValueError("Schemaresultatet har ogiltigt format.")
    else:
        raise ValueError("Okänd konfigurationsåtgärd.")
    try:
        result_json = json.dumps(result, ensure_ascii=False)
        encoded_result = result_json.encode("utf-8")
    except (TypeError, UnicodeError) as exc:
        raise ValueError("Resultatet innehåller ogiltig text.") from exc
    if len(encoded_result) > MAX_COMMAND_RESULT_BYTES:
        raise ValueError("Resultatet är större än tillåtet.")
    with database() as connection:
        outcome = "completed" if result["ok"] else "failed"
        cursor = connection.execute(
            """UPDATE agent_commands SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ? AND node_id = ? AND status = 'in_progress'""",
            (
                outcome,
                result_json,
                datetime.now(timezone.utc).isoformat(),
                command_id,
                node_id,
            ),
        )
        if cursor.rowcount == 1:
            connection.execute(
                "UPDATE audit_log SET outcome = ? WHERE command_id = ?",
                (outcome, command_id),
            )
    return cursor.rowcount == 1


def report_agent_command_progress(node_id, command_id, progress):
    if (
        not isinstance(progress, dict)
        or set(progress) != {"stage"}
        or not isinstance(progress["stage"], str)
        or progress["stage"] not in UPDATE_PROGRESS_MESSAGES
    ):
        raise ValueError("Uppdateringsstatusen har ogiltigt format.")
    now = datetime.now(timezone.utc).isoformat()
    progress_json = json.dumps({
        "stage": progress["stage"],
        "message": UPDATE_PROGRESS_MESSAGES[progress["stage"]],
        "updated_at": now,
    }, ensure_ascii=False)
    with database() as connection:
        row = connection.execute(
            "SELECT payload_json FROM agent_commands "
            "WHERE id = ? AND node_id = ? AND status = 'in_progress'",
            (command_id, node_id),
        ).fetchone()
        if row is None:
            return False
        command = json.loads(row["payload_json"])
        if not isinstance(command, dict) or command.get("action") != "update":
            return False
        cursor = connection.execute(
            "UPDATE agent_commands SET result_json = ?, updated_at = ? "
            "WHERE id = ? AND node_id = ? AND status = 'in_progress'",
            (progress_json, now, command_id, node_id),
        )
    return cursor.rowcount == 1


def take_agent_command_result(command_id):
    now = datetime.now(timezone.utc)
    with database() as connection:
        row = connection.execute(
            "SELECT status, result_json, created_at, payload_json "
            "FROM agent_commands WHERE id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] in ("completed", "failed"):
            result = json.loads(row["result_json"])
            connection.execute("DELETE FROM agent_commands WHERE id = ?", (command_id,))
            return {"status": row["status"], "result": result}
        if row["status"] == "in_progress" and row["result_json"]:
            return {"status": row["status"], "progress": json.loads(row["result_json"])}
        created_at = datetime.fromisoformat(row["created_at"])
        payload = json.loads(row["payload_json"])
        ttl = agent_command_ttl(payload)
        if (now - created_at).total_seconds() > ttl:
            connection.execute("DELETE FROM agent_commands WHERE id = ?", (command_id,))
            return {"status": "expired"}
        return {"status": row["status"]}


def admin_credentials():
    username = os.environ.get("PORTAL_ADMIN_USER", "")
    password = os.environ.get("PORTAL_ADMIN_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Ange både PORTAL_ADMIN_USER och PORTAL_ADMIN_PASSWORD.")
    if len(password) < 16:
        raise RuntimeError("PORTAL_ADMIN_PASSWORD måste innehålla minst 16 tecken.")
    return username, password


class PortalHandler(BaseHTTPRequestHandler):
    server_version = f"SvxLinkPortal/{PORTAL_VERSION}"

    def log_message(self, format_string, *args):
        logging.info("%s - %s", self.address_string(), format_string % args)

    def send_json(self, status, payload, headers=()):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_asset(self, asset):
        body = (WEB_ROOT / asset).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_ASSETS[asset])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def session_cookie(self, value, max_age):
        parts = [f"{SESSION_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict",
                 f"Max-Age={max_age}"]
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def handle_login(self):
        try:
            payload = self.read_json_body(MAX_REQUEST_BYTES)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"username", "password"}
                or not isinstance(payload["username"], str)
                or not isinstance(payload["password"], str)
            ):
                raise ValueError("Ange användarnamn och lösenord.")
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        username = payload["username"].strip()
        key = (self.client_address[0], username.lower())
        if login_throttled(key):
            self.send_json(429, {"error": "För många inloggningsförsök. Försök igen om en stund."})
            return
        result = None
        if len(username) <= 100 and len(payload["password"]) <= 200:
            result = authenticate(username, payload["password"])
        if result is None:
            record_login_failure(key)
            self.send_json(401, {"error": "Fel användarnamn eller lösenord."})
            return
        clear_login_failures(key)
        token = create_session(result[0])
        self.send_json(
            200,
            {"username": result[0], "role": result[1]},
            [self.session_cookie(token, SESSION_TTL_SECONDS)],
        )

    def read_json_body(self, max_bytes):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Ogiltig Content-Length.") from exc
        if content_length < 1 or content_length > max_bytes:
            raise ValueError("Begäran saknar innehåll eller är större än tillåtet.")
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            raise ValueError("Begäran är ofullständig.")
        return json.loads(raw.decode("utf-8"))

    def authenticated_agent_id(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        with database() as connection:
            node = connection.execute(
                "SELECT id FROM nodes WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return node["id"] if node else None

    def require_admin(self, admin_only=False):
        user = session_user(self.session_token())
        if user is None:
            if self.command == "GET" and not urlparse(self.path).path.startswith("/api/"):
                target = self.path if self.path.startswith("/") else "/"
                self.send_redirect(f"/login?next={quote(target, safe='')}")
            else:
                self.send_json(401, {"error": "Inloggning krävs."})
            return False
        self.actor, self.role = user
        if admin_only and self.role != "admin":
            self.send_json(403, {"error": "Kräver administratörsbehörighet."})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/agent/commands":
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            touch_node(node_id)
            self.send_json(200, {
                "commands": claim_agent_commands(node_id),
                "watch": RADIO_HUB.watched(node_id),
                "audio": AUDIO_HUB.wanted(node_id),
                "audio_udp_port": AUDIO_UDP.port if AUDIO_UDP else None,
            })
            return
        if path == "/login":
            if session_user(self.session_token()) is not None:
                self.send_redirect("/")
                return
            body = (WEB_ROOT / "login.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/assets/") and path[len("/assets/"):] in PUBLIC_ASSETS:
            self.send_asset(path[len("/assets/"):])
            return
        if not self.require_admin():
            return
        if path == "/":
            body = (WEB_ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/nodes/"):
            node_path = path[len("/nodes/"):]
            path_parts = node_path.split("/")
            if len(path_parts) == 1:
                node_id = path_parts[0]
                page = "node.html"
            elif len(path_parts) == 2 and path_parts[1] in ADMIN_NODE_PAGES:
                if not self.require_admin(admin_only=True):
                    return
                node_id = path_parts[0]
                page = ADMIN_NODE_PAGES[path_parts[1]]
            elif len(path_parts) == 2 and path_parts[1] in NODE_PAGES:
                node_id = path_parts[0]
                page = NODE_PAGES[path_parts[1]]
            else:
                self.send_json(404, {"error": "Sidan hittades inte."})
                return
            if not node_id or node_by_id(node_id) is None:
                self.send_json(404, {"error": "Noden hittades inte."})
                return
            body = (WEB_ROOT / page).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/dtmf":
            body = (WEB_ROOT / "dtmf.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/assets/") and path[len("/assets/"):] in STATIC_ASSETS:
            self.send_asset(path[len("/assets/"):])
        elif path == "/admin":
            if not self.require_admin(admin_only=True):
                return
            body = (WEB_ROOT / "admin.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/me":
            self.send_json(200, {"username": self.actor, "role": self.role})
        elif path == "/api/users":
            if self.require_admin(admin_only=True):
                self.send_json(200, {"users": list_users()})
        elif path == "/api/node-config":
            if self.require_admin(admin_only=True):
                self.send_json(200, {"config": get_node_config()})
        elif path == "/api/audit":
            if self.require_admin(admin_only=True):
                query = parse_qs(urlparse(self.path).query)
                node_id = query.get("node_id", [""])[0]
                self.send_json(200, {"entries": list_audit(node_id=node_id or None)})
        elif path.startswith("/api/nodes/") and path.endswith("/service-log"):
            node_id = unquote(path[len("/api/nodes/"):-len("/service-log")])
            if not node_id or "/" in node_id or node_by_id(node_id) is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                with database() as connection:
                    rows = connection.execute(
                        "SELECT id, created_at, actor, entry FROM service_log "
                        "WHERE node_id = ? ORDER BY id DESC",
                        (node_id,),
                    ).fetchall()
                self.send_json(200, {"entries": [dict(row) for row in rows]})
        elif path == "/api/backup":
            if not self.require_admin(admin_only=True):
                return
            body = json.dumps(create_backup(self.actor), ensure_ascii=False, indent=2).encode("utf-8")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="svxlink-portal-backup-{stamp}.json"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/dtmf-buttons":
            self.send_json(200, {"buttons": list_dtmf_buttons()})
        elif path == "/api/reflector-events":
            self.stream_reflector_events()
        elif path == "/api/svxlink-releases":
            self.send_json(200, {"releases": stable_svxlink_releases()})
        elif path == "/api/nodes":
            self.send_json(200, {
                "nodes": node_rows(),
                "portal_version": PORTAL_VERSION,
                "latest_svxlink_version": latest_svxlink_version(),
                "can_delete_nodes": self.role == "admin",
                "role": self.role,
            })
        elif path.startswith("/api/nodes/") and path.endswith("/audio"):
            node_id = path[len("/api/nodes/"):-len("/audio")]
            node = node_by_id(node_id) if node_id and "/" not in node_id else None
            if node is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.stream_audio(node)
        elif path.startswith("/api/nodes/") and path.endswith("/radio-events"):
            node_id = path[len("/api/nodes/"):-len("/radio-events")]
            node = node_by_id(node_id) if node_id and "/" not in node_id else None
            if node is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.stream_radio_events(node)
        elif path.startswith("/api/nodes/"):
            node_id = path[len("/api/nodes/"):]
            node = node_by_id(node_id) if node_id and "/" not in node_id else None
            if node is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(200, {"node": node})
        elif path.startswith("/api/config-commands/"):
            if not self.require_admin(admin_only=True):
                return
            command_id = path[len("/api/config-commands/"):]
            if not command_id or "/" in command_id:
                self.send_json(404, {"error": "Konfigurationsåtgärden hittades inte."})
                return
            result = take_agent_command_result(command_id)
            if result is None:
                self.send_json(404, {"error": "Konfigurationsåtgärden hittades inte."})
            else:
                self.send_json(200, result)
        elif path.startswith("/api/commands/"):
            command_id = path[len("/api/commands/"):]
            if not command_id or "/" in command_id:
                self.send_json(404, {"error": "Åtgärden hittades inte."})
                return
            result = take_agent_command_result(command_id)
            if result is None:
                self.send_json(404, {"error": "Åtgärden hittades inte."})
            else:
                self.send_json(200, result)
        else:
            self.send_json(404, {"error": "Sidan hittades inte."})

    def stream_reflector_events(self):
        REFLECTOR_HUB.ensure_started()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(data):
            payload = json.dumps(data, ensure_ascii=False)
            self.wfile.write(f"event: reflector\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            self.wfile.write(b"retry: 2000\n\n")
            sequence, state = REFLECTOR_HUB.snapshot()
            send(state)
            while True:
                new_sequence, state = REFLECTOR_HUB.wait(sequence, RADIO_STREAM_KEEPALIVE_SECONDS)
                if new_sequence > sequence:
                    sequence = new_sequence
                    send(state)
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def stream_audio(self, node):
        node_id = node["id"]
        if not ((node["status"] or {}).get("udp_audio") or {}).get("listen"):
            self.send_json(409, {"error": "Noden har ingen UDP-ljudkälla konfigurerad."})
            return
        sequence = AUDIO_HUB.subscribe(node_id)
        if sequence is None:
            self.send_json(429, {"error": "För många samtidiga ljudlyssnare för noden."})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                new_sequence, chunks = AUDIO_HUB.wait(node_id, sequence, 5)
                if chunks:
                    sequence = new_sequence
                    for data in chunks:
                        self.wfile.write(len(data).to_bytes(2, "big") + data)
                else:
                    self.wfile.write(b"\x00\x00")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            AUDIO_HUB.unsubscribe(node_id)

    def read_binary_body(self, max_bytes):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Ogiltig Content-Length.") from exc
        if content_length < 0 or content_length > max_bytes:
            raise ValueError("Begäran är större än tillåtet.")
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            raise ValueError("Begäran är ofullständig.")
        return raw

    def stream_radio_events(self, node):
        node_id = node["id"]
        subscription = RADIO_HUB.subscribe(node_id)
        if subscription is None:
            self.send_json(429, {"error": "För många samtidiga radioströmmar för noden."})
            return
        sequence, latest = subscription
        if latest is None and node["status"]:
            latest = {"t": int(time.time() * 1000), **node["status"].get("radio", {})}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(event_name, data):
            payload = json.dumps(data, ensure_ascii=False)
            self.wfile.write(f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            self.wfile.write(b"retry: 2000\n\n")
            if latest is not None:
                send("radio", latest)
            while True:
                new_sequence, events = RADIO_HUB.wait(
                    node_id, sequence, RADIO_STREAM_KEEPALIVE_SECONDS
                )
                if events:
                    for _, event in events:
                        send("radio", event)
                    sequence = new_sequence
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            RADIO_HUB.unsubscribe(node_id)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/nodes/"):
            if not self.require_admin(admin_only=True):
                return
            node_id = unquote(path[len("/api/nodes/"):])
            if not node_id or "/" in node_id:
                self.send_json(404, {"error": "Noden hittades inte."})
                return
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"password"}
                    or not isinstance(payload["password"], str)
                ):
                    raise ValueError("Bekräfta borttagningen med ditt lösenord.")
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if authenticate_user(self.actor, payload["password"]) != "admin":
                self.send_json(403, {"error": "Lösenordet kunde inte bekräftas."})
                return
            if not delete_node(self.actor, node_id):
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(200, {"message": "Noden är borttagen."})
            return
        if not path.startswith("/api/users/"):
            self.send_json(404, {"error": "Sidan hittades inte."})
            return
        if not self.require_admin(admin_only=True):
            return
        username = unquote(path[len("/api/users/"):])
        if not delete_user(self.actor, username):
            self.send_json(404, {"error": "Användaren hittades inte."})
        else:
            self.send_json(200, {"users": list_users()})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            self.handle_login()
            return
        if path == "/api/logout":
            destroy_session(self.session_token())
            self.send_json(200, {"ok": True}, [self.session_cookie("", 0)])
            return
        if path == "/api/nodes":
            if not self.require_admin(admin_only=True):
                return
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"name"}
                    or not isinstance(payload["name"], str)
                ):
                    raise ValueError("Begäran måste innehålla nodnamnet som text.")
                credentials = provision_node(payload["name"])
                with database() as connection:
                    record_audit(connection, self.actor, "node.create", credentials["name"],
                                 credentials["node_id"], credentials["name"])
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, {
                **credentials,
                "install_command": build_install_command(
                    get_node_config(), credentials["node_id"], credentials["token"]
                ),
            })
            return
        if path == "/api/node-config":
            if not self.require_admin(admin_only=True):
                return
            try:
                config = save_node_config(
                    self.actor, self.read_json_body(MAX_REQUEST_BYTES)
                )
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, {"config": config})
            return
        if path == "/api/restore":
            if not self.require_admin(admin_only=True):
                return
            try:
                payload = self.read_json_body(MAX_BACKUP_BYTES)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"password", "backup"}
                    or not isinstance(payload["password"], str)
                ):
                    raise ValueError("Begäran måste innehålla lösenord och säkerhetskopia.")
                if authenticate_user(self.actor, payload["password"]) != "admin":
                    self.send_json(403, {"error": "Lösenordet kunde inte bekräftas."})
                    return
                counts = restore_backup(self.actor, payload["backup"])
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, {"message": "Säkerhetskopian är återställd.", "restored": counts})
            return
        if path.startswith("/api/nodes/") and path.endswith("/service-log"):
            if not self.require_admin():
                return
            node_id = unquote(path[len("/api/nodes/"):-len("/service-log")])
            if not node_id or "/" in node_id:
                self.send_json(404, {"error": "Noden hittades inte."})
                return
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"entry"}
                    or not isinstance(payload["entry"], str)
                ):
                    raise ValueError("Begäran måste innehålla serviceloggen som text.")
                entry = payload["entry"].strip()
                if not entry or len(entry) > 2000:
                    raise ValueError("Loggtexten måste innehålla 1–2000 tecken.")
                with database() as connection:
                    node = connection.execute(
                        "SELECT id FROM nodes WHERE id = ?", (node_id,)
                    ).fetchone()
                    if node is None:
                        self.send_json(404, {"error": "Noden hittades inte."})
                        return
                    created_at = datetime.now(timezone.utc).isoformat()
                    cursor = connection.execute(
                        "INSERT INTO service_log (node_id, created_at, actor, entry) "
                        "VALUES (?, ?, ?, ?)",
                        (node_id, created_at, self.actor, entry),
                    )
                    log_entry = {
                        "id": cursor.lastrowid,
                        "created_at": created_at,
                        "actor": self.actor,
                        "entry": entry,
                    }
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, {"entry": log_entry})
            return
        if path == "/api/users":
            if not self.require_admin(admin_only=True):
                return
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if not isinstance(payload, dict) or set(payload) != {"username", "password", "role"}:
                    raise ValueError("Begäran måste innehålla användarnamn, lösenord och roll.")
                create_user(self.actor, payload["username"], payload["password"], payload["role"])
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, {"users": list_users()})
            return
        if path.startswith("/api/users/"):
            if not self.require_admin(admin_only=True):
                return
            username = unquote(path[len("/api/users/"):])
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if (
                    not isinstance(payload, dict)
                    or not payload
                    or not set(payload) <= {"password", "role"}
                ):
                    raise ValueError("Ange nytt lösenord och/eller ny roll.")
                found = update_user(
                    self.actor, username, payload.get("password"), payload.get("role")
                )
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if not found:
                self.send_json(404, {"error": "Användaren hittades inte."})
            else:
                self.send_json(200, {"users": list_users()})
            return
        if path == "/api/dtmf-buttons":
            if not self.require_admin(admin_only=True):
                return
            try:
                buttons = validate_dtmf_buttons(self.read_json_body(MAX_REQUEST_BYTES))
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            replace_dtmf_buttons(buttons)
            self.send_json(200, {"buttons": buttons})
            return
        if path == "/api/dtmf-commands":
            if not self.require_admin():
                return
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"node_ids", "sequence"}
                    or not isinstance(payload["node_ids"], list)
                    or not 1 <= len(payload["node_ids"]) <= MAX_DTMF_NODES
                ):
                    raise ValueError(
                        f"Begäran måste innehålla 1–{MAX_DTMF_NODES} nod-ID:n och en DTMF-sekvens."
                    )
                command = validate_dtmf_command(
                    {"action": "dtmf", "sequence": payload["sequence"]}
                )
                node_ids = payload["node_ids"]
                if (
                    any(
                        not isinstance(node_id, str)
                        or not re.fullmatch(r"[0-9a-f]{32}", node_id)
                        for node_id in node_ids
                    )
                    or len(set(node_ids)) != len(node_ids)
                ):
                    raise ValueError("Nod-ID:n måste vara unika och ha giltigt format.")
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return

            results = []
            for node_id in node_ids:
                node = node_by_id(node_id)
                if node is None:
                    results.append(
                        {
                            "node_id": node_id,
                            "name": node_id,
                            "status": "failed",
                            "error": "Noden hittades inte.",
                        }
                    )
                    continue
                try:
                    command_id = enqueue_agent_command(node_id, command, self.actor)
                except (ValueError, RuntimeError) as exc:
                    results.append(
                        {
                            "node_id": node_id,
                            "name": node["name"],
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
                    continue
                results.append(
                    {
                        "node_id": node_id,
                        "name": node["name"],
                        "command_id": command_id,
                        "status": "pending" if command_id else "failed",
                        **({"error": "Noden hittades inte."} if command_id is None else {}),
                    }
                )
            self.send_json(202, {"results": results})
            return

        if path.startswith("/api/nodes/") and path.endswith("/dtmf-commands"):
            if not self.require_admin():
                return
            node_id = path[len("/api/nodes/"):-len("/dtmf-commands")].rstrip("/")
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if not isinstance(payload, dict) or set(payload) != {"sequence"}:
                    raise ValueError("DTMF-begäran måste innehålla en sekvens.")
                command = validate_dtmf_command(
                    {"action": "dtmf", "sequence": payload["sequence"]}
                )
                command_id = enqueue_agent_command(node_id, command, self.actor)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            if command_id is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(202, {"command_id": command_id, "status": "pending"})
            return

        if path.startswith("/api/nodes/") and path.endswith("/update-commands"):
            if not self.require_admin(admin_only=True):
                return
            node_id = path[len("/api/nodes/"):-len("/update-commands")].rstrip("/")
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if not isinstance(payload, dict) or set(payload) != {"channel"}:
                    raise ValueError("Uppdateringsbegäran måste innehålla en kanal.")
                command = validate_update_command(
                    {"action": "update", "channel": payload["channel"]}
                )
                command_id = enqueue_agent_command(node_id, command, self.actor)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            if command_id is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(202, {"command_id": command_id, "status": "pending"})
            return

        if path.startswith("/api/nodes/") and path.endswith("/service-commands"):
            if not self.require_admin(admin_only=True):
                return
            node_id = path[len("/api/nodes/"):-len("/service-commands")].rstrip("/")
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if not isinstance(payload, dict) or set(payload) != {"operation"}:
                    raise ValueError("Tjänstebegäran måste innehålla en åtgärd.")
                command = validate_service_command(
                    {"action": "service", "operation": payload["operation"]}
                )
                command_id = enqueue_agent_command(node_id, command, self.actor)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            if command_id is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(202, {"command_id": command_id, "status": "pending"})
            return

        if path.startswith("/api/nodes/") and path.endswith("/config-commands"):
            if not self.require_admin(admin_only=True):
                return
            node_id = path[len("/api/nodes/"):-len("/config-commands")].rstrip("/")
            try:
                payload = self.read_json_body(MAX_COMMAND_REQUEST_BYTES)
                command_id = enqueue_agent_command(node_id, payload, self.actor)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            if command_id is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(202, {"command_id": command_id, "status": "pending"})
            return

        if path.startswith("/api/v1/agent/commands/") and path.endswith("/progress"):
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            command_id = path[len("/api/v1/agent/commands/"):-len("/progress")].rstrip("/")
            try:
                progress = self.read_json_body(MAX_REQUEST_BYTES)
                updated = report_agent_command_progress(node_id, command_id, progress)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if not updated:
                self.send_json(404, {"error": "SvxLink-uppdateringen hittades inte."})
            else:
                touch_node(node_id)
                self.send_json(200, {"ok": True})
            return

        if path.startswith("/api/v1/agent/commands/") and path.endswith("/result"):
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            command_id = path[len("/api/v1/agent/commands/"):-len("/result")].rstrip("/")
            try:
                result = self.read_json_body(MAX_COMMAND_RESULT_BYTES)
                completed = complete_agent_command(node_id, command_id, result)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if not completed:
                self.send_json(404, {"error": "Konfigurationsåtgärden hittades inte."})
            else:
                self.send_json(200, {"ok": True})
            return

        if path == "/api/v1/agent/audio":
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            try:
                pcm = self.read_binary_body(MAX_AUDIO_REQUEST_BYTES)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if pcm:
                AUDIO_HUB.add_audio(node_id, pcm)
            touch_node(node_id)
            flags, mic = AUDIO_HUB.take_for_agent(node_id)
            body = bytes([flags]) + mic
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/api/nodes/") and path.endswith("/audio-mic"):
            if not self.require_admin():
                return
            node_id = path[len("/api/nodes/"):-len("/audio-mic")]
            node = node_by_id(node_id) if node_id and "/" not in node_id else None
            if node is None:
                self.send_json(404, {"error": "Noden hittades inte."})
                return
            if not ((node["status"] or {}).get("udp_audio") or {}).get("talk"):
                self.send_json(409, {"error": "Noden har ingen UDP-ljudingång konfigurerad."})
                return
            if not node["online"]:
                self.send_json(409, {"error": "Noden är offline."})
                return
            try:
                body = self.read_binary_body(MAX_AUDIO_REQUEST_BYTES)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if not body:
                self.send_json(400, {"error": "Begäran saknar innehåll."})
                return
            accepted, started, owner = AUDIO_HUB.push_mic(
                node_id, self.actor, bool(body[0] & 1), body[1:]
            )
            if not accepted:
                self.send_json(409, {"error": f"{owner} sänder redan via noden."})
                return
            if started:
                with database() as connection:
                    record_audit(connection, self.actor, "node.ptt", "Fjärrsändning startad",
                                 node_id, node["name"])
            self.send_json(200, {"ok": True})
            return

        if path == "/api/v1/agent/radio":
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            try:
                events = validate_radio_events(self.read_json_body(MAX_REQUEST_BYTES))
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            RADIO_HUB.add_events(node_id, events)
            touch_node(node_id)
            self.send_json(200, {"ok": True, "watch": RADIO_HUB.watched(node_id)})
            return

        if path == "/api/v1/agent/logs":
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            try:
                lines = validate_log_lines(self.read_json_body(MAX_REQUEST_BYTES))
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            LIVE_LOGS.add(node_id, lines)
            touch_node(node_id)
            self.send_json(200, {"ok": True})
            return

        if path != "/api/v1/agent/status":
            self.send_json(404, {"error": "Endpoint hittades inte."})
            return
        node_id = self.authenticated_agent_id()
        if node_id is None:
            self.send_json(401, {"error": "Ogiltig nodautentisering."})
            return
        try:
            payload = self.read_json_body(MAX_REQUEST_BYTES)
            status = validate_status(payload)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        LIVE_LOGS.seed(node_id, status.get("logs") or [])
        now = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            connection.execute(
                "UPDATE nodes SET status_json = ?, last_seen = ? WHERE id = ?",
                (json.dumps(status, ensure_ascii=False), now, node_id),
            )
        self.send_json(200, {
            "ok": True,
            "received_at": now,
            "watch": RADIO_HUB.watched(node_id),
        })


def serve():
    admin_credentials()
    with database():
        pass
    host = os.environ.get("PORTAL_HOST", "127.0.0.1")
    port = int(os.environ.get("PORTAL_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), PortalHandler)
    start_audio_udp()
    logging.info("Portal lyssnar på %s:%s.", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Portalservern stängs.")
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    provision_parser = subparsers.add_parser("provision-node", help="Skapa nodidentitet")
    provision_parser.add_argument("--name", required=True, help="Nodens visningsnamn")
    subparsers.add_parser("serve", help="Starta portalens HTTP-server")
    args = parser.parse_args(argv)
    if args.command == "provision-node":
        try:
            credentials = provision_node(args.name)
        except (OSError, sqlite3.Error, ValueError) as exc:
            logging.error("Kunde inte skapa nodidentitet: %s", exc)
            return 1
        print(json.dumps(credentials, ensure_ascii=False, indent=2))
        return 0
    try:
        serve()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logging.error("Portalen kunde inte starta: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
