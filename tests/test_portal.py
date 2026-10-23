import base64
import hashlib
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

from portal.server import PortalHandler, database, provision_node, validate_status


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
                "logs": ["SvxLink started"],
            }
        )
        self.assertEqual(status["current_tg"], "240")
        self.assertEqual(status["radio"]["mode"], "tx")
        self.assertEqual(status["logs"], ["SvxLink started"])

    def test_rejects_too_many_or_oversized_log_lines(self):
        base_status = {"active": True, "service_status": "active"}
        with self.assertRaisesRegex(ValueError, "högst 50 loggrader"):
            validate_status({**base_status, "logs": ["line"] * 51})
        with self.assertRaisesRegex(ValueError, "högst 512 byte"):
            validate_status({**base_status, "logs": ["å" * 257]})
        with self.assertRaisesRegex(ValueError, "giltig Unicode"):
            validate_status({**base_status, "logs": ["\ud800"]})

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
                    "logs": ["SvxLink started", "Selecting TG 240"],
                }
                request = Request(
                    f"{base_url}/api/v1/agent/status",
                    data=json.dumps(report).encode("utf-8"),
                    headers={"Authorization": f"Bearer {node['token']}"},
                )
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)

                credentials = base64.b64encode(b"admin:this-is-a-long-test-password").decode()
                with self.assertRaises(HTTPError) as unauthenticated_agent:
                    urlopen(
                        Request(f"{base_url}/api/v1/agent/commands"),
                        timeout=2,
                    )
                self.assertEqual(unauthenticated_agent.exception.code, 401)

                unauthorized_config_request = Request(
                    f"{base_url}/api/nodes/{node['node_id']}/config-commands",
                    data=json.dumps(
                        {"action": "read", "document": "svxlink.conf"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as unauthenticated_admin:
                    urlopen(unauthorized_config_request, timeout=2)
                self.assertEqual(unauthenticated_admin.exception.code, 401)

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
                self.assertEqual(
                    detail["status"]["logs"],
                    ["SvxLink started", "Selecting TG 240"],
                )

                page_request = Request(
                    f"{base_url}/nodes/{node['node_id']}",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(page_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("Radiostatus", response.read().decode("utf-8"))

                read_command_request = Request(
                    f"{base_url}/api/nodes/{node['node_id']}/config-commands",
                    data=json.dumps(
                        {"action": "read", "document": "svxlink.conf"}
                    ).encode("utf-8"),
                    headers={
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(read_command_request, timeout=2) as response:
                    command_id = json.loads(response.read().decode("utf-8"))["command_id"]

                agent_headers = {"Authorization": f"Bearer {node['token']}"}
                with urlopen(
                    Request(f"{base_url}/api/v1/agent/commands", headers=agent_headers),
                    timeout=2,
                ) as response:
                    commands = json.loads(response.read().decode("utf-8"))["commands"]
                self.assertEqual(commands[0]["id"], command_id)
                self.assertEqual(commands[0]["document"], "svxlink.conf")

                config_content = "[GLOBAL]\nLOGICS=RepeaterLogic\n"
                read_result = {
                    "ok": True,
                    "document": "svxlink.conf",
                    "content": config_content,
                    "sha256": hashlib.sha256(config_content.encode("utf-8")).hexdigest(),
                }
                result_request = Request(
                    f"{base_url}/api/v1/agent/commands/{command_id}/result",
                    data=json.dumps(read_result).encode("utf-8"),
                    headers={**agent_headers, "Content-Type": "application/json"},
                )
                with urlopen(result_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)

                config_result_request = Request(
                    f"{base_url}/api/config-commands/{command_id}",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(config_result_request, timeout=2) as response:
                    config_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(config_result["status"], "completed")
                self.assertEqual(config_result["result"]["content"], config_content)

                bad_config_request = Request(
                    f"{base_url}/api/nodes/{node['node_id']}/config-commands",
                    data=json.dumps(
                        {
                            "action": "write",
                            "document": "node_info.json",
                            "content": "{invalid",
                            "sha256": "0" * 64,
                        }
                    ).encode("utf-8"),
                    headers={
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/json",
                    },
                )
                with self.assertRaises(HTTPError) as invalid_config:
                    urlopen(bad_config_request, timeout=2)
                self.assertEqual(invalid_config.exception.code, 400)

                with database() as connection:
                    connection.execute(
                        "UPDATE nodes SET last_seen = ? WHERE id = ?",
                        ("2000-01-01T00:00:00+00:00", node["node_id"]),
                    )
                with self.assertRaises(HTTPError) as offline_node:
                    urlopen(read_command_request, timeout=2)
                self.assertEqual(offline_node.exception.code, 409)

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
