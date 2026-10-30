"""Live reflector status stream shared by all browser viewers."""
import json
import logging
import os
import threading
import time
import urllib.request
import uuid

DEFAULT_STREAM_URLS = (
    "https://svxreflector-fernebo.sk3w.se/api/stream,"
    "https://svxreflector-kista.sk3w.se/api/stream"
)
# Kommaseparerad lista; varje reflektor har en egen nodmängd som summeras.
REFLECTOR_STREAM_URLS = [
    url.strip()
    for url in os.environ.get("SVXLINK_REFLECTOR_STREAM_URL", DEFAULT_STREAM_URLS).split(",")
    if url.strip()
]
MAX_SSE_BYTES = 5 * 1024 * 1024


def summarize_nodes(nodes):
    if not isinstance(nodes, dict):
        raise ValueError("Reflektorströmmen saknar ett giltigt nodes-objekt.")
    by_tg = {}
    visible = 0
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("hidden", False):
            continue
        visible += 1
        try:
            tg = int(node.get("tg", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tg <= 0:
            continue
        entry = by_tg.setdefault(tg, {"tg": tg, "nodes": 0, "talkers": 0})
        entry["nodes"] += 1
        if node.get("isTalker") is True:
            entry["talkers"] += 1
    groups = sorted(by_tg.values(), key=lambda i: (-i["talkers"], -i["nodes"], i["tg"]))
    return {"node_count": visible, "talkgroups": groups}


def _parent(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Ogiltig JSON Pointer i reflektorströmmen.")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
    parent = document
    for part in parts[:-1]:
        if isinstance(parent, list):
            parent = parent[int(part)]
        elif isinstance(parent, dict):
            parent = parent[part]
        else:
            raise ValueError("Ogiltig sökväg i reflektorströmmen.")
    return parent, parts[-1]


def apply_patch(document, operations):
    if not isinstance(operations, list):
        raise ValueError("Reflektorströmmen innehåller en ogiltig patch.")
    for op in operations:
        if not isinstance(op, dict) or op.get("op") not in ("add", "remove", "replace"):
            raise ValueError("Reflektorströmmen innehåller en okänd patch-åtgärd.")
        parent, key = _parent(document, op.get("path"))
        action = op["op"]
        if isinstance(parent, list):
            if action == "add":
                parent.insert(len(parent) if key == "-" else int(key), op.get("value"))
            elif action == "remove":
                del parent[int(key)]
            else:
                parent[int(key)] = op["value"]
        elif isinstance(parent, dict):
            if action == "remove":
                del parent[key]
            else:
                parent[key] = op["value"]
        else:
            raise ValueError("Ogiltig måltyp i reflektorströmmen.")
    return document


def merge_states(states):
    """Sum node counts and talkgroups from several reflector sources."""
    groups = {}
    for state in states:
        for group in state["talkgroups"]:
            entry = groups.setdefault(group["tg"], {"tg": group["tg"], "nodes": 0, "talkers": 0})
            entry["nodes"] += group["nodes"]
            entry["talkers"] += group["talkers"]
    ok_states = [state for state in states if state["ok"]]
    connected = any(state["connected"] for state in states)
    errors = [state["error"] for state in states if state["error"]]
    stamps = [state["updated_at"] for state in ok_states if state["updated_at"] is not None]
    return {
        "ok": bool(ok_states),
        "connected": connected,
        "error": None if connected and ok_states and len(ok_states) == len(states) else (" ".join(errors) or None),
        "node_count": sum(state["node_count"] for state in ok_states),
        "talkgroups": sorted(groups.values(), key=lambda i: (-i["talkers"], -i["nodes"], i["tg"])),
        "updated_at": max(stamps) if stamps else None,
    }


class ReflectorHub:
    """Aggregates the live status of every configured reflector."""

    def __init__(self, urls=None, source_factory=None):
        self.condition = threading.Condition()
        self.sequence = 0
        self.started = False
        factory = source_factory or ReflectorSource
        self.sources = [factory(url, self._on_change) for url in (urls or REFLECTOR_STREAM_URLS)]
        self.state = merge_states([source.state for source in self.sources])

    def ensure_started(self):
        with self.condition:
            if self.started:
                return
            self.started = True
        for source in self.sources:
            source.start()

    def snapshot(self):
        with self.condition:
            return self.sequence, self.state

    def wait(self, after, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.sequence > after, timeout=timeout)
            return self.sequence, self.state

    def _on_change(self):
        with self.condition:
            state = merge_states([source.state for source in self.sources])
            if state == self.state:
                return
            self.state = state
            self.sequence += 1
            self.condition.notify_all()


class ReflectorSource:
    def __init__(self, url, on_change):
        self.url = url
        self.on_change = on_change
        self.state = {
            "ok": False,
            "connected": False,
            "error": "Ansluter till reflektorströmmen.",
            "node_count": 0,
            "talkgroups": [],
            "updated_at": None,
        }
        self.document = {}
        self.generation = None

    def start(self):
        threading.Thread(target=self._loop, name="reflector-reader", daemon=True).start()

    def _publish(self, state):
        if state == self.state:
            return
        self.state = state
        self.on_change()

    def _handle(self, event_name, payload):
        if not isinstance(payload, dict) or payload.get("topic") != "status":
            return
        current = self.state
        if event_name == "source":
            source = payload.get("source")
            connected = isinstance(source, dict) and source.get("connected") is True
            if connected and current["ok"]:
                error = None
            elif isinstance(source, dict) and source.get("error"):
                error = source["error"]
            else:
                error = "Väntar på statusdata från reflektorn." if connected else "Reflektorströmmen är frånkopplad."
            self._publish({**current, "connected": connected, "error": error})
            return
        if event_name != "patch":
            return
        generation = payload.get("gen")
        if self.generation is not None and generation != self.generation:
            self.document = {}
        self.generation = generation
        try:
            apply_patch(self.document, payload.get("patch"))
            summary = summarize_nodes(self.document.get("nodes"))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            logging.warning("Ogiltig statuspatch från reflektorn: %s", exc)
            self._publish({**current, "connected": True, "error": f"Kunde inte tolka reflektorstatus: {exc}"})
            return
        self._publish({
            "ok": True,
            "connected": True,
            "error": None,
            **summary,
            "updated_at": payload.get("ts"),
        })

    def _loop(self):
        retry_delay = 1
        while True:
            self.document = {}
            self.generation = None
            body = json.dumps(
                {"id": str(uuid.uuid4()), "seq": 1, "topics": {"status": 1}}, separators=(",", ":")
            ).encode("utf-8")
            request = urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                    "Origin": "https://reflector-sm.svxlink.org",
                    "Referer": "https://reflector-sm.svxlink.org/nodes",
                    "User-Agent": "SvxLinkNodePortal/1.0",
                },
                method="POST",
            )
            opened_at = None
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    if response.status != 200 or "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                        raise RuntimeError("Reflektorströmmen returnerade ett oväntat svar.")
                    opened_at = time.monotonic()
                    event_name, data_lines, size = "message", [], 0
                    while True:
                        raw = response.readline(MAX_SSE_BYTES + 1)
                        if not raw:
                            raise ConnectionError("Reflektorströmmen stängdes.")
                        if len(raw) > MAX_SSE_BYTES:
                            raise ValueError("En SSE-rad från reflektorn överskred 5 MB.")
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            if data_lines and event_name in ("source", "patch"):
                                self._handle(event_name, json.loads("\n".join(data_lines)))
                            event_name, data_lines, size = "message", [], 0
                        elif line.startswith(":"):
                            continue
                        elif line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            size += len(line)
                            if size > MAX_SSE_BYTES:
                                raise ValueError("En SSE-händelse från reflektorn överskred 5 MB.")
            except Exception as exc:
                logging.warning("Reflektorströmmen tappades: %s", exc)
                if opened_at is not None and time.monotonic() - opened_at >= 30:
                    retry_delay = 1
                self._publish({
                    **self.state,
                    "connected": False,
                    "error": f"Reflektorströmmen är tillfälligt otillgänglig: {exc}",
                })
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)


REFLECTOR_HUB = ReflectorHub()
