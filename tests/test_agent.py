import json
import os
import stat
import tempfile
import unittest
from urllib.error import URLError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.agent import (
    CONFIG_HELPER,
    execute_config_command,
    execute_dtmf_command,
    make_status,
    read_dtmf_control_paths,
    read_logic_info,
    report_status,
    validate_portal_url,
)


class AgentStatusTests(unittest.TestCase):
    def test_reads_first_active_radio_logic(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text(
                "[GLOBAL]\nLOGICS=ParrotLogic,RepeaterLogic\n"
                "[ParrotLogic]\nTYPE=Parrot\n"
                "[RepeaterLogic]\nTYPE=Repeater\nCALLSIGN=\"SA7TEST\"\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_logic_info(config),
                {"logic": "RepeaterLogic", "callsign": "SA7TEST"},
            )

    def test_maps_webpanel_status_to_portal_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text("[GLOBAL]\nLOGICS=SimplexLogic\n[SimplexLogic]\nTYPE=Simplex\n")
            status = make_status(
                {
                    "active": True,
                    "service_status": "active",
                    "current_tg": 240,
                    "radio": {"mode": "rx", "signal_level": 72},
                    "lines": ["SvxLink started", "Receiving signal\u0000noise"],
                },
                config,
            )
        self.assertEqual(status["current_tg"], "240")
        self.assertEqual(status["radio"], {"mode": "rx", "signal_level": 72})
        self.assertEqual(status["logic"], "SimplexLogic")
        self.assertEqual(status["logs"], ["SvxLink started", "Receiving signalnoise"])

    def test_limits_forwarded_log_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text("[GLOBAL]\nLOGICS=SimplexLogic\n[SimplexLogic]\nTYPE=Simplex\n")
            status = make_status(
                {
                    "active": True,
                    "service_status": "active",
                    "lines": ["old"] + ["å" * 300 for _ in range(55)],
                },
                config,
            )

        self.assertEqual(len(status["logs"]), 50)
        self.assertTrue(all(len(line.encode("utf-8")) <= 512 for line in status["logs"]))

    def test_rejects_invalid_log_lines(self):
        with self.assertRaisesRegex(ValueError, "ogiltiga loggrader"):
            make_status(
                {
                    "active": True,
                    "service_status": "active",
                    "lines": ["valid", 42],
                },
                "/missing/svxlink.conf",
            )

    def test_accepts_http_portal_endpoint(self):
        self.assertEqual(
            validate_portal_url("http://portal.example:8080/"),
            "http://portal.example:8080",
        )

    def test_accepts_https_portal_endpoint(self):
        self.assertEqual(
            validate_portal_url("https://portal.example/"),
            "https://portal.example",
        )

    def test_rejects_unsupported_portal_scheme(self):
        with self.assertRaises(RuntimeError):
            validate_portal_url("ftp://portal.example")

    def test_rejects_portal_url_with_credentials(self):
        with self.assertRaises(RuntimeError):
            validate_portal_url("https://user:password@portal.example")

    def test_rejects_incomplete_panel_status(self):
        with self.assertRaisesRegex(ValueError, "tjänstestatus"):
            make_status({"service_status": "active"}, "/missing/svxlink.conf")

    def test_logs_local_webpanel_timeout_separately(self):
        with patch("agent.agent.request_json", side_effect=URLError("timed out")) as request:
            with patch("agent.agent.logging.warning") as warning:
                reported = report_status(
                    "http://127.0.0.1:8090/api/status",
                    "https://portal.example/api/v1/agent/status",
                    "node-1",
                    "/etc/svxlink/svxlink.conf",
                    {"Authorization": "Basic test"},
                    {"Authorization": "Bearer test"},
                )

        self.assertFalse(reported)
        request.assert_called_once()
        self.assertIn("lokala SvxLink-webpanelen", warning.call_args.args[0])

    def test_logs_portal_timeout_separately(self):
        with patch(
            "agent.agent.request_json",
            side_effect=[
                {
                    "active": True,
                    "service_status": "active",
                    "current_tg": None,
                    "radio": {},
                },
                URLError("timed out"),
            ],
        ):
            with patch("agent.agent.logging.warning") as warning:
                with patch("agent.agent.make_status", return_value={"active": True}):
                    reported = report_status(
                        "http://127.0.0.1:8090/api/status",
                        "https://portal.example/api/v1/agent/status",
                        "node-1",
                        "/etc/svxlink/svxlink.conf",
                        {"Authorization": "Basic test"},
                        {"Authorization": "Bearer test"},
                    )

        self.assertFalse(reported)
        self.assertIn("portalen", warning.call_args.args[0])

    def test_config_command_uses_fixed_privileged_helper(self):
        command = {
            "id": "a" * 32,
            "action": "read",
            "document": "svxlink.conf",
        }
        helper_result = {
            "ok": True,
            "document": "svxlink.conf",
            "content": "[GLOBAL]\n",
            "sha256": "b" * 64,
        }
        with patch(
            "agent.agent.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=(json.dumps(helper_result) + "\n").encode(),
                stderr=b"",
            ),
        ) as run_helper:
            result = execute_config_command(command)

        self.assertEqual(result, helper_result)
        self.assertEqual(run_helper.call_args.args[0], ["sudo", "-n", CONFIG_HELPER])
        self.assertNotIn('"id"', run_helper.call_args.kwargs["input"].decode())

    def test_rejects_arbitrary_config_path_before_privilege_escalation(self):
        command = {
            "id": "a" * 32,
            "action": "read",
            "document": "../../etc/passwd",
        }
        with patch("agent.agent.subprocess.run") as run_helper:
            with self.assertRaisesRegex(ValueError, "Endast"):
                execute_config_command(command)
        run_helper.assert_not_called()

    def test_reads_and_deduplicates_configured_dtmf_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text(
                "[GLOBAL]\nLOGICS=RepeaterLogic,SimplexLogic\n"
                "[RepeaterLogic]\nDTMF_CTRL_PTY=/run/svxlink/control\n"
                "[SimplexLogic]\nDTMF_CTRL_PTY=/run/svxlink/control\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_dtmf_control_paths(config), ["/run/svxlink/control"]
            )

    def test_rejects_missing_dtmf_control_path(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text(
                "[GLOBAL]\nLOGICS=RepeaterLogic\n[RepeaterLogic]\nTYPE=Repeater\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Ingen DTMF_CTRL_PTY"):
                read_dtmf_control_paths(config)

    def test_sends_dtmf_sequence_to_configured_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text(
                "[GLOBAL]\nLOGICS=RepeaterLogic\n"
                "[RepeaterLogic]\nDTMF_CTRL_PTY=/run/svxlink/control\n",
                encoding="utf-8",
            )
            command = {"id": "a" * 32, "action": "dtmf", "sequence": "*123#"}
            with patch("agent.agent.os.open", return_value=17) as open_path:
                with patch(
                    "agent.agent.os.fstat",
                    return_value=SimpleNamespace(st_mode=stat.S_IFIFO),
                ):
                    with patch("agent.agent.os.write", return_value=5) as write:
                        with patch("agent.agent.os.close") as close:
                            result = execute_dtmf_command(command, config)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(open_path.call_args.args[0], "/run/svxlink/control")
        self.assertEqual(
            open_path.call_args.args[1] & getattr(os, "O_NOFOLLOW", 0), 0
        )
        self.assertEqual(write.call_args.args, (17, b"*123#"))
        close.assert_called_once_with(17)

    def test_rejects_non_terminal_character_device(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            config.write_text(
                "[GLOBAL]\nLOGICS=RepeaterLogic\n"
                "[RepeaterLogic]\nDTMF_CTRL_PTY=/dev/null\n",
                encoding="utf-8",
            )
            command = {"id": "a" * 32, "action": "dtmf", "sequence": "*123#"}
            with patch("agent.agent.os.open", return_value=17):
                with patch(
                    "agent.agent.os.fstat",
                    return_value=SimpleNamespace(st_mode=stat.S_IFCHR),
                ):
                    with patch("agent.agent.os.isatty", return_value=False):
                        with patch("agent.agent.os.write") as write:
                            with patch("agent.agent.os.close"):
                                result = execute_dtmf_command(command, config)

        self.assertFalse(result["ok"])
        self.assertIn("inte en PTY", result["error"])
        write.assert_not_called()

    def test_rejects_invalid_dtmf_command_without_opening_path(self):
        command = {"id": "a" * 32, "action": "dtmf", "sequence": "12;reboot"}
        with patch("agent.agent.os.open") as open_path:
            with self.assertRaisesRegex(ValueError, "ogiltigt format"):
                execute_dtmf_command(command, "/missing/svxlink.conf")
        open_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
