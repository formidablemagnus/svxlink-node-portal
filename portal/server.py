#!/usr/bin/env python3
"""Small standard-library HTTP server for the SvxLink node portal."""
import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from portal.reflector import REFLECTOR_HUB
from portal.version import read_version

ROOT = Path(__file__).resolve().parent.parent
PORTAL_VERSION = read_version()
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
MAX_SCHEDULES = 100
AGENT_COMMAND_TTL_SECONDS = 300
ONLINE_WINDOW_SECONDS = 30
CONFIG_DOCUMENTS = {"svxlink.conf", "node_info.json"}
CSS = "text/css; charset=utf-8"
JS = "application/javascript; charset=utf-8"
STATIC_ASSETS = {
    "style.css": CSS,
    "sidebar.js": JS,
    "index.js": JS,
    "node.js": JS,
    "config.js": JS,
    "schedules.js": JS,
    "dtmf.js": JS,
}
VALID_RADIO_MODES = {"tx", "rx", "idle", "unknown"}
MAX_RADIO_EVENTS_PER_REQUEST = 100
MAX_RADIO_VIEWERS_PER_NODE = 20
RADIO_STREAM_KEEPALIVE_SECONDS = 15


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
COMMAND_CLEANUP_INTERVAL_SECONDS = 60
_command_cleanup_lock = threading.Lock()
_last_command_cleanup = {}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def database_path():
    return Path(os.environ.get("PORTAL_DATABASE", "/var/lib/svxlink-node-portal/portal.sqlite3"))


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
        """CREATE TABLE IF NOT EXISTS dtmf_buttons (
            position INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            sequence TEXT NOT NULL
        )"""
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
            expired_before = (now - timedelta(seconds=AGENT_COMMAND_TTL_SECONDS)).isoformat()
            delete_before = (
                now - timedelta(seconds=AGENT_COMMAND_TTL_SECONDS + 60)
            ).isoformat()
            connection.execute(
                "UPDATE agent_commands SET status = 'expired', payload_json = '{}', "
                "result_json = NULL, updated_at = ? "
                "WHERE created_at < ? AND status != 'expired'",
                (now.isoformat(), expired_before),
            )
            connection.execute(
                "DELETE FROM agent_commands WHERE created_at < ?",
                (delete_before,),
            )
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
        **validate_status_details(payload),
    }


def optional_text(payload, key, limit):
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"'{key}' måste vara text på högst {limit} tecken.")
    return value


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
    return [node_record(row) for row in rows]


def node_by_id(node_id):
    with database() as connection:
        row = connection.execute(
            "SELECT id, name, status_json, last_seen FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    return node_record(row) if row else None


def node_record(row):
    now = datetime.now(timezone.utc)
    last_seen = row["last_seen"]
    online = False
    if last_seen:
        try:
            seen_at = datetime.fromisoformat(last_seen)
            online = (now - seen_at).total_seconds() <= ONLINE_WINDOW_SECONDS
        except ValueError:
            logging.error("Ogiltig last_seen-tid för nod %s.", row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "online": online,
        "last_seen": last_seen,
        "status": json.loads(row["status_json"]) if row["status_json"] else None,
    }


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
        or payload["operation"] not in ("start", "stop")
    ):
        raise ValueError("Ogiltig SvxLink-tjänståtgärd.")
    return {"action": "service", "operation": payload["operation"]}


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
    if isinstance(payload, dict) and payload.get("action") == "schedules":
        return validate_schedules_command(payload)
    return validate_config_command(payload)


def reject_json_constant(value):
    raise ValueError(f"Ogiltigt JSON-konstantvärde: {value}.")


