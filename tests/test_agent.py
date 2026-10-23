import tempfile
import unittest
from pathlib import Path

from agent.agent import make_status, read_logic_info, validate_portal_url


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
                },
                config,
            )
        self.assertEqual(status["current_tg"], "240")
        self.assertEqual(status["radio"], {"mode": "rx", "signal_level": 72})
        self.assertEqual(status["logic"], "SimplexLogic")

    def test_rejects_non_https_portal_endpoint(self):
        with self.assertRaises(RuntimeError):
            validate_portal_url("http://portal.example")

    def test_rejects_portal_url_with_credentials(self):
        with self.assertRaises(RuntimeError):
            validate_portal_url("https://user:password@portal.example")

    def test_rejects_incomplete_panel_status(self):
        with self.assertRaisesRegex(ValueError, "tjänstestatus"):
            make_status({"service_status": "active"}, "/missing/svxlink.conf")


if __name__ == "__main__":
    unittest.main()
