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

from portal.server import (
    PortalHandler,
    claim_agent_commands,
    database,
    provision_node,
    validate_dtmf_command,
    validate_status,
)


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
                    detail_page = response.read().decode("utf-8")
                self.assertIn("Radiostatus", detail_page)
                self.assertIn("/config`", detail_page)
                self.assertNotIn("editor-svxlink", detail_page)

                dashboard_page_request = Request(
                    f"{base_url}/",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(dashboard_page_request, timeout=2) as response:
                    dashboard_page = response.read().decode("utf-8")
                self.assertIn("/config\" aria-label=\"Redigera konfiguration för", dashboard_page)
                self.assertIn("title=\"Redigera konfiguration\"", dashboard_page)

                config_page_request = Request(
                    f"{base_url}/nodes/{node['node_id']}/config",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(config_page_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    config_page = response.read().decode("utf-8")
                self.assertIn("Läser filen från noden", config_page)
                self.assertIn("readConfig('svxlink.conf', false)", config_page)
                self.assertIn("readConfig('node_info.json', false)", config_page)
                self.assertIn("editor-svxlink", config_page)

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


class PortalDtmfTests(unittest.TestCase):
    def test_validates_dtmf_sequence_characters_and_length(self):
        self.assertEqual(
            validate_dtmf_command({"action": "dtmf", "sequence": "*12AB#"}),
            {"action": "dtmf", "sequence": "*12AB#"},
        )
        for sequence in ("", "12a", "12;reboot", "1" * 65):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                validate_dtmf_command({"action": "dtmf", "sequence": sequence})

    def test_single_and_bulk_dtmf_commands_through_agent_queue(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            online_node = provision_node("Online node")
            offline_node = provision_node("Offline node")
            with database() as connection:
                connection.execute(
                    "UPDATE nodes SET last_seen = ? WHERE id = ?",
                    ("2999-01-01T00:00:00+00:00", online_node["node_id"]),
                )
                connection.execute(
                    "UPDATE nodes SET last_seen = ? WHERE id = ?",
                    ("2000-01-01T00:00:00+00:00", offline_node["node_id"]),
                )
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                credentials = base64.b64encode(
                    b"admin:this-is-a-long-test-password"
                ).decode()
                admin_headers = {"Authorization": f"Basic {credentials}"}
                agent_headers = {
                    "Authorization": f"Bearer {online_node['token']}"
                }

                with urlopen(
                    Request(
                        f"{base_url}/nodes/{online_node['node_id']}",
                        headers=admin_headers,
                    ),
                    timeout=2,
                ) as response:
                    detail_page = response.read().decode("utf-8")
                self.assertIn("DTMF-styrning", detail_page)

                with urlopen(
                    Request(f"{base_url}/dtmf", headers=admin_headers),
                    timeout=2,
                ) as response:
                    bulk_page = response.read().decode("utf-8")
                self.assertIn("Välj noder", bulk_page)
                self.assertIn("/api/dtmf-commands", bulk_page)

                single_request = Request(
                    f"{base_url}/api/nodes/{online_node['node_id']}/dtmf-commands",
                    data=json.dumps({"sequence": "*123#"}).encode("utf-8"),
                    headers={**admin_headers, "Content-Type": "application/json"},
                )
                with urlopen(single_request, timeout=2) as response:
                    single_command_id = json.loads(
                        response.read().decode("utf-8")
                    )["command_id"]

                with urlopen(
                    Request(
                        f"{base_url}/api/v1/agent/commands",
                        headers=agent_headers,
                    ),
                    timeout=2,
                ) as response:
                    commands = json.loads(response.read().decode("utf-8"))["commands"]
                self.assertEqual(commands[0]["id"], single_command_id)
                self.assertEqual(commands[0]["action"], "dtmf")
                self.assertEqual(commands[0]["sequence"], "*123#")

                result_request = Request(
                    f"{base_url}/api/v1/agent/commands/{single_command_id}/result",
                    data=json.dumps({"ok": True}).encode("utf-8"),
                    headers={**agent_headers, "Content-Type": "application/json"},
                )
                with urlopen(result_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(
                    Request(
                        f"{base_url}/api/commands/{single_command_id}",
                        headers=admin_headers,
                    ),
                    timeout=2,
                ) as response:
                    command_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(command_result["status"], "completed")
                self.assertEqual(command_result["result"], {"ok": True})

                bulk_request = Request(
                    f"{base_url}/api/dtmf-commands",
                    data=json.dumps(
                        {
                            "node_ids": [
                                online_node["node_id"],
                                offline_node["node_id"],
                                "f" * 32,
                            ],
                            "sequence": "9#",
                        }
                    ).encode("utf-8"),
                    headers={**admin_headers, "Content-Type": "application/json"},
                )
                with urlopen(bulk_request, timeout=2) as response:
                    bulk_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(
                    [result["status"] for result in bulk_result["results"]],
                    ["pending", "failed", "failed"],
                )
                self.assertEqual(
                    bulk_result["results"][1]["error"],
                    "Noden är offline. Anslut den innan du skickar kommandon.",
                )
                self.assertEqual(
                    bulk_result["results"][2]["error"], "Noden hittades inte."
                )
                bulk_command_id = bulk_result["results"][0]["command_id"]
                with urlopen(
                    Request(
                        f"{base_url}/api/v1/agent/commands",
                        headers=agent_headers,
                    ),
                    timeout=2,
                ) as response:
                    bulk_commands = json.loads(response.read().decode("utf-8"))["commands"]
                self.assertEqual(bulk_commands[0]["id"], bulk_command_id)
                self.assertEqual(bulk_commands[0]["sequence"], "9#")
                with database() as connection:
                    connection.execute(
                        "UPDATE agent_commands SET updated_at = ? WHERE id = ?",
                        ("2000-01-01T00:00:00+00:00", bulk_command_id),
                    )
                self.assertEqual(claim_agent_commands(online_node["node_id"]), [])
                with database() as connection:
                    retried = connection.execute(
                        "SELECT status FROM agent_commands WHERE id = ?",
                        (bulk_command_id,),
                    ).fetchone()
                self.assertEqual(retried["status"], "expired")

                invalid_request = Request(
                    f"{base_url}/api/nodes/{online_node['node_id']}/dtmf-commands",
                    data=json.dumps({"sequence": "12;reboot"}).encode("utf-8"),
                    headers={**admin_headers, "Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as invalid_dtmf:
                    urlopen(invalid_request, timeout=2)
                self.assertEqual(invalid_dtmf.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_global_dtmf_buttons_are_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/dtmf-buttons"
                credentials = base64.b64encode(
                    b"admin:this-is-a-long-test-password"
                ).decode()
                headers = {
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/json",
                }

                with self.assertRaises(HTTPError) as unauthenticated:
                    urlopen(url, timeout=2)
                self.assertEqual(unauthenticated.exception.code, 401)

                with urlopen(Request(url, headers=headers), timeout=2) as response:
                    self.assertEqual(json.loads(response.read())["buttons"], [])

                buttons = [
                    {"label": " TG 240 ", "sequence": "91240#"},
                    {"label": "Av", "sequence": "*0#"},
                ]
                save = Request(
                    url, data=json.dumps({"buttons": buttons}).encode(), headers=headers
                )
                with urlopen(save, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(Request(url, headers=headers), timeout=2) as response:
                    self.assertEqual(
                        json.loads(response.read())["buttons"],
                        [
                            {"label": "TG 240", "sequence": "91240#"},
                            {"label": "Av", "sequence": "*0#"},
                        ],
                    )

                invalid_payloads = [
                    {"buttons": [{"label": "", "sequence": "1"}]},
                    {"buttons": [{"label": "x" * 41, "sequence": "1"}]},
                    {"buttons": [{"label": "Bad", "sequence": "12;rm"}]},
                    {"buttons": [{"label": "Extra", "sequence": "1", "x": 1}]},
                    {"buttons": [{"label": str(i), "sequence": "1"} for i in range(31)]},
                ]
                for payload in invalid_payloads:
                    request = Request(
                        url, data=json.dumps(payload).encode(), headers=headers
                    )
                    with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=2)
                    self.assertEqual(error.exception.code, 400)

                with urlopen(Request(url, headers=headers), timeout=2) as response:
                    self.assertEqual(len(json.loads(response.read())["buttons"]), 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