def enqueue_agent_command(node_id, payload):
    command = validate_agent_command(payload)
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    with database() as connection:
        connection.execute(
            "DELETE FROM agent_commands WHERE created_at < ?",
            ((now - timedelta(seconds=AGENT_COMMAND_TTL_SECONDS)).isoformat(),),
        )
        node = connection.execute(
            "SELECT id, last_seen FROM nodes WHERE id = ?", (node_id,)
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
    return command_id


def claim_agent_commands(node_id):
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    expired_before = (now - timedelta(seconds=AGENT_COMMAND_TTL_SECONDS)).isoformat()
    retry_before = (now - timedelta(seconds=45)).isoformat()
    with database() as connection:
        connection.execute(
            "UPDATE agent_commands SET status = 'expired', updated_at = ? "
            "WHERE node_id = ? AND created_at < ? AND status IN ('pending', 'in_progress')",
            (now_text, node_id, expired_before),
        )
        stale_commands = connection.execute(
            "SELECT id, payload_json FROM agent_commands "
            "WHERE node_id = ? AND status = 'in_progress' AND updated_at < ?",
            (node_id, retry_before),
        ).fetchall()
        for row in stale_commands:
            payload = json.loads(row["payload_json"])
            status = "expired" if payload.get("action") == "dtmf" else "pending"
            connection.execute(
                "UPDATE agent_commands SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'in_progress'",
                (status, now_text, row["id"]),
            )
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
        cursor = connection.execute(
            """UPDATE agent_commands SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ? AND node_id = ? AND status = 'in_progress'""",
            (
                "completed" if result["ok"] else "failed",
                result_json,
                datetime.now(timezone.utc).isoformat(),
                command_id,
                node_id,
            ),
        )
    return cursor.rowcount == 1


def take_agent_command_result(command_id):
    now = datetime.now(timezone.utc)
    with database() as connection:
        row = connection.execute(
            "SELECT status, result_json, created_at FROM agent_commands WHERE id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] in ("completed", "failed"):
            result = json.loads(row["result_json"])
            connection.execute("DELETE FROM agent_commands WHERE id = ?", (command_id,))
            return {"status": row["status"], "result": result}
        created_at = datetime.fromisoformat(row["created_at"])
        if (now - created_at).total_seconds() > AGENT_COMMAND_TTL_SECONDS:
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

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    def require_admin(self):
        header = self.headers.get("Authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError("Wrong authorization scheme")
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeError):
            username, password = "", ""
        expected_user, expected_password = admin_credentials()
        authorized = hmac.compare_digest(username, expected_user) and hmac.compare_digest(
            password, expected_password
        )
        if not authorized:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SvxLink Portal"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        return authorized

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/agent/commands":
            node_id = self.authenticated_agent_id()
            if node_id is None:
                self.send_json(401, {"error": "Ogiltig nodautentisering."})
                return
            self.send_json(200, {
                "commands": claim_agent_commands(node_id),
                "watch": RADIO_HUB.watched(node_id),
            })
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
            elif len(path_parts) == 2 and path_parts[1] == "config":
                node_id = path_parts[0]
                page = "config.html"
            elif len(path_parts) == 2 and path_parts[1] == "schedules":
                node_id = path_parts[0]
                page = "schedules.html"
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
            asset = path[len("/assets/"):]
            body = (WEB_ROOT / asset).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_ASSETS[asset])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/dtmf-buttons":
            self.send_json(200, {"buttons": list_dtmf_buttons()})
        elif path == "/api/reflector-events":
            self.stream_reflector_events()
        elif path == "/api/nodes":
            self.send_json(200, {"nodes": node_rows(), "portal_version": PORTAL_VERSION})
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

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/nodes":
            if not self.require_admin():
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
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, credentials)
            return
        if path == "/api/dtmf-buttons":
            if not self.require_admin():
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
                    command_id = enqueue_agent_command(node_id, command)
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
                command_id = enqueue_agent_command(node_id, command)
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
            if not self.require_admin():
                return
            node_id = path[len("/api/nodes/"):-len("/service-commands")].rstrip("/")
            try:
                payload = self.read_json_body(MAX_REQUEST_BYTES)
                if not isinstance(payload, dict) or set(payload) != {"operation"}:
                    raise ValueError("Tjänstebegäran måste innehålla en åtgärd.")
                command = validate_service_command(
                    {"action": "service", "operation": payload["operation"]}
                )
                command_id = enqueue_agent_command(node_id, command)
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
            if not self.require_admin():
                return
            node_id = path[len("/api/nodes/"):-len("/config-commands")].rstrip("/")
            try:
                payload = self.read_json_body(MAX_COMMAND_REQUEST_BYTES)
                command_id = enqueue_agent_command(node_id, payload)
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
            self.send_json(200, {"ok": True, "watch": RADIO_HUB.watched(node_id)})
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
