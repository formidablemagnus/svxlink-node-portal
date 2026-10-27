import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from http.server import ThreadingHTTPServer

from portal.server import (
    PortalHandler,
    _login_failures,
    claim_agent_commands,
    complete_agent_command,
    create_backup,
    database,
    enqueue_agent_command,
    hash_password,
    provision_node,
    restore_backup,
    take_agent_command_result,
    validate_dtmf_command,
    validate_status,
)


def login_cookie(base_url, username, password):
    request = Request(
        f"{base_url}/api/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=5) as response:
        return response.headers["Set-Cookie"].split(";", 1)[0]


def session_header(base_url, basic_credentials):
    username, password = base64.b64decode(basic_credentials).decode("utf-8").split(":", 1)
    return {"Cookie": login_cookie(base_url, username, password)}


def fetch_asset(base_url, headers, name):
    with urlopen(Request(f"{base_url}/assets/{name}", headers=headers), timeout=2) as response:
        return response.read().decode("utf-8")


class PortalBackupTests(unittest.TestCase):
    def test_backup_restores_into_new_installation(self):
        password = "this-is-a-long-test-password"
        env = {"PORTAL_ADMIN_USER": "admin", "PORTAL_ADMIN_PASSWORD": password}
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {**env, "PORTAL_DATABASE": str(Path(directory) / "a.sqlite3")}):
                credentials = provision_node("Node A")
                with database() as connection:
                    connection.execute(
                        "INSERT INTO service_log (node_id, created_at, actor, entry) "
                        "VALUES (?, ?, ?, ?)",
                        (credentials["node_id"], "2025-01-02T03:04:05+00:00",
                         "admin", "Testad service"),
                    )
                backup = create_backup("admin")
            with patch.dict(os.environ, {**env, "PORTAL_DATABASE": str(Path(directory) / "b.sqlite3")}):
                with self.assertRaises(ValueError):
                    restore_backup("admin", {"format": "other"})
                counts = restore_backup("admin", json.loads(json.dumps(backup)))
                self.assertEqual(counts["nodes"], 1)
                self.assertEqual(counts["service_log"], 1)
                with database() as connection:
                    row = connection.execute("SELECT id, name FROM nodes").fetchone()
                    token_hash = hashlib.sha256(credentials["token"].encode()).hexdigest()
                    found = connection.execute(
                        "SELECT 1 FROM nodes WHERE token_hash = ?", (token_hash,)
                    ).fetchone()
                    service_entry = connection.execute(
                        "SELECT node_id, actor, entry FROM service_log"
                    ).fetchone()
                self.assertEqual(row["id"], credentials["node_id"])
                self.assertIsNotNone(found)
                self.assertEqual(
                    tuple(service_entry),
                    (credentials["node_id"], "admin", "Testad service"),
                )
                old_backup = json.loads(json.dumps(backup))
                del old_backup["tables"]["service_log"]
                restore_backup("admin", old_backup)
                with database() as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM service_log").fetchone()[0],
                        0,
                    )


