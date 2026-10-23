#!/usr/bin/env python3
"""Small standard-library HTTP server for the SvxLink node portal."""
import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
MAX_REQUEST_BYTES = 64 * 1024
MAX_LOG_LINES = 50
MAX_LOG_LINE_BYTES = 512
ONLINE_WINDOW_SECONDS = 30
VALID_RADIO_MODES = {"tx", "rx", "idle", "unknown"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def database_path():
    return Path(os.environ.get("PORTAL_DATABASE", "/var/lib/svxlink-node-portal/portal.sqlite3"))


def connect_database():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
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
    if not isinstance(logic, str) or len(logic) > 128:
        raise ValueError("'logic' måste vara text på högst 128 tecken.")
    if not isinstance(callsign, str) or len(callsign) > 64:
        raise ValueError("'callsign' måste vara text på högst 64 tecken.")
    radio = payload.get("radio", {})
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
        "radio": {"mode": mode, "signal_level": signal_level},
        "logs": logs,
    }


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


def admin_credentials():
    username = os.environ.get("PORTAL_ADMIN_USER", "")
    password = os.environ.get("PORTAL_ADMIN_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Ange både PORTAL_ADMIN_USER och PORTAL_ADMIN_PASSWORD.")
    if len(password) < 16:
        raise RuntimeError("PORTAL_ADMIN_PASSWORD måste innehålla minst 16 tecken.")
    return username, password


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "SvxLinkPortal/0.1"

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
            node_id = path[len("/nodes/"):]
            if not node_id or "/" in node_id or node_by_id(node_id) is None:
                self.send_json(404, {"error": "Noden hittades inte."})
                return
            body = (WEB_ROOT / "node.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/assets/style.css":
            body = (WEB_ROOT / "style.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/nodes":
            self.send_json(200, {"nodes": node_rows()})
        elif path.startswith("/api/nodes/"):
            node_id = path[len("/api/nodes/"):]
            node = node_by_id(node_id) if node_id and "/" not in node_id else None
            if node is None:
                self.send_json(404, {"error": "Noden hittades inte."})
            else:
                self.send_json(200, {"node": node})
        else:
            self.send_json(404, {"error": "Sidan hittades inte."})

    def do_POST(self):
        if urlparse(self.path).path != "/api/v1/agent/status":
            self.send_json(404, {"error": "Endpoint hittades inte."})
            return
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        with database() as connection:
            node = connection.execute(
                "SELECT id FROM nodes WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if node is None:
            self.send_json(401, {"error": "Ogiltig nodautentisering."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Begäran måste vara mellan 1 byte och 16 KB.")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            status = validate_status(payload)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        now = datetime.now(timezone.utc).isoformat()
        with database() as connection:
            connection.execute(
                "UPDATE nodes SET status_json = ?, last_seen = ? WHERE id = ?",
                (json.dumps(status, ensure_ascii=False), now, node["id"]),
            )
        self.send_json(200, {"ok": True, "received_at": now})


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
