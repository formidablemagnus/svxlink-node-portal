"""Read the application version from the VERSION file next to the portal directory."""
from __future__ import annotations

from pathlib import Path

VERSION_FILE: Path = Path(__file__).resolve().parent.parent / "VERSION"


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()[:64] or "okänd"
    except (OSError, UnicodeError):
        return "okänd"
