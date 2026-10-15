#!/usr/bin/env python3
"""Install the checksum-pinned official rclone binary for Linux amd64."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import zipfile


VERSION = "1.74.3"
ARCHIVE = f"rclone-v{VERSION}-linux-amd64.zip"
URL = f"https://downloads.rclone.org/v{VERSION}/{ARCHIVE}"
FALLBACK_URL = f"https://github.com/rclone/rclone/releases/download/v{VERSION}/{ARCHIVE}"
SHA256 = "dbee7ccd7a5d617e4ed4cd4555c16669b511abfe8d31164f61be35ac9e999bd2"
MEMBER = f"rclone-v{VERSION}-linux-amd64/rclone"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 600


def install(directory):
    if platform.system() != "Linux" or platform.machine().lower() not in (
        "x86_64", "amd64"
    ):
        raise ValueError("Only Linux amd64 is supported")
    directory = directory.expanduser().resolve()
    if any(ord(character) < 32 or ord(character) == 127 for character in str(directory)):
        raise ValueError("Directory must not contain control characters")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rclone-bootstrap-", dir=directory) as scratch:
        archive_path = Path(scratch) / ARCHIVE
        download_urls = (URL, FALLBACK_URL, FALLBACK_URL)
        previous_url = None
        for attempt, url in enumerate(download_urls):
            if url != previous_url:
                archive_path.unlink(missing_ok=True)
            previous_url = url
            download = subprocess.run(
                [
                    "curl", "-q", "--fail", "--silent", "--show-error",
                    "--location", "--proto", "=https", "--proto-redir", "=https",
                    "--connect-timeout", "60", "--max-time", str(DOWNLOAD_TIMEOUT_SECONDS),
                    "--continue-at", "-",
                    "--max-filesize", str(MAX_ARCHIVE_BYTES),
                    "--output", str(archive_path), url,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=DOWNLOAD_TIMEOUT_SECONDS + 10,
            )
            if download.returncode == 0:
                break
            if attempt == len(download_urls) - 1:
                raise ValueError("Official archive download failed")
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds size limit")
        digest = hashlib.sha256()
        with archive_path.open("rb") as downloaded:
            for chunk in iter(lambda: downloaded.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != SHA256:
            raise ValueError("Archive SHA256 mismatch")
        candidate = Path(scratch) / "rclone"
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
                raise ValueError("Unpacked archive exceeds size limit")
            expected = [member for member in members if member.filename == MEMBER]
            if len(expected) != 1 or expected[0].is_dir():
                raise ValueError("Expected rclone member missing or duplicated")
            member_mode = expected[0].external_attr >> 16
            if stat.S_IFMT(member_mode) not in (0, stat.S_IFREG):
                raise ValueError("Expected rclone member is not a regular file")
            if archive.testzip() is not None:
                raise ValueError("Archive CRC mismatch")
            with archive.open(expected[0]) as source, candidate.open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
        candidate.chmod(0o755)
        result = subprocess.run(
            [str(candidate), "--config", "/dev/null", "version"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        if result.stdout.splitlines()[:1] != [f"rclone v{VERSION}"]:
            raise ValueError("Installed rclone version mismatch")
        installed = directory / "rclone"
        os.replace(candidate, installed)
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with open(github_path, "a", encoding="utf-8") as path_file:
            path_file.write(str(directory) + "\n")
    print(f"{installed}\nrclone v{VERSION}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        install(arguments.directory)
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile,
            RuntimeError, EOFError):
        print("rclone bootstrap failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
