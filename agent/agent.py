#!/usr/bin/env python3
"""Collect local SvxLink status and report it to the central portal."""
import base64
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
MAX_LOG_LINES = 50
MAX_LOG_LINE_BYTES = 512
MAX_CONFIG_BYTES = 2 * 1024 * 1024
CONFIG_DOCUMENTS = {"svxlink.conf", "node_info.json"}
CONFIG_HELPER = "/usr/local/libexec/svxlink-node-portal-config"


def required_environment(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Miljövariabeln {name} måste anges.")
    return value


def validate_portal_url(value):
    parsed = urlparse(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("AGENT_PORTAL_URL måste vara en HTTP- eller HTTPS-adress.")
    return value.rstrip("/")


def read_logic_info(config_path):
    try:
        sections = {}
        current_section = None
        with open(config_path, "r", encoding="utf-8") as config_file:
            for raw_line in config_file:
                line = raw_line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and "]" in line:
                    current_section = line[1:line.index("]")].strip()
                    sections.setdefault(current_section, {})
                elif current_section and "=" in line:
                    key, value = line.split("=", 1)
                    sections[current_section][key.strip()] = value.split("#", 1)[0].split(";", 1)[0].strip()
        logic_names = [
            name.strip()
            for name in sections.get("GLOBAL", {}).get("LOGICS", "").split(",")
            if name.strip()
        ]
        for name in logic_names:
            section = sections.get(name, {})
            logic_type = section.get("TYPE", "").strip("\"'").lower()
            if logic_type in ("simplex", "repeater"):
                return {
                    "logic": name,
                    "callsign": section.get("CALLSIGN", "").strip().strip("\"'"),
                }
        return {"logic": "", "callsign": ""}
    except (OSError, UnicodeError) as exc:
        logging.warning("Kunde inte läsa SvxLink-konfigurationen %s: %s", config_path, exc)
        return {"logic": "", "callsign": ""}


def make_status(panel_status, config_path):
    if not isinstance(panel_status, dict):
        raise ValueError("Webpanelen returnerade inte ett JSON-objekt.")
    if type(panel_status.get("active")) is not bool:
        raise ValueError("Webpanelen returnerade ingen giltig tjänstestatus.")
    service_status = panel_status.get("service_status")
    if not isinstance(service_status, str):
        raise ValueError("Webpanelen returnerade ingen giltig tjänstebeskrivning.")
    radio = panel_status.get("radio")
    if not isinstance(radio, dict):
        radio = {}
    mode = radio.get("mode", "unknown")
    if mode not in ("tx", "rx", "idle", "unknown"):
        mode = "unknown"
    signal_level = radio.get("signal_level")
    if type(signal_level) is not int or not 0 <= signal_level <= 100:
        signal_level = None
    current_tg = panel_status.get("current_tg")
    if current_tg is not None and not isinstance(current_tg, (str, int)):
        current_tg = None
    logs = panel_status.get("lines", [])
    if not isinstance(logs, list) or any(not isinstance(line, str) for line in logs):
        raise ValueError("Webpanelen returnerade ogiltiga loggrader.")
    clean_logs = []
    for line in logs[-MAX_LOG_LINES:]:
        clean_line = "".join(
            char for char in line if char in "\t" or ord(char) >= 32
        )
        encoded_line = clean_line.encode("utf-8", errors="replace")[:MAX_LOG_LINE_BYTES]
        clean_logs.append(encoded_line.decode("utf-8", errors="ignore"))
    logic_info = read_logic_info(config_path)
    return {
        "active": panel_status["active"],
        "service_status": service_status[:64],
        "current_tg": str(current_tg) if current_tg is not None else None,
        "logic": logic_info["logic"],
        "callsign": logic_info["callsign"],
        "radio": {"mode": mode, "signal_level": signal_level},
        "logs": clean_logs,
    }


def request_json(url, payload=None, headers=None, timeout=10):
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers)
    opener = build_opener(RejectRedirects())
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def execute_config_command(command):
    if not isinstance(command, dict):
        raise ValueError("Konfigurationsåtgärden måste vara ett JSON-objekt.")
    action = command.get("action")
    document = command.get("document")
    if not isinstance(document, str) or document not in CONFIG_DOCUMENTS:
        raise ValueError("Endast svxlink.conf och node_info.json kan ändras.")
    if not isinstance(action, str) or action not in ("read", "write"):
        raise ValueError("Ogiltig konfigurationsåtgärd från portalen.")
    if action == "read" and set(command) != {"id", "action", "document"}:
        raise ValueError("Läsbegäran från portalen har ogiltigt format.")
    if action == "write":
        if set(command) != {"id", "action", "document", "content", "sha256"}:
            raise ValueError("Skrivbegäran från portalen har ogiltigt format.")
        content = command["content"]
        if not isinstance(content, str):
            raise ValueError("Konfigurationen är ogiltig eller större än 2 MB.")
        try:
            if len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
                raise ValueError("Konfigurationen är ogiltig eller större än 2 MB.")
        except UnicodeEncodeError as exc:
            raise ValueError("Konfigurationen måste innehålla giltig Unicode-text.") from exc
        if document == "node_info.json":
            json.loads(content, parse_constant=reject_json_constant)
        checksum = command["sha256"]
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Ogiltig kontrollsumma för konfigurationen.")
    command_id = command.get("id")
    if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f]{32}", command_id):
        raise ValueError("Ogiltigt ID för konfigurationsåtgärden.")

    try:
        process = subprocess.run(
            ["sudo", "-n", CONFIG_HELPER],
            input=json.dumps(
                {key: value for key, value in command.items() if key != "id"},
                ensure_ascii=False,
            ).encode("utf-8"),
            capture_output=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Tidsgränsen överskreds vid filåtkomst."}
    except OSError as exc:
        return {"ok": False, "error": f"Kunde inte starta konfigurationshjälpen: {exc}"}
    try:
        result = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        return {
            "ok": False,
            "error": f"Konfigurationshjälpen returnerade ett ogiltigt svar: {detail or exc}",
        }
    if process.returncode != 0 and (not isinstance(result, dict) or result.get("ok") is not False):
        return {"ok": False, "error": "Konfigurationshjälpen misslyckades."}
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        return {"ok": False, "error": "Konfigurationshjälpen returnerade ett ogiltigt svar."}
    return result


def process_config_commands(commands_url, results_url, headers):
    try:
        response = request_json(commands_url, headers=headers)
        commands = response.get("commands") if isinstance(response, dict) else None
        if not isinstance(commands, list):
            raise ValueError("Portalen returnerade en ogiltig kommandolista.")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Kunde inte hämta konfigurationsåtgärder från portalen: %s", exc)
        return

    for command in commands:
        command_id = command.get("id") if isinstance(command, dict) else None
        if not isinstance(command_id, str):
            logging.warning("Portalen skickade en konfigurationsåtgärd utan giltigt ID.")
            continue
        try:
            result = execute_config_command(command)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        try:
            request_json(
                f"{results_url}/{command_id}/result",
                payload=result,
                headers=headers,
            )
            if result.get("ok"):
                logging.info(
                    "Konfigurationsåtgärd %s genomförd för %s.",
                    command.get("action"),
                    command.get("document"),
                )
            else:
                logging.warning(
                    "Konfigurationsåtgärd %s för %s misslyckades: %s",
                    command.get("action"),
                    command.get("document"),
                    result.get("error", "okänt fel"),
                )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            logging.warning("Kunde inte skicka resultatet av konfigurationsåtgärden: %s", exc)


def report_status(panel_url, status_url, node_id, config_path, panel_headers, portal_headers):
    try:
        local_status = request_json(panel_url, headers=panel_headers)
        status = make_status(local_status, config_path)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Kunde inte hämta status från den lokala SvxLink-webpanelen: %s", exc)
        return False

    try:
        request_json(status_url, payload={"node_id": node_id, **status}, headers=portal_headers)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Kunde inte rapportera status till portalen: %s", exc)
        return False

    logging.info("Status skickad till portalen för nod %s.", node_id)
    return True


def run():
    portal_url = validate_portal_url(required_environment("AGENT_PORTAL_URL"))
    node_id = required_environment("AGENT_NODE_ID")
    token = required_environment("AGENT_TOKEN")
    if len(token) < 32:
        raise RuntimeError("AGENT_TOKEN måste innehålla minst 32 tecken.")
    panel_url = os.environ.get("AGENT_PANEL_URL", "http://127.0.0.1:8090/api/status")
    panel_user = required_environment("SVXLINK_WEB_USER")
    panel_password = required_environment("SVXLINK_WEB_PASSWORD")
    config_path = os.environ.get("SVXLINK_CONFIG", "/etc/svxlink/svxlink.conf")
    interval = max(2, int(os.environ.get("AGENT_INTERVAL_SECONDS", "5")))
    panel_auth = base64.b64encode(f"{panel_user}:{panel_password}".encode("utf-8")).decode("ascii")
    portal_headers = {"Authorization": f"Bearer {token}"}
    panel_headers = {"Authorization": f"Basic {panel_auth}"}
    status_url = f"{portal_url}/api/v1/agent/status"
    commands_url = f"{portal_url}/api/v1/agent/commands"
    results_url = commands_url

    while True:
        report_status(
            panel_url, status_url, node_id, config_path, panel_headers, portal_headers
        )
        process_config_commands(commands_url, results_url, portal_headers)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
