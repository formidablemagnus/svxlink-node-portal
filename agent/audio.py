"""UDP-ljudbrygga mellan SvxLink och portalen för fjärrdrift.

SvxLink kan använda ``AUDIO_DEV=udp:<adress>:<port>`` för både [Rx] och [Tx].
När sådana enheter finns i svxlink.conf kan portalen lyssna på noden och sända
via den. Ljudet går över portalens HTTP-anslutning som 16 kHz mono PCM16.
PTT styrs via SvxLinks squelch-PTY (``SQL_DET=PTY`` och ``SQL_PTY``).
"""
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

WIRE_RATE = 16000
FRAME_MS = 20
DEFAULT_CARD_RATE = 16000
UDP_CHANNELS = 2
MAX_PAYLOAD_BYTES = 48 * 1024
EXCHANGE_SECONDS = 0.04
UDP_EXCHANGE_SECONDS = 0.02
MAX_CONSECUTIVE_FAILURES = 5
MAX_PTT_SECONDS = 180
PTT_DRAIN_SECONDS = 0.3
UPLINK_QUEUE_FRAMES = 25
UPLINK_SILENCE_TAIL_SECONDS = 4.0
SQUELCH_OPEN = b"O"
SQUELCH_CLOSE = b"Z"
LOCAL_FALLBACK_HOST = "127.0.0.1"


def _unquote(value):
    return value.strip().strip("\"'")


def parse_udp_device(value):
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


def _expand(sections, names, member_key):
    result = []
    for name in names:
        section = sections.get(name, {})
        members = section.get(member_key)
        if members and _unquote(section.get("TYPE", "")).lower() in ("voter", "multi"):
            result.extend(_unquote(item) for item in members.split(",") if item.strip())
        else:
            result.append(name)
    return result


def _card_setting(sections, section, key, default, low, high):
    for source in (section, sections.get("GLOBAL", {})):
        try:
            value = int(_unquote(source.get(key, "")))
        except ValueError:
            continue
        if low <= value <= high:
            return value
    return default


def _card_rate(sections, section):
    return _card_setting(sections, section, "CARD_SAMPLE_RATE", DEFAULT_CARD_RATE, 8000, 96000)


def _card_channels(sections, section):
    return _card_setting(sections, section, "CARD_CHANNELS", UDP_CHANNELS, 1, 2)


def find_udp_audio(sections):
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

    def first_udp(names, member_key):
        for name in _expand(sections, names, member_key):
            section = sections.get(name, {})
            target = parse_udp_device(section.get("AUDIO_DEV", ""))
            if target:
                return name, section, target
        return None

    config = {"rx": None, "tx": None, "ptt_path": "", "issues": []}
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


def describe(sections):
    """Capabilities reported to the portal in the status payload."""
    config = find_udp_audio(sections)
    return {
        "listen": config["tx"] is not None,
        "talk": config["rx"] is not None,
        "ptt": bool(config["ptt_path"]),
        "sample_rate": WIRE_RATE,
        "issues": config["issues"][:5],
        "error": "",
    }


