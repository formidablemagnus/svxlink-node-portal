import array
import base64
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent import audio
from portal.server import AudioHub, PortalHandler, provision_node, validate_udp_audio
from tests.test_portal import session_header


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sections(rx_port=10000, tx_port=10001):
    return {
        "GLOBAL": {"LOGICS": "SimplexLogic"},
        "SimplexLogic": {"TYPE": "Simplex", "RX": "Rx1", "TX": "Tx1"},
        "Rx1": {
            "AUDIO_DEV": f"udp:127.0.0.1:{rx_port}", "CARD_SAMPLE_RATE": "48000",
            "SQL_DET": "PTY", "SQL_PTY": "/dev/shm/ptt",
        },
        "Tx1": {"AUDIO_DEV": f"udp:127.0.0.1:{tx_port}", "CARD_SAMPLE_RATE": "48000"},
    }


class UdpConfigTests(unittest.TestCase):
    def test_parse_udp_device(self):
        self.assertEqual(audio.parse_udp_device("udp:10.1.1.29:10000"), ("10.1.1.29", 10000))
        self.assertEqual(audio.parse_udp_device('"udp:10001"'), ("", 10001))
        self.assertIsNone(audio.parse_udp_device("alsa:default"))
        self.assertIsNone(audio.parse_udp_device("udp:host:notaport"))

    def test_describe_reports_capabilities(self):
        info = audio.describe(sections())
        self.assertTrue(info["listen"] and info["talk"] and info["ptt"])
        plain = {"GLOBAL": {"LOGICS": "L"}, "L": {"TYPE": "Simplex", "RX": "Rx1", "TX": "Tx1"},
                 "Rx1": {"AUDIO_DEV": "alsa:default"}, "Tx1": {"AUDIO_DEV": "alsa:default"}}
        info = audio.describe(plain)
        self.assertFalse(info["listen"] or info["talk"] or info["ptt"])

    def test_card_settings_fall_back_to_global_section(self):
        data = sections()
        for name in ("Rx1", "Tx1"):
            del data[name]["CARD_SAMPLE_RATE"]
        data["GLOBAL"].update({"CARD_SAMPLE_RATE": "48000", "CARD_CHANNELS": "1"})
        config = audio.find_udp_audio(data)
        self.assertEqual((config["tx"]["rate"], config["tx"]["channels"]), (48000, 1))
        self.assertEqual((config["rx"]["rate"], config["rx"]["channels"]), (48000, 1))
        del data["GLOBAL"]["CARD_SAMPLE_RATE"], data["GLOBAL"]["CARD_CHANNELS"]
        config = audio.find_udp_audio(data)
        self.assertEqual((config["tx"]["rate"], config["tx"]["channels"]), (16000, 2))

    def test_voter_receivers_are_expanded(self):
        data = sections()
        data["SimplexLogic"]["RX"] = "Voter"
        data["Voter"] = {"TYPE": "Voter", "RECEIVERS": "Rx1"}
        self.assertEqual(audio.find_udp_audio(data)["rx"]["name"], "Rx1")

    def test_resampling_round_trip_lengths(self):
        wire = array.array("h", [1000] * 320).tobytes()
        node = audio.wire_to_node(wire, 2, 48000)
        self.assertEqual(len(node), 960 * 2 * 2)
        back = audio.node_to_wire(node, 2, 48000)
        self.assertEqual(len(back), len(wire))
        self.assertEqual(array.array("h", back)[10], 1000)

    def test_non_integer_rate_is_resampled(self):
        wire = array.array("h", range(160)).tobytes()
        node = audio.wire_to_node(wire, 1, 8000)
        self.assertEqual(len(node), 80 * 2)


class AudioBridgeTests(unittest.TestCase):
    def test_bridge_relays_audio_both_ways_and_controls_ptt(self):
        rx_port, tx_port = free_udp_port(), free_udp_port()
        svxlink_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        svxlink_rx.bind(("127.0.0.1", rx_port))
        svxlink_rx.settimeout(3)
        self.addCleanup(svxlink_rx.close)
        writes = []
        bridge = audio.AudioBridge("unused", lambda _: sections(rx_port, tx_port))
        replies = []
        mic = array.array("h", [500] * 640).tobytes()
        uploads = []

        def exchange(payload):
            uploads.append(payload)
            index = len(uploads)
            if index == 1:
                svxlink_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                svxlink_tx.sendto(bytes(960 * 4), ("127.0.0.1", tx_port))
                svxlink_tx.close()
                return bytes([3]) + mic
            if index < 5:
                return bytes([1])
            replies.append(index)
            return bytes([0])

        with patch.object(audio.PttControl, "_write", lambda self, command: writes.append(command)):
            thread = threading.Thread(target=bridge.run, args=(exchange,))
            thread.start()
            frame, _ = svxlink_rx.recvfrom(65535)
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(frame), 960 * 2 * 2)
        self.assertEqual(writes, [b"O", b"Z"])
        self.assertTrue(any(len(item) == 320 * 2 for item in uploads))

    def test_missing_udp_devices_end_session_with_error(self):
        bridge = audio.AudioBridge("unused", lambda _: {"GLOBAL": {}})
        bridge.run(lambda payload: b"\x01")
        self.assertIn("udp:", bridge.last_error)


