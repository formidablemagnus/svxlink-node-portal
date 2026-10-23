"""Live collection of local SvxLink information (service, config, log, STATE_PTY)."""
import json
import logging
import os
import re
import select
import subprocess
import threading
import time
from collections import deque

MAX_TAIL_BYTES = 256 * 1024
MAX_LINES = 250
MAX_STATE_BUFFER = 65536
RADIO_EVENTS = ("Rx:sql_state", "Voter:sql_state", "Tx:state")
TG_EVENT_RE = re.compile(
    r"(Talker start|Talker stop) on TG #([0-9]+)(?::\s*([^\s\x00]+))?", re.IGNORECASE
)
SELECT_TG_RE = re.compile(r"Selecting TG #([0-9]+)", re.IGNORECASE)
ERROR_RE = re.compile(r"ERROR|FATAL|CRITICAL", re.IGNORECASE)
WARNING_RE = re.compile(r"WARNING|UDP frame\(s\) lost", re.IGNORECASE)
EVENT_RE = re.compile(
    r"ReflectorLogic|Talker start|Talker stop|Node joined|Node left|Selecting TG",
    re.IGNORECASE,
)


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


def unquote(value):
    return value.strip().strip("\"'")


def active_logics(sections):
    names = [
        unquote(name)
        for name in sections.get("GLOBAL", {}).get("LOGICS", "").split(",")
        if name.strip()
    ]
    result = []
    for name in names:
        logic = sections.get(name, {})
        if unquote(logic.get("TYPE", "")).lower() in ("simplex", "repeater"):
            result.append((name, logic))
    return result


def clean_text(value, limit=None):
    text = "".join(char for char in value if char == "\t" or ord(char) >= 32).strip()
    if limit is not None:
        text = text.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")
    return text


