import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config_helper import read_document, write_document


class ConfigHelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.svxlink_config = root / "svxlink.conf"
        self.node_info = root / "node_info.json"
        self.svxlink_config.write_text("[GLOBAL]\nLOGICS=RepeaterLogic\n", encoding="utf-8")
        self.node_info.write_text('{"CALLSIGN": "SA7TEST"}\n', encoding="utf-8")
        self.paths = {
            "svxlink.conf": str(self.svxlink_config),
            "node_info.json": str(self.node_info),
        }
        self.path_patch = patch("config_helper.CONFIG_PATHS", self.paths)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def test_read_and_write_creates_backup_and_preserves_mode(self):
        original_mode = os.stat(self.svxlink_config).st_mode & 0o777
        original = read_document("svxlink.conf")
        updated = "[GLOBAL]\nLOGICS=SimplexLogic\n"
        result = write_document(
            "svxlink.conf",
            updated,
            original["sha256"],
        )

        self.assertEqual(self.svxlink_config.read_text(encoding="utf-8"), updated)
        self.assertEqual(result["sha256"], hashlib.sha256(updated.encode()).hexdigest())
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), original["content"].encode("utf-8"))
        self.assertEqual(os.stat(self.svxlink_config).st_mode & 0o777, original_mode)

    def test_write_rejects_stale_checksum_without_modifying_file(self):
        stale_checksum = read_document("svxlink.conf")["sha256"]
        self.svxlink_config.write_text("external edit\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "ändrats sedan den lästes"):
            write_document("svxlink.conf", "portal edit\n", stale_checksum)
        self.assertEqual(self.svxlink_config.read_text(encoding="utf-8"), "external edit\n")

    def test_node_info_must_be_valid_json(self):
        checksum = read_document("node_info.json")["sha256"]
        with self.assertRaisesRegex(ValueError, "Ogiltig JSON"):
            write_document("node_info.json", "{invalid", checksum)

    def test_only_fixed_documents_are_allowed(self):
        with self.assertRaisesRegex(ValueError, "Endast"):
            read_document("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
