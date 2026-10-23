import tempfile
import unittest
from urllib.error import URLError
from pathlib import Path
from unittest.mock import patch

from agent.agent import make_status, read_logic_info, report_status, validate_portal_url


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


if __name__ == "__main__":
    unittest.main()
