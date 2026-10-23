#!/usr/bin/env python3
"""Collect local SvxLink status and report it to the central portal."""
import base64
import json
import logging
import os
import re
import stat
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


def read_config_sections(config_path):
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
                sections[current_section][key.strip()] = (
                    value.split("#", 1)[0].split(";", 1)[0].strip()
                )
    return sections


def read_logic_info(config_path):
    try:
        sections = read_config_sections(config_path)
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


def read_dtmf_control_paths(config_path):
    try:
        sections = read_config_sections(config_path)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Kunde inte läsa SvxLink-konfigurationen: {exc}") from exc

    logic_names = [
        name.strip().strip("\"'")
        for name in sections.get("GLOBAL", {}).get("LOGICS", "").split(",")
        if name.strip()
    ]
    paths = []
    for name in logic_names:
        path = sections.get(name, {}).get("DTMF_CTRL_PTY", "").strip().strip("\"'")
        if path and path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError(
            "Ingen DTMF_CTRL_PTY hittades i logikerna i svxlink.conf."
        )
    return paths


def execute_dtmf_command(command, config_path):
    if not isinstance(command, dict) or set(command) != {"id", "action", "sequence"}:
        raise ValueError("DTMF-kommandot från portalen har ogiltigt format.")
    command_id = command["id"]
    sequence = command["sequence"]
    if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f]{32}", command_id):
        raise ValueError("Ogiltigt ID för DTMF-kommandot.")
    if (
        command["action"] != "dtmf"
        or not isinstance(sequence, str)
        or not 1 <= len(sequence) <= 64
        or not re.fullmatch(r"[0-9A-D*#]+", sequence)
    ):
        raise ValueError("DTMF-sekvensen från portalen har ogiltigt format.")

    paths = read_dtmf_control_paths(config_path)
    sent = 0
    for path in paths:
        descriptor = None
        try:
            flags = (
                os.O_WRONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags)
            mode = os.fstat(descriptor).st_mode
            if not (stat.S_ISCHR(mode) or stat.S_ISFIFO(mode)):
                raise ValueError(f"DTMF_CTRL_PTY är inte en PTY eller FIFO: {path}")
            written = os.write(descriptor, sequence.encode("ascii"))
            if written != len(sequence):
                raise OSError("Ofullständig skrivning av DTMF-sekvensen.")
            sent += 1
        except (OSError, ValueError) as exc:
            if sent:
                return {
                    "ok": False,
                    "error": (
                        f"DTMF-sekvensen skickades till {sent} av {len(paths)} "
                        f"PTY-mål innan ett fel uppstod: {exc}"
                    ),
                }
            return {"ok": False, "error": f"Kunde inte skicka DTMF till {path}: {exc}"}
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return {"ok": True}


def execute_agent_command(command, config_path):
    if isinstance(command, dict) and command.get("action") == "dtmf":
        return execute_dtmf_command(command, config_path)
    return execute_config_command(command)


def process_agent_commands(commands_url, results_url, headers, config_path):
    try:
        response = request_json(commands_url, headers=headers)
        commands = response.get("commands") if isinstance(response, dict) else None
        if not isinstance(commands, list):
            raise ValueError("Portalen returnerade en ogiltig kommandolista.")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Kunde inte hämta åtgärder från portalen: %s", exc)
        return

    for command in commands:
        command_id = command.get("id") if isinstance(command, dict) else None
        if not isinstance(command_id, str):
            logging.warning("Portalen skickade en åtgärd utan giltigt ID.")
            continue
        try:
            result = execute_agent_command(command, config_path)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        try:
            request_json(
                f"{results_url}/{command_id}/result",
                payload=result,
                headers=headers,
            )
            if result.get("ok"):
                logging.info(
                    "Åtgärd %s genomförd.",
                    command.get("action"),
                )
            else:
                logging.warning(
                    "Åtgärd %s misslyckades: %s",
                    command.get("action"),
                    result.get("error", "okänt fel"),
                )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            logging.warning("Kunde inte skicka resultatet av åtgärden: %s", exc)


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
        process_agent_commands(
            commands_url, results_url, portal_headers, config_path
        )
        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