class AudioHubTests(unittest.TestCase):
    def test_listener_audio_fanout(self):
        hub = AudioHub()
        sequence = hub.subscribe("n")
        hub.add_audio("n", bytes(7000))
        new_sequence, chunks = hub.wait("n", sequence, 0.1)
        self.assertGreater(new_sequence, sequence)
        self.assertEqual(sum(len(chunk) for chunk in chunks), 7000)
        self.assertTrue(hub.wanted("n"))
        hub.unsubscribe("n")
        self.assertFalse(hub.wanted("n"))

    def test_only_one_user_can_transmit(self):
        hub = AudioHub()
        self.assertEqual(hub.push_mic("n", "anna", True, b"\x01\x00"), (True, True, "anna"))
        self.assertEqual(hub.push_mic("n", "anna", True, b"\x01\x00"), (True, False, "anna"))
        self.assertEqual(hub.push_mic("n", "bo", True, b"\x01\x00"), (False, False, "anna"))
        flags, mic = hub.take_for_agent("n")
        self.assertEqual(flags, 3)
        self.assertEqual(len(mic), 4)
        self.assertEqual(hub.push_mic("n", "anna", False, b""), (True, False, None))
        self.assertEqual(hub.take_for_agent("n"), (0, b""))

    def test_ptt_expires_without_heartbeat(self):
        hub = AudioHub()
        hub.push_mic("n", "anna", True, b"")
        time.sleep(0.05)
        with patch("portal.server.PTT_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(hub.take_for_agent("n"), (0, b""))

    def test_validate_udp_audio(self):
        self.assertEqual(validate_udp_audio(None)["listen"], False)
        with self.assertRaises(ValueError):
            validate_udp_audio({"listen": "yes"})


class UdpTransportTests(unittest.TestCase):
    def test_udp_exchange_relays_audio_and_rejects_bad_token(self):
        from portal.audio_udp import AudioUdpServer
        from portal.server import node_audio_key, touch_node

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3")},
        ):
            node = provision_node("UDP node")
            hub = AudioHub()
            server = AudioUdpServer("127.0.0.1", 0, hub, node_audio_key, touch_node)
            server.start()
            address = ("127.0.0.1", server.port)
            good = audio.UdpExchange(node["node_id"], node["token"], address)
            bad = audio.UdpExchange(node["node_id"], "x" * 40, address)
            bad.REPLY_TIMEOUT = 0.1
            try:
                self.assertIsNone(bad.exchange(b"\x01\x00"))
                self.assertEqual(hub.wait(node["node_id"], 0, 0)[1], [])

                hub.subscribe(node["node_id"])
                reply = good.exchange(b"\x05\x00" * 10)
                self.assertEqual(reply, b"\x01")
                self.assertEqual(hub.wait(node["node_id"], 0, 0)[1], [b"\x05\x00" * 10])

                hub.push_mic(node["node_id"], "admin", True, b"\x02\x00" * 5)
                reply = good.exchange(b"")
                self.assertEqual(reply, bytes([3]) + b"\x02\x00" * 5)
            finally:
                good.close()
                bad.close()
                server.close()


class PortalAudioEndpointTests(unittest.TestCase):
    def test_audio_flows_between_agent_and_browser(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Audio node")
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                credentials = base64.b64encode(b"admin:this-is-a-long-test-password").decode()
                browser = session_header(base_url, credentials)
                agent = {"Authorization": f"Bearer {node['token']}"}

                def send(path, data, headers, content_type):
                    request = Request(
                        f"{base_url}{path}", data=data,
                        headers={**headers, "Content-Type": content_type},
                    )
                    with urlopen(request, timeout=3) as response:
                        return response.read()

                def agent_status(udp_audio):
                    body = {"active": True, "service_status": "active", "udp_audio": udp_audio}
                    send("/api/v1/agent/status", json.dumps(body).encode(), agent,
                         "application/json")

                def mic(flag, pcm=b""):
                    return send(f"/api/nodes/{node['node_id']}/audio-mic", bytes([flag]) + pcm,
                                browser, "application/octet-stream")

                agent_status({"listen": False, "talk": False})
                with self.assertRaises(HTTPError) as no_audio:
                    urlopen(Request(f"{base_url}/api/nodes/{node['node_id']}/audio",
                                    headers=browser), timeout=3)
                self.assertEqual(no_audio.exception.code, 409)
                with self.assertRaises(HTTPError) as no_mic:
                    mic(1, b"\x01\x00")
                self.assertEqual(no_mic.exception.code, 409)

                agent_status({"listen": True, "talk": True, "ptt": True})
                stream = urlopen(Request(f"{base_url}/api/nodes/{node['node_id']}/audio",
                                         headers=browser), timeout=5)
                try:
                    with urlopen(Request(f"{base_url}/api/v1/agent/commands", headers=agent),
                                 timeout=3) as response:
                        self.assertTrue(json.loads(response.read())["audio"])
                    reply = send("/api/v1/agent/audio", bytes(640), agent,
                                 "application/octet-stream")
                    self.assertEqual(reply, b"\x01")
                    length = int.from_bytes(stream.read(2), "big")
                    while length == 0:
                        length = int.from_bytes(stream.read(2), "big")
                    self.assertEqual(len(stream.read(length)), 640)

                    mic(1, b"\x05\x00" * 10)
                    reply = send("/api/v1/agent/audio", b"", agent, "application/octet-stream")
                    self.assertEqual(reply[0], 3)
                    self.assertEqual(reply[1:], b"\x05\x00" * 10)
                    mic(0)
                    reply = send("/api/v1/agent/audio", b"", agent, "application/octet-stream")
                    self.assertEqual(reply, b"\x01")
                finally:
                    stream.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
