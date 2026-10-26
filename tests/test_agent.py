import json
import os
import stat
import tempfile
import unittest
from urllib.error import URLError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent import version as agent_version
from agent.agent import (
    CONFIG_HELPER,
    execute_config_command,
    execute_dtmf_command,
    execute_service_command,
    read_dtmf_control_paths,
    read_logic_info,
    validate_portal_url,
)
from agent.collector import Collector, RadioState, normalize_node_info, parse_state_line, read_node_info


class AgentVersionTests(unittest.TestCase):
    def test_version_file_is_in_agent_directory(self):
        self.assertEqual(
            agent_version.VERSION_FILE,
            Path(agent_version.__file__).resolve().parent / "VERSION",
        )

    def test_reads_own_version_file(self):
        with tempfile.TemporaryDirectory() as directory:
            version_file = Path(directory) / "VERSION"
            version_file.write_text("2.3.4\n", encoding="utf-8")
            with patch("agent.version.VERSION_FILE", version_file):
                self.assertEqual(agent_version.read_version(), "2.3.4")


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

    def test_collector_snapshot_reads_config_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "svxlink.conf"
            log = Path(directory) / "svxlink.log"
            config.write_text(
                "[GLOBAL]\nLOGICS=SimplexLogic\n[SimplexLogic]\nTYPE=Simplex\nCALLSIGN=SA7TEST\n"
                "[ReflectorLogic]\nMONITOR_TGS=240,2400,240\n",
                encoding="utf-8",
            )
            log.write_text(
                "x ReflectorLogic: Selecting TG #240\nx Talker start on TG #240: SA7ABC\n"
                "x ERROR boom\x00\n",
                encoding="utf-8",
            )
            collector = Collector(config, log)
            collector._poll_log()
            status = collector.snapshot()
            collector.stop()
        self.assertEqual(status["current_tg"], "240")
        self.assertEqual(status["logic"], "SimplexLogic")
        self.assertEqual(status["callsign"], "SA7TEST")
        self.assertEqual(status["monitor_tgs"], ["240", "2400"])
        self.assertEqual(status["tg_event"], "TG 240 · Talker start · SA7ABC")
        self.assertEqual(status["errors"], 1)
        self.assertEqual(status["radio"]["mode"], "unknown")
        self.assertEqual(status["logs"][-1], "x ERROR boom")

    def test_collector_limits_forwarded_log_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "svxlink.log"
            log.write_text("\n".join(["å" * 300] * 60) + "\n", encoding="utf-8")
            collector = Collector(Path(directory) / "missing.conf", log)
            collector._poll_log()
            status = collector.snapshot()
            collector.stop()
        self.assertEqual(len(status["logs"]), 50)
        self.assertTrue(all(len(line.encode("utf-8")) <= 512 for line in status["logs"]))

    def test_radio_state_tracks_rx_tx_and_voter_events(self):
        state = RadioState()
        self.assertEqual(state.snapshot()["mode"], "unknown")
        state.apply(*parse_state_line(
            b'1700000000.000 Rx:sql_state {"name":"Rx1","sql_open":true,"siglev":72}'
        ))
        snapshot = state.snapshot()
        self.assertEqual((snapshot["mode"], snapshot["signal_level"]), ("rx", 72))
        state.apply(*parse_state_line(b'1 Tx:state {"name":"Tx1","transmit":true}'))
        self.assertEqual(state.snapshot()["mode"], "tx")
        state.apply(*parse_state_line(b'2 Tx:state {"name":"Tx1","transmit":false}'))
        state.apply(*parse_state_line(
            b'3 Voter:sql_state [{"name":"A","enabled":true,"sql_open":false,'
            b'"siglev":150,"active":true}]'
        ))
        snapshot = state.snapshot()
        self.assertEqual((snapshot["mode"], snapshot["signal_level"]), ("rx", 100))
        self.assertIsNone(parse_state_line(b"4 Other:event {}"))
        with self.assertRaises(ValueError):
            state.apply(*parse_state_line(b'5 Tx:state {"name":"","transmit":true}'))

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

    def test_service_command_only_runs_fixed_svxlink_service_operation(self):
        command = {"id": "a" * 32, "action": "service", "operation": "stop"}
        with patch(
            "agent.agent.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stderr=b""),
        ) as run_systemctl:
            result = execute_service_command(command)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            run_systemctl.call_args.args[0],
            ["sudo", "-n", "/usr/bin/systemctl", "stop", "svxlink.service"],
        )
        for operation in ("reload", "start;reboot"):
            with self.subTest(operation=operation), patch(
                "agent.agent.subprocess.run"
            ) as run_invalid:
                with self.assertRaisesRegex(ValueError, "ogiltigt format"):
                    execute_service_command(
                        {"id": "a" * 32, "action": "service", "operation": operation}
                    )
                run_invalid.assert_not_called()


class NodeInfoTests(unittest.TestCase):
    EXAMPLE = """{
  "nodeLocation": "On The road", "nodeClass": "hotspot", "hidden": false,
  "sysop": "SA7MHN", "toneToTalkgroup": {"79.7": 0},
  "qth": [{"name": "Mobile", "pos": {"lat": "55.38", "long": "13.15", "loc": "JO65NJ"},
    "rx": {"B": {"name": "Rx1", "freq": 433.950, "sqlType": "CTCSS | GPIO",
      "ctcssFreq": [79.7], "ant": [{"comment": "Mobile", "height": 2, "dir": 0}]},},
    "tx": {"T": {"name": "Tx1", "freq": 433.950, "pwr": 1, "ctcssFreq": 79.7,
      "ant": [{"comment": "Mobile", "height": 2, "dir": 0}]}}}]
}"""

    def test_reads_node_info_with_trailing_comma_and_normalizes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "node_info.json"
            path.write_text(self.EXAMPLE, encoding="utf-8")
            info = read_node_info(str(path))
        self.assertEqual(info["sysop"], "SA7MHN")
        self.assertEqual(info["tone_to_tg"], [{"tone": "79.7", "tg": "0"}])
        qth = info["qth"][0]
        self.assertEqual(qth["loc"], "JO65NJ")
        self.assertEqual(qth["rx"][0]["ctcss"], [79.7])
        self.assertEqual(qth["rx"][0]["sql_type"], "CTCSS | GPIO")
        self.assertEqual(qth["tx"][0]["ctcss"], [79.7])
        self.assertEqual(qth["tx"][0]["pwr"], 1)
        self.assertEqual(qth["tx"][0]["ants"][0]["height"], 2)

    def test_non_object_node_info_is_ignored(self):
        self.assertIsNone(normalize_node_info([1, 2]))


if __name__ == "__main__":
    unittest.main()
