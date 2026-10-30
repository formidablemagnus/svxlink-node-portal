#!/usr/bin/env python3
"""Run a fixed SvxLink build workflow and install its package as root."""
from __future__ import annotations

import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import suppress
from typing import IO, NoReturn

BUILD_SCRIPT = "/usr/local/libexec/svxlink-node-portal-build-update"
BUILD_DIR = "/var/lib/svxlink-node-updater/svxlink/src/build"
UPDATER_USER = "svxlink-node-updater"
RUNUSER_BIN = "/usr/sbin/runuser"
PYTHON_BIN = "/usr/bin/python3"
DPKG_BIN = "/usr/bin/dpkg"
DPKG_DEB_BIN = "/usr/bin/dpkg-deb"
SYSTEMCTL_BIN = "/usr/bin/systemctl"
SYSTEMD_RUN_BIN = "/usr/bin/systemd-run"
BUILD_TIMEOUT_SECONDS = 3 * 60 * 60
INSTALL_TIMEOUT_SECONDS = 30 * 60
PACKAGE_PATTERN = re.compile(r"svxlink-[A-Za-z0-9.+~_-]{1,128}-Linux\.deb")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9.+:~_-]{1,128}")


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


CHANNEL_PATTERN = re.compile(r"prerelease|release|[0-9]+(?:\.[0-9]+){1,3}")


