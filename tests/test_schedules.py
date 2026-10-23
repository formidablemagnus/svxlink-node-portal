import base64
import json
import unittest
from unittest.mock import patch

from agent import schedules

SHA_EMPTY = schedules.checksum("")


def webpanel_line(schedule, target="/dev/shm/dtmf_ctrl"):
    metadata = base64.urlsafe_b64encode(
        json.dumps(schedule, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).decode("ascii")
    expression = " ".join(schedule[k] for k in ("minute", "hour", "day", "month", "weekday"))
    return f"{expression} printf '\\%s\\n' '{schedule['dtmf']}' > {target} # SVXLINK-WEBPANEL:{metadata}"


SCHEDULE = {
    "label": "TG 2407", "dtmf": "942407364#",
    "minute": "45", "hour": "19", "day": "*", "month": "*", "weekday": "4",
}


class ScheduleTests(unittest.TestCase):
    def crontab(self, schedule_lines, other=("MAILTO=\"\"",)):
        return "\n".join(
            [*other, "", schedules.CRONTAB_START, *schedule_lines, schedules.CRONTAB_END]
        ) + "\n"

    def test_existing_webpanel_schedules_are_parsed(self):
        content = self.crontab([webpanel_line(SCHEDULE)])
        data = schedules.response_data(content, "/dev/shm/dtmf_ctrl")
        self.assertEqual(data["schedules"][0]["dtmf"], "942407364#")
        self.assertEqual(data["schedules"][0]["target"], "/dev/shm/dtmf_ctrl")
        self.assertEqual(data["other_lines"], ['MAILTO=""'])

    def test_rebuilding_keeps_line_identical_to_webpanel(self):
        line = webpanel_line(SCHEDULE)
        content = self.crontab([line])
        parsed = schedules.parse_managed_crontab(content)[0]
        self.assertEqual(schedules.build_crontab(content, parsed), content)

    def test_metadata_encoding_differences_are_tolerated(self):
        loose = {**SCHEDULE, "label": "TG 2407 "}
        content = self.crontab([webpanel_line(loose)])
        self.assertEqual(schedules.parse_managed_crontab(content)[0][0]["label"], "TG 2407")

    def test_manually_edited_line_is_rejected(self):
        line = webpanel_line(SCHEDULE).replace("45 19 ", "46 19 ", 1)
        with self.assertRaises(RuntimeError):
            schedules.parse_managed_crontab(self.crontab([line]))

    def test_write_adds_new_schedule_and_preserves_other_lines(self):
        written = []
        command = {
            "id": "a" * 32, "action": "schedules", "operation": "write",
            "schedules": [{**SCHEDULE, "label": "Ny"}], "sha256": SHA_EMPTY,
        }
        with patch.object(schedules, "read_crontab", return_value=""), \
                patch.object(schedules, "write_crontab", side_effect=written.append):
            result = schedules.execute_schedules_command(command, "/dev/shm/dtmf_ctrl")
        self.assertTrue(result["ok"])
        self.assertIn(schedules.CRONTAB_START, written[0])
        self.assertEqual(result["schedules"][0]["label"], "Ny")

    def test_write_rejects_changed_crontab_and_foreign_target(self):
        command = {
            "id": "a" * 32, "action": "schedules", "operation": "write",
            "schedules": [], "sha256": "0" * 64,
        }
        with patch.object(schedules, "read_crontab", return_value="x\n"):
            with self.assertRaises(ValueError):
                schedules.execute_schedules_command(command, "/dev/shm/dtmf_ctrl")
        command.update(sha256=SHA_EMPTY, schedules=[{**SCHEDULE, "target": "/etc/passwd"}])
        with patch.object(schedules, "read_crontab", return_value=""):
            with self.assertRaises(ValueError):
                schedules.execute_schedules_command(command, "/dev/shm/dtmf_ctrl")

    def test_invalid_fields_are_rejected(self):
        for change in ({"dtmf": "12;rm"}, {"minute": "60"}, {"hour": "*/0"}):
            with self.assertRaises(ValueError):
                schedules.validate_schedules([{**SCHEDULE, **change}])


if __name__ == "__main__":
    unittest.main()
