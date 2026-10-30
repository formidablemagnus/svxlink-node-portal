"""UDP-ljudbrygga mellan SvxLink och portalen för fjärrdrift.

SvxLink kan använda ``AUDIO_DEV=udp:<adress>:<port>`` för både [Rx] och [Tx].
När sådana enheter finns i svxlink.conf kan portalen lyssna på noden och sända
via den. Ljudet går över portalens HTTP-anslutning som 16 kHz mono PCM16.
PTT styrs via SvxLinks squelch-PTY (``SQL_DET=PTY`` och ``SQL_PTY``).
"""
from __future__ import annotations

import array
import hashlib
import hmac
import logging
import os
import queue
import socket
import stat
import struct
import sys
import threading
import time
from contextlib import suppress
from collections.abc import Callable, Mapping, Sequence
from typing import Any

WIRE_RATE = 16000
FRAME_MS = 20
WIRE_FRAME_BYTES = WIRE_RATE * FRAME_MS // 1000 * 2
DEFAULT_CARD_RATE = 16000
UDP_CHANNELS = 2
MAX_PAYLOAD_BYTES = 48 * 1024
EXCHANGE_SECONDS = 0.04
UDP_EXCHANGE_SECONDS: float = 0.02
MAX_CONSECUTIVE_FAILURES = 5
MAX_REPLY_DATAGRAMS = 4
MAX_PTT_SECONDS = 180
PTT_DRAIN_SECONDS = 0.3
PTT_RELEASE_ATTEMPTS = 3
PTT_RELEASE_RETRY_SECONDS = 0.05
UPLINK_QUEUE_FRAMES = 40
UPLINK_PREBUFFER_FRAMES = 8
UPLINK_PREBUFFER_MAX_SECONDS = 0.3
UPLINK_SILENCE_TAIL_SECONDS = 4.0
SQUELCH_OPEN = b"O"
SQUELCH_CLOSE = b"Z"
LOCAL_FALLBACK_HOST = "127.0.0.1"


def _unquote(value: str) -> str:
    return value.strip().strip("\"'")


def parse_udp_device(value: str) -> tuple[str, int] | None:
    """Return (host, port) for an ``udp:`` AUDIO_DEV value, otherwise None."""
    value = _unquote(value or "")
    if not value.lower().startswith("udp:"):
        return None
    parts = value[4:].split(":")
    try:
        if len(parts) == 1:
            host, port = "", int(parts[0])
        elif len(parts) == 2:
            host, port = parts[0].strip(), int(parts[1])
        else:
            return None
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    return host, port


def _expand(sections: Mapping[str, Mapping[str, str]], names: Sequence[str], member_key: str) -> list[str]:
    result: list[str] = []
    for name in names:
        section = sections.get(name, {})
        members = section.get(member_key)
        if members and _unquote(section.get("TYPE", "")).lower() in ("voter", "multi"):
            result.extend(_unquote(item) for item in members.split(",") if item.strip())
        else:
            result.append(name)
    return result