def read_request() -> str:
    try:
        request = json.loads(sys.stdin.buffer.read(4096).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Ogiltig begäran till uppdateringshjälpen: {exc}")
    if (
        not isinstance(request, dict)
        or set(request) != {"channel"}
        or not isinstance(request["channel"], str)
        or not CHANNEL_PATTERN.fullmatch(request["channel"])
    ):
        fail("Välj pre-release, senaste stabila release eller ett versionsnummer.")
    return request["channel"]


def copy_limited(source: IO[bytes], destination: IO[bytes], maximum: int) -> int:
    copied = 0
    while True:
        block = source.read(min(1024 * 1024, maximum + 1 - copied))
        if not block:
            return copied
        copied += len(block)
        if copied > maximum:
            fail("Paketfilen är större än tillåtet.")
        destination.write(block)


def run_build(channel: str) -> dict[str, str]:
    if not isinstance(channel, str) or not CHANNEL_PATTERN.fullmatch(channel):
        fail("Välj pre-release, senaste stabila release eller ett versionsnummer.")
    try:
        process = subprocess.run(
            [RUNUSER_BIN, "--user", UPDATER_USER, "--", BUILD_SCRIPT, channel],
            capture_output=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        fail("Tidsgränsen överskreds under SvxLink-bygget.")
    except OSError as exc:
        fail(f"Kunde inte starta byggprocessen: {exc}")
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        fail((detail or "SvxLink-bygget misslyckades.")[-1024:])
    try:
        result = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Byggscriptet returnerade ett ogiltigt svar: {exc}")
    if (
        not isinstance(result, dict)
        or set(result) != {"channel", "package", "version"}
        or result["channel"] != channel
        or not isinstance(result["package"], str)
        or not PACKAGE_PATTERN.fullmatch(result["package"])
        or not isinstance(result["version"], str)
        or not VERSION_PATTERN.fullmatch(result["version"])
    ):
        fail("Byggscriptet returnerade ogiltiga paketuppgifter.")
    return result


def install_package(package: dict[str, str]) -> None:
    if (
        set(package) != {"channel", "package", "version"}
        or not isinstance(package.get("channel"), str)
        or not CHANNEL_PATTERN.fullmatch(package["channel"])
        or not isinstance(package.get("package"), str)
        or not PACKAGE_PATTERN.fullmatch(package["package"])
        or not isinstance(package.get("version"), str)
        or not VERSION_PATTERN.fullmatch(package["version"])
    ):
        fail("Byggscriptet returnerade ogiltiga paketuppgifter.")
    if os.path.realpath(BUILD_DIR) != BUILD_DIR:
        fail("Byggkatalogen får inte innehålla symboliska länkar.")
    package_path = os.path.join(BUILD_DIR, package["package"])
    try:
        updater_uid = pwd.getpwnam(UPDATER_USER).pw_uid
        package_stat = os.lstat(package_path)
        if (
            not stat.S_ISREG(package_stat.st_mode)
            or package_stat.st_uid != updater_uid
            or package_stat.st_size <= 0
            or package_stat.st_size > 1024 * 1024 * 1024
        ):
            fail("Paketfilen har fel typ, ägare eller storlek.")
        package_fd = os.open(package_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        fail(f"Kunde inte öppna SvxLink-paketet: {exc}")

    temporary_path = None
    try:
        opened_stat = os.fstat(package_fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_ino != package_stat.st_ino
            or opened_stat.st_dev != package_stat.st_dev
            or opened_stat.st_uid != updater_uid
        ):
            fail("Paketfilen ändrades innan den kunde läsas.")
        with tempfile.NamedTemporaryFile(
            prefix="svxlink-update-", suffix=".deb", dir="/var/tmp", delete=False
        ) as package_file:
            temporary_path = package_file.name
            os.fchmod(package_file.fileno(), 0o600)
            with os.fdopen(os.dup(package_fd), "rb") as source:
                copied = copy_limited(source, package_file, 1024 * 1024 * 1024)
        final_stat = os.fstat(package_fd)
        if (
            copied != opened_stat.st_size
            or final_stat.st_size != opened_stat.st_size
            or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
            or final_stat.st_ctime_ns != opened_stat.st_ctime_ns
        ):
            fail("Paketet kopierades inte fullständigt.")

        package_name = subprocess.run(
            [DPKG_DEB_BIN, "--field", temporary_path, "Package"],
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
        if (
            package_name.returncode != 0
            or package_name.stdout.decode("utf-8", errors="replace").strip() != "svxlink"
        ):
            fail("Det byggda Debian-paketet är inte paketet svxlink.")

        package_version = subprocess.run(
            [DPKG_DEB_BIN, "--field", temporary_path, "Version"],
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
        version = package_version.stdout.decode("utf-8", errors="replace").strip()
        if (
            package_version.returncode != 0
            or not VERSION_PATTERN.fullmatch(version)
            or version != package["version"]
        ):
            fail("Paketets versionsnummer stämmer inte med byggresultatet.")

        install = subprocess.run(
            [DPKG_BIN, "--install", temporary_path],
            capture_output=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        if install.returncode != 0:
            detail = (
                install.stderr.decode("utf-8", errors="replace").strip()
                or install.stdout.decode("utf-8", errors="replace").strip()
            )
            fail((detail or "dpkg kunde inte installera SvxLink.")[-1024:])
    except subprocess.TimeoutExpired as exc:
        fail(f"Tidsgränsen överskreds vid paketinstallation: {exc.cmd[0]}")
    finally:
        os.close(package_fd)
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)

    try:
        restart = subprocess.run(
            [SYSTEMCTL_BIN, "restart", "svxlink.service"],
            capture_output=True,
            timeout=120,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        fail("Tidsgränsen överskreds när SvxLink-tjänsten startades om.")
    except OSError as exc:
        fail(f"Kunde inte starta om SvxLink-tjänsten: {exc}")
    if restart.returncode != 0:
        detail = restart.stderr.decode("utf-8", errors="replace").strip()
        fail((detail or "systemctl kunde inte starta om SvxLink.")[-1024:])


def run_update_service(channel: str) -> None:
    package = run_build(channel)
    install_package(package)
    print(json.dumps({
        "ok": True,
        "channel": channel,
        "version": package["version"],
    }))


def start_update_service(channel: str) -> None:
    if not isinstance(channel, str) or not CHANNEL_PATTERN.fullmatch(channel):
        fail("Välj pre-release, senaste stabila release eller ett versionsnummer.")
    try:
        process = subprocess.run(
            [
                SYSTEMD_RUN_BIN,
                "--quiet",
                "--system",
                "--wait",
                "--pipe",
                "--collect",
                f"--unit=svxlink-node-portal-update-{uuid.uuid4().hex}",
                "--property=RuntimeMaxSec=3h45min",
                PYTHON_BIN,
                __file__,
                "--run-update",
                channel,
            ],
            capture_output=True,
            timeout=3 * 60 * 60 + 50 * 60,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        fail("Tidsgränsen överskreds vid SvxLink-uppdateringen.")
    except OSError as exc:
        fail(f"Kunde inte starta uppdateringstjänsten: {exc}")
    if process.returncode != 0:
        detail = (
            process.stderr.decode("utf-8", errors="replace").strip()
            or process.stdout.decode("utf-8", errors="replace").strip()
        )
        fail((detail or "SvxLink-uppdateringen misslyckades.")[-1024:])
    sys.stdout.buffer.write(process.stdout)


def main() -> None:
    if len(sys.argv) == 1:
        start_update_service(read_request())
        return
    if (
        len(sys.argv) == 3
        and sys.argv[1] == "--run-update"
        and os.geteuid() == 0
        and CHANNEL_PATTERN.fullmatch(sys.argv[2])
    ):
        run_update_service(sys.argv[2])
        return
    fail("Ogiltig uppdateringsåtgärd.")


if __name__ == "__main__":
    main()