def _read_samples(data, channels):
    width = 2 * channels
    samples = array.array("h")
    samples.frombytes(bytes(data[: len(data) // width * width]))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _resample(samples, source_rate, target_rate):
    if source_rate == target_rate or not samples:
        return samples
    if source_rate % target_rate == 0:
        factor = source_rate // target_rate
        groups = zip(*(samples[offset::factor] for offset in range(factor)))
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


def node_to_wire(data, channels, rate):
    mono = _read_samples(data, channels)[0::channels]
    wire = _resample(mono, rate, WIRE_RATE)
    if sys.byteorder == "big":
        wire.byteswap()
    return wire.tobytes()


def wire_to_node(data, channels, rate):
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

    def __init__(self, path):
        self.path = path
        self.active = False
        self.started = 0.0
        self.blocked = False

    def _write(self, command):
        descriptor = os.open(self.path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            mode = os.fstat(descriptor).st_mode
            if not (stat.S_ISCHR(mode) or stat.S_ISFIFO(mode)):
                raise ValueError(f"SQL_PTY är inte en PTY eller FIFO: {self.path}")
            os.write(descriptor, command)
        finally:
            os.close(descriptor)

    def set(self, wanted):
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

    def release(self):
        if not self.active:
            return
        self.active = False
        try:
            self._write(SQUELCH_CLOSE)
        except (OSError, ValueError) as exc:
            logging.warning("Kunde inte släppa PTT via %s: %s", self.path, exc)


class UdpExchange:
    """Audio exchange with the portal over UDP; mirrors portal/audio_udp.py."""

    MAGIC = b"SVA1"
    MAC_BYTES = 16
    REPLY_TIMEOUT = 0.25
    MAX_REPLY_BYTES = 4096
    HOLD_TIMEOUTS = 50

    def __init__(self, node_id, token, address):
        self.node_id = node_id.encode("ascii")
        self.key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")
        self.address = address
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.last_flags = 0
        self.timeouts = 0
        self.answered = False

    def close(self):
        self.sock.close()

    def _mac(self, direction, head, body):
        return hmac.new(self.key, direction + head + body, hashlib.sha256).digest()[:self.MAC_BYTES]

    def exchange(self, payload):
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
            parts = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if parts:
                        break
                    raise socket.timeout()
                self.sock.settimeout(remaining)
                try:
                    data, _ = self.sock.recvfrom(self.MAX_REPLY_BYTES)
                except socket.timeout:
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
                if not body[0] & 4:
                    break
            self.timeouts = 0
            self.answered = True
            self.last_flags = parts[0][0] & 3
            return bytes([self.last_flags]) + b"".join(part[1:] for part in parts)
        except socket.timeout:
            self.timeouts += 1
        except OSError as exc:
            logging.debug("UDP-ljudutbyte misslyckades: %s", exc)
            self.timeouts += 1
        if not self.answered or self.timeouts > self.HOLD_TIMEOUTS:
            return None
        # A lost reply must not drop PTT, so repeat the last flags without microphone audio.
        return bytes([self.last_flags])


class AudioBridge:
    def __init__(self, config_path, read_sections, channels=None):
        self.config_path = config_path
        self.read_sections = read_sections
        self.channels = channels
        self.exchange_seconds = EXCHANGE_SECONDS
        self.last_error = ""
        self.mic_frames = 0
        self.on_activity = None
        self.running = threading.Event()

    def _start_sockets(self, config):
        listener = None
        sender = None
        try:
            tx = config["tx"]
            if tx:
                listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    listener.bind((tx["host"] or "0.0.0.0", tx["port"]))
                except OSError as exc:
                    raise OSError(
                        f"Kan inte ta emot ljud på UDP {tx['host'] or '0.0.0.0'}:{tx['port']} "
                        f"från [{tx['name']}]. Ange en lokal adress, till exempel "
                        f"udp:{LOCAL_FALLBACK_HOST}:{tx['port']}. ({exc})"
                    ) from exc
            if config["rx"]:
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            for sock in (listener, sender):
                if sock:
                    sock.close()
            raise
        return listener, sender

    def _collect(self, listener, tx):
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
            except socket.timeout:
                break
            except OSError:
                time.sleep(remaining)
                break
            wire = node_to_wire(packet, channels, tx_rate)
            if size + len(wire) <= MAX_PAYLOAD_BYTES:
                chunks.append(wire)
                size += len(wire)
        return b"".join(chunks)

    def _uplink_sender(self, sender, address, frames, stop, silence=b""):
        # SvxLinks VOX stänger bara när det kommer tystnad; utan paket står
        # ljudenheten still och squelchen blir kvar öppen. Därför skickas
        # tystnad en stund efter sista mikrofonramen.
        next_time = time.monotonic()
        last_real = 0.0
        while not stop.is_set():
            try:
                frame = frames.get(timeout=0.005 if silence else 0.1)
                last_real = time.monotonic()
            except queue.Empty:
                now = time.monotonic()
                if silence and last_real and now - last_real < UPLINK_SILENCE_TAIL_SECONDS:
                    frame = silence
                else:
                    next_time = now
                    continue
            now = time.monotonic()
            if next_time > now:
                time.sleep(next_time - now)
            else:
                next_time = now
            try:
                sender.sendto(frame, address)
            except OSError as exc:
                logging.warning("UDP-sändningsfel mot SvxLink: %s", exc)
            next_time += FRAME_MS / 1000

    def run(self, exchange):
        """Run one session. ``exchange(bytes)`` returns the portal reply or None on failure."""
        self.running.set()
        try:
            self._run(exchange)
        finally:
            self.running.clear()

    def _run(self, exchange):
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
        frames = queue.Queue(maxsize=UPLINK_QUEUE_FRAMES)
        stop = threading.Event()
        thread = None
        rx_channels = (self.channels or rx["channels"]) if rx else UDP_CHANNELS
        frame_bytes = (rx["rate"] * FRAME_MS // 1000 * rx_channels * 2) if rx else 0
        if rx:
            host = rx["host"]
            if host in ("", "0.0.0.0"):
                host = LOCAL_FALLBACK_HOST
            thread = threading.Thread(
                target=self._uplink_sender,
                args=(sender, (host, rx["port"]), frames, stop, bytes(frame_bytes)),
                name="audio-uplink", daemon=True,
            )
            thread.start()
        failures = 0
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
                    if mic:
                        converted = wire_to_node(mic, rx_channels, rx["rate"])
                        for offset in range(0, len(converted), frame_bytes):
                            frame = converted[offset:offset + frame_bytes]
                            if len(frame) < frame_bytes:
                                frame += bytes(frame_bytes - len(frame))
                            self._push(frames, frame)
                            self.mic_frames += 1
                            if self.on_activity and self.mic_frames % 25 == 1:
                                self.on_activity()
                    self._safe_ptt(ptt, True)
                elif ptt:
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
    def _push(frames, frame):
        try:
            frames.put_nowait(frame)
        except queue.Full:
            try:
                frames.get_nowait()
            except queue.Empty:
                pass
            try:
                frames.put_nowait(frame)
            except queue.Full:
                pass

    def _safe_ptt(self, ptt, wanted):
        try:
            ptt.set(wanted)
            if self.last_error.startswith("PTT"):
                self.last_error = ""
        except (OSError, ValueError) as exc:
            ptt.blocked = True
            self.last_error = f"PTT misslyckades via {ptt.path}: {exc}"
            logging.warning("%s", self.last_error)