def _card_setting(sections: Mapping[str, Mapping[str, str]], section: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    for source in (section, sections.get("GLOBAL", {})):
        try:
            value = int(_unquote(source.get(key, "")))
        except ValueError:
            continue
        if low <= value <= high:
            return value
    return default


def _card_rate(sections: Mapping[str, Mapping[str, str]], section: Mapping[str, str]) -> int:
    return _card_setting(sections, section, "CARD_SAMPLE_RATE", DEFAULT_CARD_RATE, 8000, 96000)


def _card_channels(sections: Mapping[str, Mapping[str, str]], section: Mapping[str, str]) -> int:
    return _card_setting(sections, section, "CARD_CHANNELS", UDP_CHANNELS, 1, 2)


def find_udp_audio(sections: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """Find the UDP audio devices used by the active logics in svxlink.conf."""
    rx_names, tx_names = [], []
    logic_names = [
        _unquote(name)
        for name in sections.get("GLOBAL", {}).get("LOGICS", "").split(",")
        if name.strip()
    ]
    for name in logic_names:
        logic = sections.get(name, {})
        if _unquote(logic.get("TYPE", "")).lower() not in ("simplex", "repeater"):
            continue
        if logic.get("RX"):
            rx_names.append(_unquote(logic["RX"]))
        if logic.get("TX"):
            tx_names.append(_unquote(logic["TX"]))

    def first_udp(names: Sequence[str], member_key: str) -> tuple[str, Mapping[str, str], tuple[str, int]] | None:
        for name in _expand(sections, names, member_key):
            section = sections.get(name, {})
            target = parse_udp_device(section.get("AUDIO_DEV", ""))
            if target:
                return name, section, target
        return None

    config: dict[str, Any] = {"rx": None, "tx": None, "ptt_path": "", "issues": []}
    rx = first_udp(rx_names, "RECEIVERS")
    if rx:
        name, section, (host, port) = rx
        config["rx"] = {"name": name, "host": host, "port": port,
                        "rate": _card_rate(sections, section),
                        "channels": _card_channels(sections, section)}
        if _unquote(section.get("SQL_DET", "")).upper() == "PTY":
            config["ptt_path"] = _unquote(section.get("SQL_PTY", ""))
            if not config["ptt_path"]:
                config["issues"].append(f"SQL_PTY saknas i [{name}].")
    tx = first_udp(tx_names, "TRANSMITTERS")
    if tx:
        name, section, (host, port) = tx
        config["tx"] = {"name": name, "host": host, "port": port,
                        "rate": _card_rate(sections, section),
                        "channels": _card_channels(sections, section)}
    return config


def describe(sections: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """Capabilities reported to the portal in the status payload."""
    config = find_udp_audio(sections)
    return {
        "listen": config["tx"] is not None,
        "talk": config["rx"] is not None,
        "ptt": bool(config["ptt_path"]),
        "sample_rate": WIRE_RATE,
        "rx_rate": config["rx"]["rate"] if config["rx"] else 0,
        "rx_channels": config["rx"]["channels"] if config["rx"] else 0,
        "issues": config["issues"][:5],
        "error": "",
    }


def _read_samples(data: bytes, channels: int) -> array.array[int]:
    width = 2 * channels
    samples = array.array("h")
    samples.frombytes(bytes(data[: len(data) // width * width]))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _resample(samples: array.array[int], source_rate: int, target_rate: int) -> array.array[int]:
    if source_rate == target_rate or not samples:
        return samples
    if source_rate % target_rate == 0:
        factor = source_rate // target_rate
        groups = zip(*(samples[offset::factor] for offset in range(factor)), strict=False)
        return array.array("h", [sum(group) // factor for group in groups])
    last = len(samples) - 1
    step = source_rate / target_rate
    output = array.array("h")
    for index in range(len(samples) * target_rate // source_rate):
        position = index * step
        base = int(position)
        fraction = position - base
        first = samples[min(base, last)]
        second = samples[min(base + 1, last)]
        output.append(int(first + (second - first) * fraction))
    return output


def node_to_wire(data: bytes, channels: int, rate: int) -> bytes:
    mono = _read_samples(data, channels)[0::channels]
    wire = _resample(mono, rate, WIRE_RATE)
    if sys.byteorder == "big":
        wire.byteswap()
    return wire.tobytes()


def wire_to_node(data: bytes, channels: int, rate: int) -> bytes:
    mono = _read_samples(data, 1)
    resampled = _resample(mono, WIRE_RATE, rate)
    if channels > 1:
        interleaved = array.array("h", [0]) * (len(resampled) * channels)
        for channel in range(channels):
            interleaved[channel::channels] = resampled
        resampled = interleaved
    if sys.byteorder == "big":
        resampled.byteswap()
    return resampled.tobytes()


class PttControl:
    """Opens and closes SvxLink's squelch through its PTY."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.active = False
        self.started = 0.0
        self.blocked = False

    def _write(self, command: bytes) -> None:
        descriptor = os.open(self.path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            mode = os.fstat(descriptor).st_mode
            if not (stat.S_ISCHR(mode) or stat.S_ISFIFO(mode)):
                raise ValueError(f"SQL_PTY är inte en PTY eller FIFO: {self.path}")
            os.write(descriptor, command)
        finally:
            os.close(descriptor)

    def set(self, wanted: bool) -> None:
        if not self.path:
            return
        if wanted and self.active and time.monotonic() - self.started > MAX_PTT_SECONDS:
            logging.warning("PTT släpptes efter %s sekunders sändning.", MAX_PTT_SECONDS)
            self.release()
            self.blocked = True
        if not wanted:
            self.blocked = False
        if wanted and not self.active and not self.blocked:
            self._write(SQUELCH_OPEN)
            self.active = True
            self.started = time.monotonic()
        elif not wanted and self.active:
            self.release()

    def release(self, force: bool = False) -> None:
        """Close the squelch; ``active`` is only cleared once SvxLink accepted the close."""
        if not self.path or not (self.active or force):
            return
        error: Exception | None = None
        for attempt in range(PTT_RELEASE_ATTEMPTS):
            try:
                self._write(SQUELCH_CLOSE)
            except (OSError, ValueError) as exc:
                error = exc
                if attempt + 1 < PTT_RELEASE_ATTEMPTS:
                    time.sleep(PTT_RELEASE_RETRY_SECONDS)
            else:
                self.active = False
                return
        logging.warning("Kunde inte släppa PTT via %s: %s", self.path, error)


class UdpExchange:
    """Audio exchange with the portal over UDP; mirrors portal/audio_udp.py."""

    MAGIC = b"SVA1"
    MAC_BYTES = 16
    REPLY_TIMEOUT = 0.25
    MAX_REPLY_BYTES = 4096
    HOLD_TIMEOUTS = 12

    def __init__(self, node_id: str, token: str, address: tuple[str, int]) -> None:
        self.node_id = node_id.encode("ascii")
        self.key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")
        self.address = address
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.REPLY_TIMEOUT)
        self.seq = 0
        self.last_flags = 0
        self.timeouts = 0
        self.answered = False

    def close(self) -> None:
        self.sock.close()

    def _mac(self, direction: bytes, head: bytes, body: bytes) -> bytes:
        return hmac.new(self.key, direction + head + body, hashlib.sha256).digest()[:self.MAC_BYTES]

    def exchange(self, payload: bytes) -> bytes | None:
        """Return the portal reply, ``b""`` style flags on a lost reply, or None when unreachable."""
        self.seq += 1
        ts = int(time.time())
        id_head = bytes([len(self.node_id)]) + self.node_id
        head = id_head + struct.pack(">II", ts, self.seq)
        packet = self.MAGIC + head + self._mac(b"Q", head, payload) + payload
        expected = struct.pack(">II", ts, self.seq)
        try:
            self.sock.sendto(packet, self.address)
            deadline = time.monotonic() + self.REPLY_TIMEOUT
            parts: list[bytes] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if parts:
                        break
                    raise TimeoutError()
                self.sock.settimeout(remaining)
                try:
                    data, _ = self.sock.recvfrom(self.MAX_REPLY_BYTES)
                except TimeoutError:
                    if parts:
                        break
                    raise
                prefix = len(self.MAGIC)
                if not data.startswith(self.MAGIC) or data[prefix:prefix + 8] != expected:
                    continue
                mac = data[prefix + 8:prefix + 8 + self.MAC_BYTES]
                body = data[prefix + 8 + self.MAC_BYTES:]
                if not body or not hmac.compare_digest(mac, self._mac(b"R", expected, body)):
                    continue
                parts.append(body)
                if len(parts) >= MAX_REPLY_DATAGRAMS and body[0] & 4:
                    return None
                if not body[0] & 4:
                    break
            self.timeouts = 0
            self.answered = True
            self.last_flags = parts[0][0] & 3
            return bytes([self.last_flags]) + b"".join(part[1:] for part in parts)
        except TimeoutError:
            self.timeouts += 1
        except OSError as exc:
            logging.debug("UDP-ljudutbyte misslyckades: %s", exc)
            self.timeouts += 1
        if not self.answered or self.timeouts > self.HOLD_TIMEOUTS:
            return None
        # A lost reply must not drop PTT, so repeat the last flags without microphone audio.
        return bytes([self.last_flags])


class AudioBridge:
    def __init__(self, config_path: str, read_sections: Callable[[str], Mapping[str, Mapping[str, str]]], channels: int | None=None) -> None:
        self.config_path = config_path
        self.read_sections = read_sections
        self.channels = channels
        self.exchange_seconds = EXCHANGE_SECONDS
        self.last_error = ""
        self.mic_frames = 0
        self.on_activity: Callable[[], None] | None = None
        self.running = threading.Event()

    def _start_sockets(self, config: Mapping[str, Any]) -> tuple[socket.socket | None, socket.socket | None]:
        listener = None
        sender = None
        try:
            tx = config["tx"]
            if tx:
                listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                bind_host = tx["host"]
                if not isinstance(bind_host, str) or not isinstance(tx["port"], int) or not 1 <= tx["port"] <= 65535:
                    raise ValueError("Invalid local UDP audio address.")
                # Empty AUDIO_DEV host intentionally binds all interfaces; set a host to restrict the listener.
                try:
                    bind_address = socket.getaddrinfo(
                        bind_host, tx["port"], socket.AF_INET, socket.SOCK_DGRAM, 0, socket.AI_PASSIVE
                    )[0][4]
                    listener.bind(bind_address)
                except OSError as exc:
                    raise OSError(
                        f"Kan inte ta emot ljud på UDP {tx['host'] or socket.inet_ntoa(bytes(4))}:{tx['port']} "
                        f"från [{tx['name']}]. Ange en lokal adress, till exempel "
                        f"udp:{LOCAL_FALLBACK_HOST}:{tx['port']}. ({exc})"
                    ) from exc
            if config["rx"]:
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sender.settimeout(1.0)
        except BaseException:
            for sock in (listener, sender):
                if sock:
                    sock.close()
            raise
        return listener, sender

    def _collect(self, listener: socket.socket | None, tx: Mapping[str, Any] | None) -> bytes:
        deadline = time.monotonic() + self.exchange_seconds
        channels = self.channels or (tx["channels"] if tx else UDP_CHANNELS)
        tx_rate = tx["rate"] if tx else WIRE_RATE
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if listener is None:
                time.sleep(remaining)
                break
            listener.settimeout(remaining)
            try:
                packet, _ = listener.recvfrom(65535)
            except TimeoutError:
                break
            except OSError:
                time.sleep(remaining)
                break
            wire = node_to_wire(packet, channels, tx_rate)
            if size + len(wire) <= MAX_PAYLOAD_BYTES:
                chunks.append(wire)
                size += len(wire)
        return b"".join(chunks)

    def _uplink_sender(self, sender: socket.socket, address: tuple[str, int], frames: queue.Queue[bytes], stop: threading.Event, silence: bytes=b"") -> None:
        # Jitterbuffert: webbläsaren levererar ljud i skurar, så uppspelningen
        # startar först när några ramar finns i kö. Efter sista mikrofonramen
        # skickas tystnad en stund så att SvxLinks VOX kan stänga squelchen.
        frame_seconds = FRAME_MS / 1000
        next_time = time.monotonic()
        last_real = 0.0
        playing = False
        waiting_since: float | None = None
        while not stop.is_set():
            now = time.monotonic()
            if not playing:
                size = frames.qsize()
                if size and waiting_since is None:
                    waiting_since = now
                elif not size:
                    waiting_since = None
                if size and (size >= UPLINK_PREBUFFER_FRAMES
                             or (waiting_since is not None and now - waiting_since >= UPLINK_PREBUFFER_MAX_SECONDS)):
                    playing = True
                    next_time = now
                else:
                    in_tail = silence and last_real and now - last_real < UPLINK_SILENCE_TAIL_SECONDS
                    if in_tail and now >= next_time:
                        try:
                            sender.sendto(silence, address)
                        except OSError as exc:
                            logging.warning("UDP-sändningsfel mot SvxLink: %s", exc)
                        next_time = max(next_time + frame_seconds, now - frame_seconds)
                    else:
                        if not in_tail:
                            next_time = now
                        stop.wait(0.005)
                    continue
            try:
                frame = frames.get_nowait()
            except queue.Empty:
                playing = False
                waiting_since = None
                continue
            last_real = now
            if next_time > now:
                time.sleep(next_time - now)
            else:
                next_time = now
            try:
                sender.sendto(frame, address)
            except OSError as exc:
                logging.warning("UDP-sändningsfel mot SvxLink: %s", exc)
            next_time += frame_seconds

    def run(self, exchange: Callable[[bytes], bytes | None]) -> None:
        """Run one session. ``exchange(bytes)`` returns the portal reply or None on failure."""
        self.running.set()
        try:
            self._run(exchange)
        finally:
            self.running.clear()

    def _run(self, exchange: Callable[[bytes], bytes | None]) -> None:
        try:
            config = find_udp_audio(self.read_sections(self.config_path))
            if not config["rx"] and not config["tx"]:
                raise OSError("Inga udp:-ljudenheter hittades i svxlink.conf.")
            listener, sender = self._start_sockets(config)
        except (OSError, UnicodeError) as exc:
            self.last_error = str(exc)
            logging.warning("Kunde inte starta ljudbryggan: %s", exc)
            return
        self.last_error = ""
        rx, tx = config["rx"], config["tx"]
        ptt = PttControl(config["ptt_path"]) if rx else None
        if ptt:
            # Stäng en squelch som hängt kvar från en tidigare session eller en omstartad agent.
            ptt.release(force=True)
        frames: queue.Queue[bytes] = queue.Queue(maxsize=UPLINK_QUEUE_FRAMES)
        stop = threading.Event()
        thread: threading.Thread | None = None
        rx_channels = (self.channels or rx["channels"]) if rx else UDP_CHANNELS
        frame_bytes = (rx["rate"] * FRAME_MS // 1000 * rx_channels * 2) if rx else 0
        if rx and sender is not None:
            host = rx["host"]
            if host in ("", socket.inet_ntoa(bytes(4))):
                host = LOCAL_FALLBACK_HOST
            thread = threading.Thread(
                target=self._uplink_sender,
                args=(sender, (host, rx["port"]), frames, stop, bytes(frame_bytes)),
                name="audio-uplink", daemon=True,
            )
            try:
                thread.start()
            except BaseException:
                stop.set()
                for sock in (listener, sender):
                    if sock:
                        sock.close()
                raise
        failures = 0
        wire_rest = b""
        logging.info("Ljudbrygga startad.")
        try:
            while True:
                payload = self._collect(listener, tx)
                reply = exchange(payload)
                if reply is None:
                    failures += 1
                    if ptt:
                        self._safe_ptt(ptt, False)
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        break
                    continue
                failures = 0
                if not reply or not reply[0] & 1:
                    break
                wanted = bool(reply[0] & 2) and rx is not None
                mic = reply[1:]
                if wanted:
                    # Bara hela 20 ms-ramar omvandlas; resten sparas till nästa utbyte
                    # så att inga tystnadsluckor fylls in mitt i talet.
                    wire_rest += mic
                    usable = len(wire_rest) // WIRE_FRAME_BYTES * WIRE_FRAME_BYTES
                    if usable:
                        converted = wire_to_node(wire_rest[:usable], rx_channels, rx["rate"])
                        wire_rest = wire_rest[usable:]
                        for offset in range(0, len(converted) - frame_bytes + 1, frame_bytes):
                            self._push(frames, converted[offset:offset + frame_bytes])
                            self.mic_frames += 1
                            if self.on_activity and self.mic_frames % 25 == 1:
                                self.on_activity()
                    if ptt:
                        self._safe_ptt(ptt, True)
                elif ptt:
                    wire_rest = b""
                    deadline = time.monotonic() + PTT_DRAIN_SECONDS
                    while not frames.empty() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self._safe_ptt(ptt, False)
        finally:
            if ptt:
                self._safe_ptt(ptt, False)
                ptt.release()
            stop.set()
            if thread:
                thread.join(timeout=1)
            for sock in (listener, sender):
                if sock:
                    sock.close()
            logging.info("Ljudbrygga stoppad.")

    @staticmethod
    def _push(frames: queue.Queue[bytes], frame: bytes) -> None:
        try:
            frames.put_nowait(frame)
        except queue.Full:
            with suppress(queue.Empty):
                frames.get_nowait()
            with suppress(queue.Full):
                frames.put_nowait(frame)

    def _safe_ptt(self, ptt: PttControl, wanted: bool) -> None:
        try:
            ptt.set(wanted)
            if self.last_error.startswith("PTT"):
                self.last_error = ""
        except (OSError, ValueError) as exc:
            ptt.blocked = True
            self.last_error = f"PTT misslyckades via {ptt.path}: {exc}"
            logging.warning("%s", self.last_error)
