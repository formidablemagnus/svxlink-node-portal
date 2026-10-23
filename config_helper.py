#!/usr/bin/env python3
"""Restricted privileged reader/writer for SvxLink node configuration files."""
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime

CONFIG_PATHS = {
    "svxlink.conf": "/etc/svxlink/svxlink.conf",
    "node_info.json": "/etc/svxlink/node_info.json",
}
MAX_CONFIG_BYTES = 2 * 1024 * 1024


def respond(data, status=0):
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    return status


def reject_json_constant(value):
    raise ValueError(f"Ogiltigt JSON-konstantvärde: {value}.")


def validate_document(document):
    if document not in CONFIG_PATHS:
        raise ValueError("Endast svxlink.conf och node_info.json kan ändras.")
    return CONFIG_PATHS[document]


def read_regular_file(path, document):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"{document} måste vara en vanlig fil, inte en länk.")
        with os.fdopen(file_descriptor, "rb") as config_file:
            file_descriptor = None
            content = config_file.read(MAX_CONFIG_BYTES + 1)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    if len(content) > MAX_CONFIG_BYTES:
        raise ValueError(f"{document} är större än 2 MB.")
    return content, file_stat


def read_document(document):
    path = validate_document(document)
    content, _ = read_regular_file(path, document)
    decoded = content.decode("utf-8")
    return {
        "ok": True,
        "document": document,
        "content": decoded,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def validate_json_document(content):
    try:
        json.loads(content, parse_constant=reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Ogiltig JSON på rad {exc.lineno}, kolumn {exc.colno}: {exc.msg}"
        ) from exc


def write_document(document, content, expected_sha256):
    path = validate_document(document)
    if not isinstance(content, str):
        raise ValueError(f"{document} måste vara text.")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise ValueError(f"Ogiltig kontrollsumma för {document}.")
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError(f"{document} måste vara mellan 1 byte och 2 MB.")
    if b"\x00" in encoded:
        raise ValueError(f"{document} får inte innehålla NUL-tecken.")
    if document == "node_info.json":
        validate_json_document(content)

    original, file_stat = read_regular_file(path, document)
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise ValueError(f"{document} har ändrats sedan den lästes. Läs om den innan du sparar.")

    directory = os.path.dirname(path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = f"{path}.bak.{timestamp}"
    backup_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(backup_fd, "wb") as backup_file:
            backup_file.write(original)
            backup_file.flush()
            os.fsync(backup_file.fileno())
        if os.name == "posix":
            os.chown(backup_path, file_stat.st_uid, file_stat.st_gid)
        os.chmod(backup_path, stat.S_IMODE(file_stat.st_mode))
    except Exception:
        os.unlink(backup_path)
        raise

    temp_fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(temp_fd, "wb") as temp_file:
            temp_file.write(encoded)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if os.name == "posix":
            os.chown(temp_path, file_stat.st_uid, file_stat.st_gid)
        os.chmod(temp_path, stat.S_IMODE(file_stat.st_mode))
        latest, _ = read_regular_file(path, document)
        if hashlib.sha256(latest).hexdigest() != expected_sha256:
            raise ValueError(f"{document} ändrades under sparningen. Läs om den innan du försöker igen.")
        os.replace(temp_path, path)
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise

    return {
        "ok": True,
        "document": document,
        "backup": backup_path,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def main():
    if os.geteuid() != 0:
        return respond({"ok": False, "error": "Hjälpprogrammet måste köras via sudo."}, 1)
    if len(sys.argv) != 1:
        return respond({"ok": False, "error": "Argument är inte tillåtna."}, 2)
    try:
        raw = sys.stdin.buffer.read(MAX_CONFIG_BYTES * 6 + 65536 + 1)
        if len(raw) > MAX_CONFIG_BYTES * 6 + 65536:
            raise ValueError("Begäran är större än tillåtet.")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Begäran måste vara ett JSON-objekt.")
        action = request.get("action")
        document = request.get("document")
        if action == "read" and set(request) == {"action", "document"}:
            return respond(read_document(document))
        if action == "write" and set(request) == {
            "action", "document", "content", "sha256"
        }:
            return respond(
                write_document(document, request["content"], request["sha256"])
            )
        raise ValueError("Ogiltig konfigurationsåtgärd.")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return respond({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