class PortalServiceLogTests(unittest.TestCase):
    def test_manual_entries_record_user_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Service log node")
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                endpoint = f"/api/nodes/{node['node_id']}/service-log"
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(f"{base}{endpoint}", timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)

                admin_cookie = login_cookie(base, "admin", "this-is-a-long-test-password")
                with urlopen(
                    Request(f"{base}/nodes/{node['node_id']}/service-log",
                            headers={"Cookie": admin_cookie}),
                    timeout=2,
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("Servicelogg", response.read().decode("utf-8"))
                self.assertIn("loadEntries", fetch_asset(
                    base, {"Cookie": admin_cookie}, "service-log.js"
                ))

                def call(path, body, cookie):
                    request = Request(
                        f"{base}{path}",
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Cookie": cookie, "Content-Type": "application/json"},
                    )
                    with urlopen(request, timeout=2) as response:
                        return response.status, json.loads(response.read())

                status, admin_entry = call(endpoint, {"entry": "  Service utförd  "}, admin_cookie)
                self.assertEqual(status, 201)
                self.assertEqual(admin_entry["entry"]["actor"], "admin")
                self.assertEqual(admin_entry["entry"]["entry"], "Service utförd")
                self.assertIsNotNone(datetime.fromisoformat(admin_entry["entry"]["created_at"]))

                with self.assertRaises(HTTPError) as invalid:
                    call(endpoint, {"entry": "  "}, admin_cookie)
                self.assertEqual(invalid.exception.code, 400)

                operator_password = "Operator-password-1"
                call("/api/users", {
                    "username": "operator", "password": operator_password, "role": "operator",
                }, admin_cookie)
                operator_cookie = login_cookie(base, "operator", operator_password)
                _, operator_entry = call(endpoint, {"entry": "Kontrollerat radion"}, operator_cookie)
                self.assertEqual(operator_entry["entry"]["actor"], "operator")

                with urlopen(Request(f"{base}{endpoint}", headers={"Cookie": admin_cookie}),
                             timeout=2) as response:
                    entries = json.loads(response.read())["entries"]
                self.assertEqual([entry["actor"] for entry in entries], ["operator", "admin"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class PortalStatusTests(unittest.TestCase):
    def test_admin_can_create_node_from_dashboard_api(self):
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
                base_url = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(
                        Request(
                            f"{base_url}/api/nodes",
                            data=json.dumps({"name": "New node"}).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        ),
                        timeout=2,
                    )
                self.assertEqual(unauthorized.exception.code, 401)

                credentials_header = base64.b64encode(
                    b"admin:this-is-a-long-test-password"
                ).decode()
                headers = {
                    **session_header(base_url, credentials_header),
                    "Content-Type": "application/json",
                }
                with urlopen(
                    Request(f"{base_url}/", headers=headers), timeout=2
                ) as response:
                    dashboard = response.read().decode("utf-8")
                self.assertIn('id="create-node-form"', dashboard)
                self.assertIn('id="created-node-token"', dashboard)

                invalid_request = Request(
                    f"{base_url}/api/nodes",
                    data=json.dumps({"name": "  "}).encode("utf-8"),
                    headers=headers,
                )
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(invalid_request, timeout=2)
                self.assertEqual(invalid.exception.code, 400)

                invalid_unicode_request = Request(
                    f"{base_url}/api/nodes",
                    data=b'{"name":"\\ud800"}',
                    headers=headers,
                )
                with self.assertRaises(HTTPError) as invalid_unicode:
                    urlopen(invalid_unicode_request, timeout=2)
                self.assertEqual(invalid_unicode.exception.code, 400)

                create_request = Request(
                    f"{base_url}/api/nodes",
                    data=json.dumps({"name": "Repeater north"}).encode("utf-8"),
                    headers=headers,
                )
                with urlopen(create_request, timeout=2) as response:
                    self.assertEqual(response.status, 201)
                    credentials = json.loads(response.read().decode("utf-8"))

                with urlopen(Request(f"{base_url}/api/nodes", headers=headers), timeout=2) as response:
                    self.assertTrue(json.loads(response.read().decode("utf-8"))["can_delete_nodes"])
                self.assertEqual(credentials["name"], "Repeater north")
                self.assertEqual(len(credentials["node_id"]), 32)
                self.assertTrue(credentials["token"])
                with database() as connection:
                    row = connection.execute(
                        "SELECT token_hash FROM nodes WHERE id = ?",
                        (credentials["node_id"],),
                    ).fetchone()
                self.assertEqual(
                    row["token_hash"],
                    hashlib.sha256(credentials["token"].encode("utf-8")).hexdigest(),
                )
                self.assertNotEqual(row["token_hash"], credentials["token"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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
                    headers={**session_header(base_url, credentials)},
                )
                with urlopen(dashboard_request, timeout=2) as response:
                    dashboard = json.loads(response.read().decode("utf-8"))
                self.assertEqual(dashboard["nodes"][0]["name"], "Test node")
                self.assertTrue(dashboard["nodes"][0]["online"])
                self.assertEqual(dashboard["nodes"][0]["status"]["radio"]["mode"], "rx")

                detail_request = Request(
                    f"{base_url}/api/nodes/{node['node_id']}",
                    headers={**session_header(base_url, credentials)},
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
                    headers={**session_header(base_url, credentials)},
                )
                with urlopen(page_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    detail_page = response.read().decode("utf-8")
                self.assertIn("Radiostatus", detail_page)
                self.assertIn(
                    "/config`",
                    fetch_asset(base_url, {**session_header(base_url, credentials)}, "node.js"),
                )
                self.assertNotIn("editor-svxlink", detail_page)

                dashboard_page_request = Request(
                    f"{base_url}/",
                    headers={**session_header(base_url, credentials)},
                )
                with urlopen(dashboard_page_request, timeout=2) as response:
                    dashboard_page = response.read().decode("utf-8")
                dashboard_script = fetch_asset(
                    base_url, {**session_header(base_url, credentials)}, "index.js"
                )
                self.assertIn("/config\" aria-label=\"Redigera konfiguration för", dashboard_script)
                self.assertIn("title=\"Redigera konfiguration\"", dashboard_script)

                config_page_request = Request(
                    f"{base_url}/nodes/{node['node_id']}/config",
                    headers={**session_header(base_url, credentials)},
                )
                with urlopen(config_page_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    config_page = response.read().decode("utf-8")
                self.assertIn("Läser filen från noden", config_page)
                self.assertNotIn("editor-node-info", config_page)
                nodeinfo_page_request = Request(
                    f"{base_url}/nodes/{node['node_id']}/nodeinfo",
                    headers={**session_header(base_url, credentials)},
                )
                with urlopen(nodeinfo_page_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    nodeinfo_page = response.read().decode("utf-8")
                self.assertIn("editor-node-info", nodeinfo_page)
                self.assertNotIn("editor-svxlink", nodeinfo_page)
                config_script = fetch_asset(
                    base_url, {**session_header(base_url, credentials)}, "config.js"
                )
                self.assertIn("readConfig(name, false)", config_script)
                self.assertIn("'node_info.json': 'editor-node-info'", config_script)
                self.assertIn("editor-svxlink", config_page)

                read_command_request = Request(
                    f"{base_url}/api/nodes/{node['node_id']}/config-commands",
                    data=json.dumps(
                        {"action": "read", "document": "svxlink.conf"}
                    ).encode("utf-8"),
                    headers={
                        **session_header(base_url, credentials),
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
                    headers={**session_header(base_url, credentials)},
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
                        **session_header(base_url, credentials),
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
                            headers={**session_header(base_url, credentials)},
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


class PortalRadioStreamTests(unittest.TestCase):
    def test_agent_radio_events_reach_sse_viewer_and_enable_watch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Radio node")
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                credentials = base64.b64encode(b"admin:this-is-a-long-test-password").decode()
                agent_headers = {
                    "Authorization": f"Bearer {node['token']}",
                    "Content-Type": "application/json",
                }

                def agent_get_commands():
                    with urlopen(
                        Request(f"{base_url}/api/v1/agent/commands", headers=agent_headers),
                        timeout=2,
                    ) as response:
                        return json.loads(response.read())

                def agent_post_radio(events):
                    request = Request(
                        f"{base_url}/api/v1/agent/radio",
                        data=json.dumps({"events": events}).encode("utf-8"),
                        headers=agent_headers,
                    )
                    with urlopen(request, timeout=2) as response:
                        return json.loads(response.read())

                self.assertFalse(agent_get_commands()["watch"])
                stream = urlopen(
                    Request(
                        f"{base_url}/api/nodes/{node['node_id']}/radio-events",
                        headers={**session_header(base_url, credentials)},
                    ),
                    timeout=5,
                )
                try:
                    self.assertTrue(agent_get_commands()["watch"])
                    event = {
                        "t": 1700000000000,
                        "mode": "rx",
                        "signal_level": 55,
                        "receivers": [{"name": "Rx1", "receiving": True, "siglev": 55}],
                        "transmitters": [],
                    }
                    self.assertTrue(agent_post_radio([event])["watch"])
                    received = None
                    while received is None:
                        line = stream.readline().decode("utf-8")
                        self.assertNotEqual(line, "")
                        if line.startswith("data: "):
                            received = json.loads(line[6:])
                    self.assertEqual(received["mode"], "rx")
                    self.assertEqual(received["receivers"][0]["siglev"], 55)
                finally:
                    stream.close()

                with self.assertRaises(HTTPError) as invalid:
                    agent_post_radio([{"t": 1, "mode": "bogus"}])
                self.assertEqual(invalid.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class PortalLiveDataTests(unittest.TestCase):
    def test_streamed_logs_and_radio_are_merged_into_node_status(self):
        from portal import server as portal_server

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Live node")
            node_id = node["node_id"]
            portal_server._last_touch.clear()
            with portal_server.database() as connection:
                connection.execute(
                    "UPDATE nodes SET status_json = ? WHERE id = ?",
                    (json.dumps({"logs": ["old"], "radio": {"mode": "idle"}}), node_id),
                )
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                headers = {
                    "Authorization": "Bearer " + node["token"],
                    "Content-Type": "application/json",
                }

                def post(path, body):
                    request = Request(
                        f"{base_url}{path}", data=json.dumps(body).encode("utf-8"),
                        headers=headers,
                    )
                    return urlopen(request, timeout=2)

                post("/api/v1/agent/logs", {"lines": ["a" * 1000, "b"]}).close()
                post("/api/v1/agent/radio", {"events": [{
                    "t": 1, "mode": "rx", "signal_level": 10,
                    "receivers": [], "transmitters": [],
                }]}).close()
                with self.assertRaises(HTTPError) as invalid:
                    post("/api/v1/agent/logs", {"lines": [1]})
                self.assertEqual(invalid.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            record = portal_server.node_by_id(node_id)
            self.assertEqual(len(record["status"]["logs"]), 2)
            self.assertEqual(len(record["status"]["logs"][0]), 512)
            self.assertEqual(record["status"]["radio"]["mode"], "rx")
            self.assertNotIn("t", record["status"]["radio"])
            self.assertTrue(record["online"])
            portal_server.LIVE_LOGS.nodes.pop(node_id, None)


class PortalServiceCommandTests(unittest.TestCase):
    def test_admin_service_request_is_queued_for_agent(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Online node")
            with database() as connection:
                connection.execute(
                    "UPDATE nodes SET last_seen = ? WHERE id = ?",
                    ("2999-01-01T00:00:00+00:00", node["node_id"]),
                )
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                credentials = base64.b64encode(
                    b"admin:this-is-a-long-test-password"
                ).decode()
                headers = {
                    **session_header(base_url, credentials),
                    "Content-Type": "application/json",
                }
                endpoint = f"{base_url}/api/nodes/{node['node_id']}/service-commands"
                request = Request(
                    endpoint,
                    data=json.dumps({"operation": "start"}).encode("utf-8"),
                    headers=headers,
                )
                with urlopen(request, timeout=2) as response:
                    command_id = json.loads(response.read())["command_id"]

                queued = claim_agent_commands(node["node_id"])
                self.assertEqual(
                    queued,
                    [{"id": command_id, "action": "service", "operation": "start"}],
                )
                self.assertTrue(complete_agent_command(node["node_id"], command_id, {"ok": True}))

                with urlopen(
                    Request(
                        f"{base_url}/api/commands/{command_id}",
                        headers={**session_header(base_url, credentials)},
                    ),
                    timeout=2,
                ) as response:
                    self.assertEqual(
                        json.loads(response.read()),
                        {"status": "completed", "result": {"ok": True}},
                    )

                with self.assertRaises(HTTPError) as invalid_operation:
                    urlopen(
                        Request(
                            endpoint,
                            data=json.dumps({"operation": "reload"}).encode("utf-8"),
                            headers=headers,
                        ),
                        timeout=2,
                    )
                self.assertEqual(invalid_operation.exception.code, 400)
                with urlopen(
                    Request(
                        f"{base_url}/nodes/{node['node_id']}",
                        headers={**session_header(base_url, credentials)},
                    ),
                    timeout=2,
                ) as response:
                    detail_page = response.read().decode("utf-8")
                self.assertIn('id="service-start"', detail_page)
                self.assertIn('id="service-stop"', detail_page)
                node_script = fetch_asset(
                    base_url, {**session_header(base_url, credentials)}, "node.js"
                )
                self.assertIn("filtered.slice().reverse()", node_script)
                self.assertIn("waitForServiceStatus(operation !== 'stop')", node_script)
                self.assertIn('id="service-restart"', detail_page)
                self.assertIn("Du kan nu starta den.", node_script)
                self.assertIn("Du kan nu stänga av den.", node_script)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class PortalUpdateCommandTests(unittest.TestCase):
    def test_update_command_is_admin_only_and_keeps_a_long_build_alive(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": "this-is-a-long-test-password",
            },
        ):
            node = provision_node("Update node")
            with database() as connection:
                connection.execute(
                    "UPDATE nodes SET last_seen = ? WHERE id = ?",
                    ("2999-01-01T00:00:00+00:00", node["node_id"]),
                )
                connection.execute(
                    "INSERT INTO portal_users (username, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "operator",
                        hash_password("Operator-password-1"),
                        "operator",
                        "2025-01-01T00:00:00+00:00",
                    ),
                )
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                admin_credentials = base64.b64encode(
                    b"admin:this-is-a-long-test-password"
                ).decode()
                admin_headers = {
                    **session_header(base_url, admin_credentials),
                    "Content-Type": "application/json",
                }
                operator_credentials = base64.b64encode(
                    b"operator:Operator-password-1"
                ).decode()
                operator_headers = {
                    **session_header(base_url, operator_credentials),
                    "Content-Type": "application/json",
                }
                endpoint = f"{base_url}/api/nodes/{node['node_id']}/update-commands"
                with self.assertRaises(HTTPError) as invalid_channel:
                    urlopen(
                        Request(
                            endpoint,
                            data=json.dumps({"channel": "master"}).encode(),
                            headers=admin_headers,
                        ),
                        timeout=2,
                    )
                self.assertEqual(invalid_channel.exception.code, 400)
                with self.assertRaises(HTTPError) as forbidden:
                    urlopen(
                        Request(
                            endpoint,
                            data=json.dumps({"channel": "release"}).encode(),
                            headers=operator_headers,
                        ),
                        timeout=2,
                    )
                self.assertEqual(forbidden.exception.code, 403)

                with urlopen(
                    Request(
                        endpoint,
                        data=json.dumps({"channel": "release"}).encode(),
                        headers=admin_headers,
                    ),
                    timeout=2,
                ) as response:
                    command_id = json.loads(response.read())["command_id"]
                self.assertEqual(
                    claim_agent_commands(node["node_id"]),
                    [{
                        "id": command_id,
                        "action": "update",
                        "channel": "release",
                    }],
                )
                agent_headers = {
                    "Authorization": "Bearer " + node["token"],
                    "Content-Type": "application/json",
                }
                with urlopen(
                    Request(
                        f"{base_url}/api/v1/agent/commands/{command_id}/progress",
                        data=json.dumps({"stage": "starting"}).encode(),
                        headers=agent_headers,
                    ),
                    timeout=2,
                ) as response:
                    self.assertEqual(response.status, 200)
                progress = take_agent_command_result(command_id)
                self.assertEqual(progress["status"], "in_progress")
                self.assertEqual(progress["progress"]["stage"], "starting")
                self.assertEqual(
                    progress["progress"]["message"],
                    "Uppdateringen har startat.",
                )
                with urlopen(
                    Request(
                        f"{base_url}/api/nodes/{node['node_id']}",
                        headers={**session_header(base_url, admin_credentials)},
                    ),
                    timeout=2,
                ) as response:
                    node_status = json.loads(response.read())["node"]
                self.assertEqual(node_status["update"]["status"], "in_progress")
                self.assertEqual(
                    node_status["update"]["progress"]["stage"],
                    "starting",
                )
                with urlopen(
                    Request(
                        f"{base_url}/api/nodes",
                        headers={**session_header(base_url, admin_credentials)},
                    ),
                    timeout=2,
                ) as response:
                    dashboard = json.loads(response.read())["nodes"]
                self.assertEqual(dashboard[0]["update"]["status"], "in_progress")
                dashboard_script = fetch_asset(
                    base_url,
                    session_header(base_url, admin_credentials),
                    "index.js",
                )
                self.assertIn("update-spinner", dashboard_script)
                node_script = fetch_asset(
                    base_url,
                    session_header(base_url, admin_credentials),
                    "node.js",
                )
                self.assertIn("nodeUpdate?.status", node_script)
                with self.assertRaises(HTTPError) as duplicate_update:
                    urlopen(
                        Request(
                            endpoint,
                            data=json.dumps({"channel": "release"}).encode(),
                            headers=admin_headers,
                        ),
                        timeout=2,
                    )
                self.assertEqual(duplicate_update.exception.code, 409)

                old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                with database() as connection:
                    connection.execute(
                        "UPDATE agent_commands SET created_at = ?, updated_at = ? WHERE id = ?",
                        (old, old, command_id),
                    )
                self.assertEqual(claim_agent_commands(node["node_id"]), [])
                self.assertEqual(
                    take_agent_command_result(command_id)["status"],
                    "in_progress",
                )
                with self.assertRaisesRegex(RuntimeError, "Noden uppdateras"):
                    enqueue_agent_command(
                        node["node_id"],
                        {"action": "service", "operation": "restart"},
                        "admin",
                    )
                self.assertEqual(claim_agent_commands(node["node_id"]), [])
                self.assertTrue(complete_agent_command(
                    node["node_id"],
                    command_id,
                    {"ok": True, "channel": "release", "version": "26.05.2"},
                ))
                self.assertEqual(
                    take_agent_command_result(command_id),
                    {
                        "status": "completed",
                        "result": {"ok": True, "channel": "release", "version": "26.05.2"},
                    },
                )
                service_id = enqueue_agent_command(
                    node["node_id"],
                    {"action": "service", "operation": "restart"},
                    "admin",
                )
                self.assertEqual(
                    claim_agent_commands(node["node_id"]),
                    [{"id": service_id, "action": "service", "operation": "restart"}],
                )
                self.assertTrue(complete_agent_command(
                    node["node_id"], service_id, {"ok": True}
                ))
                with urlopen(
                    Request(
                        f"{base_url}/api/audit",
                        headers={**session_header(base_url, admin_credentials)},
                    ),
                    timeout=2,
                ) as response:
                    updates = [
                        entry for entry in json.loads(response.read())["entries"]
                        if entry["action"] == "update"
                    ]
                self.assertEqual(
                    (updates[0]["detail"], updates[0]["outcome"]),
                    ("release", "completed"),
                )
                with urlopen(
                    Request(
                        f"{base_url}/nodes/{node['node_id']}",
                        headers={**session_header(base_url, admin_credentials)},
                    ),
                    timeout=2,
                ) as response:
                    self.assertIn('id="update-start"', response.read().decode("utf-8"))
                self.assertIn(
                    "/update-commands",
                    fetch_asset(base_url, session_header(base_url, admin_credentials), "node.js"),
                )
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
            stopped_node = provision_node("Stopped node")
            with database() as connection:
                connection.execute(
                    "UPDATE nodes SET last_seen = ?, status_json = ? WHERE id = ?",
                    (
                        "2999-01-01T00:00:00+00:00",
                        json.dumps({"active": True}),
                        online_node["node_id"],
                    ),
                )
                connection.execute(
                    "UPDATE nodes SET last_seen = ?, status_json = ? WHERE id = ?",
                    (
                        "2999-01-01T00:00:00+00:00",
                        json.dumps({"active": False}),
                        stopped_node["node_id"],
                    ),
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
                admin_headers = {**session_header(base_url, credentials)}
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
                dtmf_script = fetch_asset(base_url, admin_headers, "dtmf.js")
                self.assertIn("/api/dtmf-commands", dtmf_script)
                self.assertIn("node.status?.active === true", dtmf_script)

                stopped_request = Request(
                    f"{base_url}/api/nodes/{stopped_node['node_id']}/dtmf-commands",
                    data=json.dumps({"sequence": "*123#"}).encode("utf-8"),
                    headers={**admin_headers, "Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as stopped_error:
                    urlopen(stopped_request, timeout=2)
                self.assertEqual(stopped_error.exception.code, 409)
                self.assertIn(
                    "SvxLink-tjänsten är inte igång",
                    json.loads(stopped_error.exception.read().decode("utf-8"))["error"],
                )

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
                                stopped_node["node_id"],
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
                    ["pending", "failed", "failed", "failed"],
                )
                self.assertEqual(
                    bulk_result["results"][1]["error"],
                    "Noden är offline. Anslut den innan du skickar kommandon.",
                )
                self.assertEqual(
                    bulk_result["results"][2]["error"],
                    "SvxLink-tjänsten är inte igång. Starta tjänsten innan du skickar DTMF.",
                )
                self.assertEqual(
                    bulk_result["results"][3]["error"], "Noden hittades inte."
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
                    **session_header(url.rsplit("/api/", 1)[0], credentials),
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


class PortalUserAndAuditTests(unittest.TestCase):
    def test_users_roles_and_audit_log(self):
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
                base = f"http://127.0.0.1:{server.server_port}"

                cookies = {}

                def call(user, password, path, body=None, method=None):
                    if (user, password) not in cookies:
                        cookies[(user, password)] = login_cookie(base, user, password)
                    request = Request(
                        base + path,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={"Cookie": cookies[(user, password)],
                                 "Content-Type": "application/json"},
                        method=method,
                    )
                    with urlopen(request, timeout=2) as response:
                        return json.loads(response.read())

                def status(*args, **kwargs):
                    with self.assertRaises(HTTPError) as error:
                        call(*args, **kwargs)
                    return error.exception.code

                admin = ("admin", "this-is-a-long-test-password")
                call(*admin, "/api/users", {
                    "username": "olle", "password": "Operator-password-1", "role": "operator",
                })
                self.assertEqual(status(*admin, "/api/users", {
                    "username": "olle", "password": "Operator-password-1", "role": "operator",
                }), 400)
                self.assertEqual(status(*admin, "/api/users", {
                    "username": "kort", "password": "short", "role": "operator",
                }), 400)
                for weak in ("alllowercase1", "NOLOWERCASE1", "NoDigitsHere", "Ab1defg"):
                    self.assertEqual(status(*admin, "/api/users", {
                        "username": "svag", "password": weak, "role": "operator",
                    }), 400)
                call(*admin, "/api/users", {"username": "ok8", "password": "Abcdef12", "role": "operator"})
                call(*admin, "/api/users/ok8", {"password": "Abcdef34"}, "POST")
                self.assertEqual(status(*admin, "/api/users/ok8", {"password": "abcdef34"}, "POST"), 400)
                call(*admin, "/api/users/ok8", {"role": "admin"}, "POST")
                call(*admin, "/api/users/ok8", None, "DELETE")
                operator = ("olle", "Operator-password-1")
                self.assertEqual(call(*operator, "/api/me")["role"], "operator")
                self.assertEqual(status(*operator, "/api/users"), 403)
                self.assertEqual(status(*operator, "/api/audit"), 403)
                self.assertEqual(status("olle", "wrong", "/api/me"), 401)

                self.assertEqual(status(*operator, "/api/nodes", {"name": "Audit"}), 403)
                node = call(*admin, "/api/nodes", {"name": "Audit"})
                self.assertFalse(call(*operator, "/api/nodes")["can_delete_nodes"])
                self.assertEqual(call(*operator, "/api/nodes")["role"], "operator")
                node_path = f"/api/nodes/{node['node_id']}"
                self.assertEqual(call(*operator, node_path)["node"]["id"], node["node_id"])
                self.assertEqual(status(*operator, f"{node_path}/service-commands",
                                        {"operation": "stop"}), 403)
                self.assertEqual(status(*operator, f"{node_path}/config-commands",
                                        {"action": "read", "document": "node_info.json"}), 403)
                self.assertEqual(status(*operator, "/api/config-commands/" + "0" * 32), 403)
                self.assertEqual(status(*operator, "/api/dtmf-buttons", []), 403)
                self.assertEqual(status(*operator, f"/nodes/{node['node_id']}/config"), 403)
                self.assertEqual(status(*operator, f"/nodes/{node['node_id']}/nodeinfo"), 403)
                self.assertEqual(status(*operator, f"/nodes/{node['node_id']}/schedules"), 403)
                self.assertEqual(status(*operator, node_path, {"password": operator[1]}, "DELETE"), 403)
                self.assertEqual(status(*admin, node_path, {"password": "wrong-password"}, "DELETE"), 403)
                with database() as connection:
                    connection.execute(
                        "UPDATE nodes SET last_seen = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), node["node_id"]),
                    )
                queued = call(*operator, f"/api/nodes/{node['node_id']}/dtmf-commands",
                              {"sequence": "91240#"})
                claim_agent_commands(node["node_id"])
                complete_agent_command(node["node_id"], queued["command_id"], {"ok": True})

                entries = call(*admin, "/api/audit")["entries"]
                dtmf = next(entry for entry in entries if entry["action"] == "dtmf")
                self.assertEqual(
                    (dtmf["actor"], dtmf["detail"], dtmf["outcome"]),
                    ("olle", "91240#", "completed"),
                )
                self.assertTrue(any(entry["action"] == "user.create" for entry in entries))

                call(*admin, "/api/users/olle", {"role": "admin"}, "POST")
                self.assertEqual(call(*operator, "/api/me")["role"], "admin")
                self.assertTrue(call(*operator, "/api/nodes")["can_delete_nodes"])
                self.assertEqual(
                    call(*operator, node_path, {"password": operator[1]}, "DELETE")["message"],
                    "Noden är borttagen.",
                )
                with database() as connection:
                    self.assertIsNone(connection.execute(
                        "SELECT id FROM nodes WHERE id = ?", (node["node_id"],)
                    ).fetchone())
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM agent_commands WHERE node_id = ?",
                        (node["node_id"],),
                    ).fetchone()[0], 0)
                delete_entry = next(
                    entry for entry in call(*admin, "/api/audit")["entries"]
                    if entry["action"] == "node.delete"
                )
                self.assertEqual(
                    (delete_entry["actor"], delete_entry["node_name"], delete_entry["detail"]),
                    ("olle", "Audit", "Audit"),
                )
                call(*admin, "/api/users/olle", method="DELETE")
                self.assertEqual(status(*operator, "/api/me"), 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class PortalLoginTests(unittest.TestCase):
    def test_login_page_sessions_and_case_insensitive_users(self):
        import http.client

        password = "this-is-a-long-test-password"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PORTAL_DATABASE": str(Path(directory) / "portal.sqlite3"),
                "PORTAL_ADMIN_USER": "admin",
                "PORTAL_ADMIN_PASSWORD": password,
            },
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                base = f"http://127.0.0.1:{port}"

                def raw(method, path, body=None, cookie=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    headers = {"Content-Type": "application/json"}
                    if cookie:
                        headers["Cookie"] = cookie
                    conn.request(
                        method, path, json.dumps(body) if body is not None else None, headers
                    )
                    response = conn.getresponse()
                    data = response.read()
                    result = (response.status, response.getheader("Location"),
                              response.getheader("Set-Cookie"), data)
                    conn.close()
                    return result

                self.assertEqual(raw("GET", "/api/nodes")[0], 401)
                status, location, _, _ = raw("GET", "/nodes/x/config")
                self.assertEqual(status, 302)
                self.assertTrue(location.startswith("/login?next="))
                self.assertEqual(raw("GET", "/login")[0], 200)
                self.assertEqual(raw("GET", "/assets/login.js")[0], 200)
                self.assertEqual(raw("GET", "/assets/node.js")[0], 302)

                bad = raw("POST", "/api/login", {"username": "ADMIN", "password": "nope"})
                self.assertEqual(bad[0], 401)

                status, _, set_cookie, _ = raw(
                    "POST", "/api/login", {"username": "ADMIN", "password": password}
                )
                self.assertEqual(status, 200)
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Strict", set_cookie)
                admin_cookie = set_cookie.split(";", 1)[0]
                self.assertEqual(raw("GET", "/api/me", cookie=admin_cookie)[0], 200)

                user_password = "Operator-password-1"
                created = raw(
                    "POST", "/api/users",
                    {"username": "Olle", "password": user_password, "role": "operator"},
                    admin_cookie,
                )
                self.assertEqual(created[0], 201)
                duplicate = raw(
                    "POST", "/api/users",
                    {"username": "OLLE", "password": user_password, "role": "operator"},
                    admin_cookie,
                )
                self.assertEqual(duplicate[0], 400)

                olle_cookie = login_cookie(base, "oLLE", user_password)
                self.assertEqual(raw("GET", "/api/me", cookie=olle_cookie)[0], 200)

                raw("POST", "/api/logout", cookie=olle_cookie)
                self.assertEqual(raw("GET", "/api/me", cookie=olle_cookie)[0], 401)

                olle_cookie = login_cookie(base, "olle", user_password)
                raw("DELETE", "/api/users/olle", cookie=admin_cookie)
                self.assertEqual(raw("GET", "/api/me", cookie=olle_cookie)[0], 401)

                for _ in range(10):
                    raw("POST", "/api/login", {"username": "admin", "password": "wrong"})
                self.assertEqual(
                    raw("POST", "/api/login", {"username": "admin", "password": password})[0],
                    429,
                )
            finally:
                _login_failures.clear()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
