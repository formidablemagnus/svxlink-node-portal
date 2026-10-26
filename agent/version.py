"""Read the agent version from its own VERSION file."""
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def read_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()[:64] or "okänd"
    except (OSError, UnicodeError):
        return "okänd"
