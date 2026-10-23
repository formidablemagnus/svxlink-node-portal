import base64
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from http.server import ThreadingHTTPServer

from portal.server import PortalHandler, provision_node, validate_status


class PortalStatusTests(unittest.TestCase):
    def test_accepts_and_normalizes_status(self):
        status = validate_status(
            {
                "active": True,
                "service_status": "active",
                "current_tg": 240,
                "logic": "RepeaterLogic",
                "callsign": "SA7TEST",
                "radio": {"mode": "tx", "signal_level": None},
            }
        )
        self.assertEqual(status["current_tg"], "240")
        self.assertEqual(status["radio"]["mode"], "tx")

    def test_rejects_invalid_radio_mode(self):
        with self.assertRaisesRegex(ValueError, "radio.mode"):
            validate_status({"active": False, "service_status": "inactive", "radio": {"mode": "scan"}})

    def test_rejects_out_of_range_signal_level(self):
        with self.assertRaisesRegex(ValueError, "signal_level"):
            validate_status(
                {
                    "active": True,
                    "service_status": "active",
                    "radio": {"mode": "rx", "signal_level": 101},
                }
            )

    def test_rejects_unexpected_radio_mode_type(self):
        with self.assertRaisesRegex(ValueError, "radio.mode"):
            validate_status(
                {"active": True, "service_status": "active", "radio": {"mode": []}}
            )

    def test_agent_report_updates_authenticated_dashboard_api(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Test node")
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                report = {
                    "active": True,
                    "service_status": "active",
                    "current_tg": "240",
                    "logic": "RepeaterLogic",
                    "callsign": "SA7TEST",
                    "radio": {"mode": "rx", "signal_level": 55},
                }
                request = Request(
                    f"{base_url}/api/v1/agent/status",
                    data=json.dumps(report).encode("utf-8"),
                    headers={"Authorization": f"Bearer {node['token']}"},
                )
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)

                credentials = base64.b64encode(b"admin:this-is-a-long-test-password").decode()
                dashboard_request = Request(
                    f"{base_url}/api/nodes",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(dashboard_request, timeout=2) as response:
                    dashboard = json.loads(response.read().decode("utf-8"))
                self.assertEqual(dashboard["nodes"][0]["name"], "Test node")
                self.assertTrue(dashboard["nodes"][0]["online"])
                self.assertEqual(dashboard["nodes"][0]["status"]["radio"]["mode"], "rx")

                detail_request = Request(
                    f"{base_url}/api/nodes/{node['node_id']}",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(detail_request, timeout=2) as response:
                    detail = json.loads(response.read().decode("utf-8"))["node"]
                self.assertEqual(detail["id"], node["node_id"])
                self.assertEqual(detail["status"]["callsign"], "SA7TEST")
                self.assertEqual(detail["status"]["current_tg"], "240")

                page_request = Request(
                    f"{base_url}/nodes/{node['node_id']}",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(page_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("Radiostatus", response.read().decode("utf-8"))

                with self.assertRaises(HTTPError) as missing_node:
                    urlopen(
                        Request(
                            f"{base_url}/api/nodes/missing-node",
                            headers={"Authorization": f"Basic {credentials}"},
                        ),
                        timeout=2,
                    )
                self.assertEqual(missing_node.exception.code, 404)

                with self.assertRaises(HTTPError) as admin_error:
                    urlopen(f"{base_url}/api/nodes", timeout=2)
                self.assertEqual(admin_error.exception.code, 401)

                bad_token_request = Request(
                    f"{base_url}/api/v1/agent/status",
                    data=json.dumps(report).encode("utf-8"),
                    headers={"Authorization": "Bearer invalid-token"},
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(bad_token_request, timeout=2)
                self.assertEqual(error.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