def _valid_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class RadioState:
    """Receiver and transmitter state derived from STATE_PTY events."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.receivers = {}
        self.transmitters = {}
        self.seen = False

    def apply(self, event_name, data):
        if event_name == "Tx:state":
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("name"), str)
                or not data["name"]
                or not isinstance(data.get("transmit"), bool)
            ):
                raise ValueError("felaktig sändarstatus")
            self.transmitters[data["name"]] = data["transmit"]
        elif event_name == "Rx:sql_state":
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("name"), str)
                or not data["name"]
                or not isinstance(data.get("sql_open"), bool)
                or not _valid_int(data.get("siglev"))
            ):
                raise ValueError("felaktig mottagarstatus")
            self.receivers[data["name"]] = {
                "receiving": data["sql_open"],
                "siglev": max(0, min(data["siglev"], 100)),
            }
        else:
            if not isinstance(data, list):
                raise ValueError("felaktig voter-status")
            for receiver in data:
                if (
                    not isinstance(receiver, dict)
                    or not isinstance(receiver.get("name"), str)
                    or not receiver["name"]
                    or not isinstance(receiver.get("enabled"), bool)
                    or not isinstance(receiver.get("sql_open"), bool)
                    or not _valid_int(receiver.get("siglev"))
                    or ("active" in receiver and not isinstance(receiver["active"], bool))
                ):
                    raise ValueError("felaktig voter-mottagarstatus")
                self.receivers[receiver["name"]] = {
                    "receiving": receiver["enabled"]
                    and (receiver.get("active", False) or receiver["sql_open"]),
                    "siglev": max(0, min(receiver["siglev"], 100)),
                }
        self.seen = True

    def snapshot(self, error=None):
        if not self.seen:
            return {
                "mode": "unknown",
                "signal_level": None,
                "receivers": [],
                "transmitters": [],
                "error": error or "Väntar på status från SvxLink.",
            }
        levels = [r["siglev"] for r in self.receivers.values() if r["receiving"]]
        if any(self.transmitters.values()):
            mode, level = "tx", None
        elif levels:
            mode, level = "rx", max(levels)
        else:
            mode, level = "idle", None
        return {
            "mode": mode,
            "signal_level": level,
            "receivers": [
                {"name": name[:64], "receiving": r["receiving"], "siglev": r["siglev"]}
                for name, r in sorted(self.receivers.items())
            ][:32],
            "transmitters": [
                {"name": name[:64], "transmit": value}
                for name, value in sorted(self.transmitters.items())
            ][:32],
            "error": error,
        }


def parse_state_line(raw_line):
    """Return (event_name, data) for a STATE_PTY line, or None if it is not relevant."""
    parts = raw_line.decode("utf-8").split(" ", 2)
    if len(parts) != 3 or parts[1] not in RADIO_EVENTS:
        return None
    return parts[1], json.loads(parts[2])


class Collector:
    """Gathers node information in background threads and reports changes via callbacks."""

    def __init__(self, config_path, log_path, service="svxlink",
                 on_radio=None, on_change=None):
        self.config_path = config_path
        self.log_path = log_path
        self.service = service
        self.on_radio = on_radio or (lambda snapshot: None)
        self.on_change = on_change or (lambda reason: None)
        self._lock = threading.Lock()
        self._radio = RadioState()
        self._radio_error = "Väntar på status från SvxLink."
        self._last_snapshot = self._radio.snapshot(self._radio_error)
        self._lines = deque(maxlen=MAX_LINES)
        self._log_error = None
        self._service = ("okänt", False)
        self._config = {"key": None, "sections": {}, "error": None}
        self._pty_checks = []
        self._stop = threading.Event()
        self._log_file = None
        self._log_identity = None
        self._log_offset = 0
        self._log_buffer = b""

    def start(self):
        for target, name in (
            (self._state_loop, "state-pty"),
            (self._housekeeping_loop, "housekeeping"),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._close_log()

    def config_sections(self):
        try:
            file_stat = os.stat(self.config_path)
            key = (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)
            with self._lock:
                if key == self._config["key"]:
                    return self._config["sections"], None
            sections = read_config_sections(self.config_path)
            with self._lock:
                self._config.update({"key": key, "sections": sections, "error": None})
            return sections, None
        except (OSError, UnicodeError) as exc:
            return {}, f"Kunde inte läsa {self.config_path}: {exc}"

    def radio_snapshot(self):
        with self._lock:
            return self._last_snapshot

    def _publish_radio(self, error):
        with self._lock:
            snapshot = self._radio.snapshot(error)
            changed = snapshot != self._last_snapshot
            self._last_snapshot = snapshot
        if changed:
            self.on_radio(snapshot)
            if snapshot["mode"] != "unknown":
                self.on_change("radio")
        return changed

    def _set_radio_unavailable(self, error):
        with self._lock:
            self._radio.reset()
        self._publish_radio(error)

    def _state_pty_path(self):
        if not self._service[1]:
            return None, "SvxLink-tjänsten är stoppad."
        sections, error = self.config_sections()
        if error:
            return None, error
        logics = active_logics(sections)
        if not logics:
            return None, "Ingen aktiv simplex- eller repeaterlogik hittades i [GLOBAL] LOGICS."
        name, logic = logics[0]
        path = unquote(logic.get("STATE_PTY", ""))
        if not path:
            return None, f"STATE_PTY saknas i [{name}]."
        return path, None

    def _state_loop(self):
        while not self._stop.is_set():
            path, error = self._state_pty_path()
            if path is None:
                self._set_radio_unavailable(error)
                self._stop.wait(1)
                continue
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0),
                )
            except OSError as exc:
                self._set_radio_unavailable(f"Kunde inte läsa STATE_PTY {path}: {exc}")
                self._stop.wait(1)
                continue
            try:
                self._read_state(descriptor, path)
            except OSError as exc:
                self._set_radio_unavailable(f"Kunde inte läsa STATE_PTY {path}: {exc}")
            finally:
                os.close(descriptor)
            self._stop.wait(0.5)

    def _read_state(self, descriptor, path):
        buffer = b""
        while not self._stop.is_set():
            current_path, _ = self._state_pty_path()
            if current_path != path:
                return
            ready, _, _ = select.select([descriptor], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(descriptor, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                # No writer on the PTY; avoid spinning on EOF.
                self._stop.wait(0.2)
                continue
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop()
            parse_error = None
            if len(buffer) > MAX_STATE_BUFFER:
                buffer = b""
                parse_error = "Statusraden från STATE_PTY var för lång."
            for raw_line in lines:
                try:
                    parsed = parse_state_line(raw_line)
                    if parsed is None:
                        continue
                    with self._lock:
                        self._radio.apply(*parsed)
                except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    parse_error = f"Kunde inte tolka status från STATE_PTY: {exc}"
            self._publish_radio(parse_error)

    def _housekeeping_loop(self):
        next_slow = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_slow:
                    self._refresh_service()
                    self._refresh_pty_checks()
                    next_slow = now + 1
                self._poll_log()
            except Exception:
                logging.exception("Fel i bakgrundsinsamlingen")
            self._stop.wait(0.1)

    def _refresh_service(self):
        try:
            result = subprocess.run(
                ["systemctl", "is-active", self.service],
                capture_output=True, text=True, timeout=2, check=False,
            )
            value = (result.stdout or result.stderr).strip() or "okänt"
            status = (value[:64], value == "active")
        except (OSError, subprocess.SubprocessError) as exc:
            logging.warning("Kunde inte läsa status för tjänsten %s: %s", self.service, exc)
            status = ("okänt", False)
        with self._lock:
            changed = status != self._service
            self._service = status
        if changed:
            self.on_change("service")

    def _refresh_pty_checks(self):
        sections, error = self.config_sections()
        if error:
            issues = [{"variable": "svxlink.conf", "logic": None,
                       "path": self.config_path, "reason": error[:256]}]
        else:
            logics = active_logics(sections)
            if not logics:
                issues = [{"variable": "LOGICS", "logic": None, "path": None,
                           "reason": "Ingen aktiv simplex- eller repeaterlogik hittades."}]
            else:
                name, logic = logics[0]
                issues = []
                for variable, access in (("DTMF_CTRL_PTY", os.W_OK), ("STATE_PTY", os.R_OK)):
                    path = unquote(logic.get(variable, ""))
                    if not path:
                        reason = f"{variable} saknas eller är kommenterad i [{name}]."
                    elif not self._service[1]:
                        continue
                    elif not os.path.exists(path):
                        reason = f"{path} är inte aktiv eller kan inte nås."
                    elif not os.access(path, access):
                        reason = f"Agenten saknar behörighet att använda {path}."
                    else:
                        continue
                    issues.append({"variable": variable, "logic": name,
                                   "path": path or None, "reason": reason[:256]})
        with self._lock:
            changed = issues != self._pty_checks
            self._pty_checks = issues
        if changed:
            self.on_change("pty")

    def _open_log(self):
        file_obj = open(self.log_path, "rb")
        file_stat = os.fstat(file_obj.fileno())
        offset = max(0, file_stat.st_size - MAX_TAIL_BYTES)
        file_obj.seek(offset)
        raw = file_obj.read(MAX_TAIL_BYTES)
        if offset:
            first_newline = raw.find(b"\n")
            raw = raw[first_newline + 1:] if first_newline >= 0 else b""
        lines = [clean_text(line.decode("utf-8", errors="replace")) for line in raw.splitlines()]
        with self._lock:
            self._lines.clear()
            self._lines.extend(line for line in lines if line)
        self._log_file = file_obj
        self._log_identity = (file_stat.st_dev, file_stat.st_ino)
        self._log_offset = file_obj.tell()
        self._log_buffer = b""

    def _close_log(self):
        if self._log_file is not None:
            self._log_file.close()
        self._log_file = None
        self._log_identity = None

    def _poll_log(self):
        new_lines = []
        try:
            path_stat = os.stat(self.log_path)
            identity = (path_stat.st_dev, path_stat.st_ino)
            if self._log_file is None or identity != self._log_identity:
                self._close_log()
                self._open_log()
                new_lines.append(None)
            else:
                size = os.fstat(self._log_file.fileno()).st_size
                if size < self._log_offset:
                    self._close_log()
                    self._open_log()
                    new_lines.append(None)
                elif size > self._log_offset:
                    self._log_file.seek(self._log_offset)
                    raw = self._log_file.read(min(size - self._log_offset, MAX_TAIL_BYTES))
                    self._log_offset = self._log_file.tell()
                    pending = (self._log_buffer + raw).split(b"\n")
                    self._log_buffer = pending.pop()
                    with self._lock:
                        for raw_line in pending:
                            line = clean_text(raw_line.decode("utf-8", errors="replace"))
                            if line:
                                self._lines.append(line)
                                new_lines.append(line)
            error = None
        except OSError as exc:
            self._close_log()
            error = f"Kunde inte läsa loggfilen {self.log_path}: {exc}"
        with self._lock:
            error_changed = error != self._log_error
            self._log_error = error
        if new_lines or error_changed:
            self.on_change("log")

    def snapshot(self, max_logs=50, max_line_bytes=512):
        sections, _ = self.config_sections()
        logic_name, callsign = "", ""
        logics = active_logics(sections)
        if logics:
            logic_name = logics[0][0]
            callsign = unquote(logics[0][1].get("CALLSIGN", ""))
        monitor_tgs, monitor_error = self._monitor_tgs(sections)
        with self._lock:
            lines = list(self._lines)
            log_error = self._log_error
            service_status, active = self._service
            radio = dict(self._last_snapshot)
            pty_checks = list(self._pty_checks)
        tg_event = tg_time = ""
        for line in reversed(lines):
            match = TG_EVENT_RE.search(line)
            if match:
                tg_event = f"TG {match.group(2)} · {match.group(1)}" + (
                    f" · {match.group(3)}" if match.group(3) else ""
                )
                tg_time = line[:24].strip()
                break
        current_tg = None
        for line in reversed(lines):
            match = SELECT_TG_RE.search(line)
            if match:
                current_tg = match.group(1)
                break
        logs = [clean_text(line, max_line_bytes) for line in lines[-max_logs:]]
        if log_error:
            logs.append(clean_text(log_error, max_line_bytes))
        return {
            "active": active,
            "service_status": service_status,
            "current_tg": current_tg,
            "logic": logic_name[:128],
            "callsign": callsign[:64],
            "radio": radio,
            "logs": logs[-max_logs:],
            "tg_event": tg_event[:128],
            "tg_time": tg_time[:32],
            "monitor_tgs": monitor_tgs,
            "monitor_tgs_error": monitor_error,
            "pty_checks": pty_checks,
            "errors": sum(1 for line in lines if ERROR_RE.search(line)),
            "warnings": sum(1 for line in lines if WARNING_RE.search(line)),
            "events": sum(1 for line in lines if EVENT_RE.search(line)),
        }

    @staticmethod
    def _monitor_tgs(sections):
        value = sections.get("ReflectorLogic", {}).get("MONITOR_TGS")
        if value is None:
            return [], "MONITOR_TGS saknas i [ReflectorLogic]."
        talkgroups = []
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if not item.isdigit() or int(item) <= 0:
                return [], "MONITOR_TGS innehåller ett ogiltigt talgruppsvärde."
            normalized = str(int(item))
            if normalized not in talkgroups:
                talkgroups.append(normalized)
        return talkgroups[:64], None
