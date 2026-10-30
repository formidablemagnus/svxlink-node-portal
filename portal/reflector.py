"""Live reflector status stream shared by all browser viewers."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from collections.abc import Callable, Mapping, Sequence
from typing import Any
import logging
import os
import threading
import time
import urllib.request
import uuid

DEFAULT_STREAM_URLS: str = (
    "https://svxreflector-fernebo.sk3w.se/api/stream,"
    "https://reflector-sm.svxlink.org/api/stream"
)
# Kommaseparerad lista; varje reflektor har en egen nodmängd som summeras.
def _validate_stream_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Invalid reflector stream URL; expected HTTP or HTTPS.")
    return url


REFLECTOR_STREAM_URLS: list[str] = [
    _validate_stream_url(url.strip())
    for url in os.environ.get("SVXLINK_REFLECTOR_STREAM_URL", DEFAULT_STREAM_URLS).split(",")
    if url.strip()
]
MAX_SSE_BYTES: int = 5 * 1024 * 1024


def summarize_nodes(nodes: Any) -> dict[str, Any]:
    if not isinstance(nodes, dict):
        raise ValueError("Reflektorströmmen saknar ett giltigt nodes-objekt.")
    by_tg: dict[int, dict[str, int]] = {}
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


def _parent(document: Any, pointer: Any) -> tuple[Any, str]:
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


def apply_patch(document: Any, operations: Any) -> Any:
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


def merge_states(states: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum node counts and talkgroups from several reflector sources."""
    groups: dict[int, dict[str, int]] = {}
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

    def __init__(self, urls: Sequence[str] | None=None, source_factory: Callable[[str, Callable[[], None]], ReflectorSource] | None=None) -> None:
        self.condition = threading.Condition()
        self.sequence: int = 0
        self.started: bool = False
        factory = source_factory or ReflectorSource
        self.sources: list[ReflectorSource] = [factory(_validate_stream_url(url), self._on_change) for url in (urls or REFLECTOR_STREAM_URLS)]
        self.state: dict[str, Any] = merge_states([source.state for source in self.sources])

    def ensure_started(self) -> None:
        with self.condition:
            if self.started:
                return
            self.started = True
        for source in self.sources:
            source.start()

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        with self.condition:
            return self.sequence, self.state

    def wait(self, after: int, timeout: float | None) -> tuple[int, dict[str, Any]]:
        with self.condition:
            self.condition.wait_for(lambda: self.sequence > after, timeout=timeout)
            return self.sequence, self.state

    def close(self) -> None:
        for source in self.sources:
            source.close()

    def _on_change(self) -> None:
        with self.condition:
            state = merge_states([source.state for source in self.sources])
            if state == self.state:
                return
            self.state = state
            self.sequence += 1
            self.condition.notify_all()


class ReflectorSource:
    def __init__(self, url: str, on_change: Callable[[], None]) -> None:
        self.url = _validate_stream_url(url)
        self.on_change = on_change
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state: dict[str, Any] = {
            "ok": False,
            "connected": False,
            "error": "Ansluter till reflektorströmmen.",
            "node_count": 0,
            "talkgroups": [],
            "updated_at": None,
        }
        self.document: dict[str, Any] = {}
        self.generation: Any = None

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, name="reflector-reader", daemon=True)
            self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)

    def _publish(self, state: dict[str, Any]) -> None:
        if state == self.state:
            return
        self.state = state
        self.on_change()

    def _handle(self, event_name: str, payload: Any) -> None:
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
            if len(json.dumps(self.document, separators=(",", ":")).encode("utf-8")) > MAX_SSE_BYTES:
                raise ValueError("Reflector status exceeded the size limit.")
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

    def _loop(self) -> None:
        retry_delay = 1
        while not self.stop_event.is_set():
            self.document = {}
            self.generation = None
            body = json.dumps(
                {"id": str(uuid.uuid4()), "seq": 1, "topics": {"status": 1}}, separators=(",", ":")
            ).encode("utf-8")
            request = urllib.request.Request(  # noqa: S310 - URL validated at configuration load
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
                opener = urllib.request.build_opener(
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()
                )
                with opener.open(request, timeout=45) as response:
                    if response.status != 200 or "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                        raise RuntimeError("Reflektorströmmen returnerade ett oväntat svar.")
                    opened_at = time.monotonic()
                    event_name: str = "message"
                    data_lines: list[str] = []
                    size = 0
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
                if self.stop_event.wait(retry_delay):
                    break
                retry_delay = min(retry_delay * 2, 30)


REFLECTOR_HUB = ReflectorHub()
