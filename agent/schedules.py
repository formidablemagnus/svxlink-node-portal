"""Hantering av schemalagda DTMF-kommandon i svxlink-användarens crontab.

Formatet är kompatibelt med SvxLink Webpanel: schemaläggningarna ligger i ett
markerat block och varje rad bär sina värden som base64-kodad JSON.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

SUDO_BIN = "/usr/bin/sudo"
CRONTAB_BIN = "/usr/bin/crontab"
CRONTAB_USER = "svxlink"
CRONTAB_START = "# BEGIN SVXLINK-WEBPANEL MANAGED SCHEDULES"
CRONTAB_END = "# END SVXLINK-WEBPANEL MANAGED SCHEDULES"
# SvxLink exposes the default DTMF control PTY at this shared-memory path.
DEFAULT_DTMF_TARGET = "/dev/shm/dtmf_ctrl"  # noqa: S108
MAX_SCHEDULES = 100
MAX_CRONTAB_BYTES = 1024 * 1024
FIELDS = (
    ("minute", "minut", 0, 59),
    ("hour", "timme", 0, 23),
    ("day", "dag i månaden", 1, 31),
    ("month", "månad", 1, 12),
    ("weekday", "veckodag", 0, 7),
)
TARGET_PATTERN = re.compile(r"/[A-Za-z0-9_./-]{1,200}")
LINE_PATTERN = re.compile(
    r"\S+ \S+ \S+ \S+ \S+ printf '\\%s\\n' '[0-9*#]+' > (\S+) "
    r"# SVXLINK-WEBPANEL:([A-Za-z0-9_=-]+)"
)


def validate_cron_field(value: str, field: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > 20:
        raise ValueError(f"Cron-fältet {field} måste vara 1–20 tecken.")
    for part in value.split(","):
        match = re.fullmatch(r"(\*|([0-9]+)(?:-([0-9]+))?)(?:/([0-9]+))?", part)
        if not match:
            raise ValueError(
                f"Ogiltigt cron-fält för {field}. Använd *, värde, intervall, lista eller steg."
            )
        if match.group(4) is not None and int(match.group(4)) < 1:
            raise ValueError(f"Cron-steget för {field} måste vara minst 1.")
        if match.group(2) is None:
            continue
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) is not None else start
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"Cron-fältet {field} måste ligga mellan {minimum} och {maximum}.")


def validate_target(target: object) -> str:
    if not isinstance(target, str) or not TARGET_PATTERN.fullmatch(target):
        raise ValueError("Ogiltig DTMF-sökväg för schemaläggningen.")
    return target


def validate_schedules(
    schedules: object, default_target: str = DEFAULT_DTMF_TARGET
) -> list[dict[str, str]]:
    if not isinstance(schedules, list) or len(schedules) > MAX_SCHEDULES:
        raise ValueError(f"Ange högst {MAX_SCHEDULES} schemaläggningar.")
    clean = []
    for schedule in schedules:
        if not isinstance(schedule, dict):
            raise ValueError("Ogiltig schemaläggning.")
        label = schedule.get("label", "")
        dtmf = schedule.get("dtmf", "")
        if not isinstance(label, str) or len(label.strip()) > 40 or "\n" in label or "\r" in label:
            raise ValueError("Schemanamnet får innehålla högst 40 tecken.")
        if not isinstance(dtmf, str) or not 1 <= len(dtmf) <= 20 or not re.fullmatch(r"[0-9*#]+", dtmf):
            raise ValueError("DTMF-koden måste innehålla 1–20 tecken: 0–9, * eller #.")
        entry = {"label": label.strip(), "dtmf": dtmf}
        for key, field, minimum, maximum in FIELDS:
            value: Any = schedule.get(key)
            validate_cron_field(value, field, minimum, maximum)
            entry[key] = value
        entry["target"] = validate_target(schedule.get("target", default_target))
        clean.append(entry)
    return clean


def cron_job_line(schedule: Mapping[str, str]) -> str:
    metadata = {key: schedule[key] for key in ("label", "dtmf", *(f[0] for f in FIELDS))}
    encoded = base64.urlsafe_b64encode(
        json.dumps(metadata, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).decode("ascii")
    expression = " ".join(schedule[field[0]] for field in FIELDS)
    command = f"printf '\\%s\\n' '{schedule['dtmf']}' > {schedule['target']}"
    return f"{expression} {command} # SVXLINK-WEBPANEL:{encoded}"


def parse_managed_crontab(
    content: str,
) -> tuple[list[dict[str, str]], list[str], tuple[int, int] | None]:
    lines = content.splitlines()
    starts = [i for i, line in enumerate(lines) if line == CRONTAB_START]
    ends = [i for i, line in enumerate(lines) if line == CRONTAB_END]
    if not starts and not ends:
        return [], lines, None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise RuntimeError("Panelens crontab-markörer saknas eller är felaktiga; crontaben ändrades inte.")
    start, end = starts[0], ends[0]
    schedules = []
    for line in lines[start + 1:end]:
        if not line.strip():
            continue
        match = LINE_PATTERN.fullmatch(line)
        if not match:
            raise RuntimeError("Panelen hittade en okänd rad i sitt crontab-block; crontaben ändrades inte.")
        try:
            metadata = json.loads(base64.urlsafe_b64decode(match.group(2).encode("ascii")).decode("ascii"))
            if not isinstance(metadata, dict):
                raise ValueError("Ogiltiga schemadata.")
            metadata.pop("target", None)
            schedule = validate_schedules([{**metadata, "target": match.group(1)}])[0]
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(
                "Panelens crontab-block innehåller ogiltiga schemadata; crontaben ändrades inte."
            ) from exc
        if cron_job_line(schedule).partition(" # SVXLINK-WEBPANEL:")[0] != line.partition(" # SVXLINK-WEBPANEL:")[0]:
            raise RuntimeError("En schemalagd rad har ändrats manuellt; crontaben ändrades inte.")
        schedules.append(schedule)
    return schedules, lines[:start] + lines[end + 1:], (start, end)


def checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def response_data(content: str, default_target: str) -> dict[str, object]:
    schedules, other_lines, _ = parse_managed_crontab(content)
    return {
        "ok": True,
        "schedules": schedules,
        "other_lines": [line for line in other_lines if line.strip()],
        "cron_target": default_target,
        "sha256": checksum(content),
    }


def run_crontab(
    arguments: Sequence[str], input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    if tuple(arguments) not in (("-l",), ("-",), ("-r",)):
        raise ValueError("Ogiltiga crontab-argument.")
    try:
        return subprocess.run(  # noqa: S603
            ["sudo", "-n", CRONTAB_BIN, "-u", CRONTAB_USER, *arguments],  # noqa: S607
            input=input_text, capture_output=True, text=True, timeout=15, check=False,
            encoding="utf-8", env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            shell=False, executable=SUDO_BIN,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Kommandot crontab eller sudo saknas på noden.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tidsgränsen överskreds vid crontab-åtgärden.") from exc
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Kunde inte köra crontab: {exc}") from exc


def read_crontab() -> str:
    result = run_crontab(["-l"])
    if result.returncode != 0:
        if result.returncode == 1 and "no crontab for" in (result.stderr or "").lower():
            return ""
        raise RuntimeError(
            "Kunde inte läsa crontab: " + (result.stderr or "crontab -l misslyckades.").strip()[:500]
        )
    if len(result.stdout.encode("utf-8")) > MAX_CRONTAB_BYTES:
        raise RuntimeError("Crontaben är större än 1 MB och kan inte hanteras av portalen.")
    return result.stdout


def write_crontab(updated: str) -> None:
    result = run_crontab(["-"] if updated else ["-r"], updated if updated else None)
    if result.returncode != 0:
        if not updated and "no crontab for" in (result.stderr or "").lower():
            return
        details = (result.stderr or result.stdout or "crontab misslyckades.").strip()
        raise RuntimeError("Kunde inte spara crontab: " + details[:500])


def build_crontab(content: str, clean: list[dict[str, str]]) -> str:
    _, _, managed_range = parse_managed_crontab(content)
    lines = content.splitlines()
    if managed_range is not None:
        start, end = managed_range
        lines = lines[:start] + lines[end + 1:]
    if clean:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [CRONTAB_START, *(cron_job_line(s) for s in clean), CRONTAB_END]
    updated = "\n".join(lines).rstrip("\n")
    return updated + "\n" if updated else ""


def execute_schedules_command(
    command: dict[str, object], default_target: str
) -> dict[str, object]:
    """Utför läs- eller skrivåtgärd. default_target används för nya rader."""
    validate_target(default_target)
    operation = command.get("operation")
    if operation == "read" and set(command) == {"id", "action", "operation"}:
        return response_data(read_crontab(), default_target)
    if operation != "write" or set(command) != {"id", "action", "operation", "schedules", "sha256"}:
        raise ValueError("Ogiltig schemaläggningsåtgärd från portalen.")
    expected = command["sha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Ogiltig kontrollsumma för crontab.")
    clean = validate_schedules(command["schedules"], default_target)
    content = read_crontab()
    if checksum(content) != expected:
        raise ValueError("Crontaben har ändrats sedan den lästes. Läs om den innan du sparar.")
    allowed = {default_target, *(s["target"] for s in parse_managed_crontab(content)[0])}
    if any(schedule["target"] not in allowed for schedule in clean):
        raise ValueError("Schemaläggningen använder en DTMF-sökväg som inte är tillåten.")
    updated = build_crontab(content, clean)
    if updated != content:
        write_crontab(updated)
    return response_data(updated, default_target)
