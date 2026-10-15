#!/usr/bin/env python3
"""Prepare a private SteamCMD home and optionally bootstrap saved auth state."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


REPOSITORY = "kolto-labs/mod-builds"
HOME_MARKER = ".modsync-steam-home"
HOME_MARKER_CONTENT = b"modsync-private-steamcmd-home-v1\n"
LOGIN_OK = re.compile(
    rb"Logging in[^\r\n]* to Steam Public\.\.\.OK|Waiting for user info\.\.\.OK|Logged in OK",
    re.IGNORECASE,
)
LOGIN_OUTPUT_LIMIT = 256 * 1024
SYSTEM_ENVIRONMENT = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG"}
GH_ENVIRONMENT = SYSTEM_ENVIRONMENT | {
    "HOME", "USERPROFILE", "GH_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "XDG_CONFIG_HOME",
}
WARM_DIAGNOSTICS = (
    ("steam-update-check", re.compile(rb"checking for (?:available )?updates|checking for update", re.I)),
    ("steam-update-download", re.compile(rb"(?:download|downloading)[^\r\n]{0,100}(?:steamcmd|client|package|update)|(?:steamcmd|client|package|update)[^\r\n]{0,100}(?:download|downloading)", re.I)),
    ("steam-update-validation", re.compile(rb"(?:validat|extract|unpack|install)[^\r\n]{0,100}(?:steamcmd|client|package|update)|(?:steamcmd|client|package|update)[^\r\n]{0,100}(?:validat|extract|unpack|install)", re.I)),
    ("steam-network-connect", re.compile(rb"(?:connect|connection|resolve|dns)[^\r\n]{0,100}(?:fail|error|timeout|refused|unavailable)|(?:fail|error|timeout|refused)[^\r\n]{0,100}(?:connect|connection|resolve|dns)", re.I)),
    ("linux-runtime-dependency", re.compile(rb"(?:error while loading shared libraries|cannot open shared object file|wrong ELF class|steamcmd[^\r\n]{0,100}(?:not found|cannot execute))", re.I)),
    ("steam-update-complete", re.compile(rb"update complete|install complete|verifying installation", re.I)),
    ("steamcmd-client-started", re.compile(rb"Steam Console Client \(c\) Valve Corporation|Loading Steam API\.\.\.OK", re.I)),
    ("steamcmd-quit-completed", re.compile(rb"Steam>\s*quit|\+quit", re.I)),
)


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("helper-unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def prompt_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        value = getpass.getpass(f"{name}: ")
    if not value:
        raise ValueError("missing-credential")
    if name == "STEAM_USERNAME" and value.strip().casefold() == "anonymous":
        raise ValueError("anonymous-account-rejected")
    return value


def private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


class BoundedMemoryLog:
    def __init__(self, limit: int = LOGIN_OUTPUT_LIMIT):
        self.limit = limit
        self.data = bytearray()

    def write(self, content: bytes) -> int:
        self.data.extend(content)
        if len(self.data) > self.limit:
            del self.data[:-self.limit]
        return len(content)

    def flush(self) -> None:
        pass

    def contains_login_success(self) -> bool:
        return LOGIN_OK.search(self.data) is not None

    def clear(self) -> None:
        self.data.clear()


def verify_real_path(path: Path, *, directory: bool) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError("unsafe-linked-path")
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise OSError("unsafe-path")
        if current == absolute:
            expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
            if not expected:
                raise OSError("invalid-path-type")


def private_home_is_valid(home: Path) -> None:
    verify_real_path(home, directory=True)
    if home.is_symlink():
        raise OSError("unsafe-private-home")


def make_child_environment(home: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in SYSTEM_ENVIRONMENT
    }
    environment.update(
        HOME=str(home), USERPROFILE=str(home), TMPDIR=str(home), TEMP=str(home), TMP=str(home),
    )
    return environment


def prepare(repo: Path, private_home: Path):
    if repo.is_symlink() or not repo.is_dir():
        raise OSError("invalid-repository-path")
    os.umask(0o077)
    private_home = private_home.expanduser().absolute()
    if private_home.exists() or private_home.is_symlink():
        if private_home.is_symlink() or not private_home.is_dir():
            raise OSError("invalid-private-home")
        marker = private_home / HOME_MARKER
        if marker.exists() or marker.is_symlink():
            verify_real_path(marker, directory=False)
            if marker.read_bytes() != HOME_MARKER_CONTENT:
                raise OSError("invalid-private-home-marker")
        elif any(private_home.iterdir()):
            raise OSError("unmarked-private-home")
    else:
        private_home.mkdir(mode=0o700, parents=True)
    os.chmod(private_home, 0o700)
    verify_real_path(private_home, directory=True)
    marker = private_home / HOME_MARKER
    if not marker.exists():
        private_file(marker, HOME_MARKER_CONTENT)

    steam = load_module("steam_acquire", repo / "tools/private-ci/src/steam_acquire.py")
    steam_root = private_home / "steamcmd"
    if steam_root.exists() or steam_root.is_symlink():
        if steam_root.is_symlink() or not steam_root.is_dir():
            raise OSError("invalid-steamcmd-root")
        verify_real_path(steam_root, directory=True)
    command_name = "steamcmd.sh" if platform.system() == "Linux" else "steamcmd.exe"
    command = steam_root / command_name
    if not command.is_file():
        command = steam.bootstrap("linux" if platform.system() == "Linux" else "windows", steam_root)
    verify_real_path(steam_root, directory=True)
    verify_real_path(command, directory=False)
    if command.parent != steam_root:
        raise OSError("invalid-steamcmd-command")

    child_environment = make_child_environment(private_home)
    warm_script = steam_root / "warm.script"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    descriptor, warm_log_name = tempfile.mkstemp(
        prefix=f"warm-{timestamp}-{os.getpid()}-", suffix=".log", dir=steam_root,
    )
    warm_log = Path(warm_log_name)
    os.fchmod(descriptor, 0o600)
    private_file(warm_script, b"quit\n")
    succeeded = False
    try:
        with os.fdopen(descriptor, "wb") as log:
            steam.run_steam(command, warm_script, log, child_environment, 600)
        succeeded = True
    finally:
        warm_script.unlink(missing_ok=True)
        if succeeded:
            warm_log.unlink(missing_ok=True)
    if not succeeded:
        raise RuntimeError("steamcmd-warm-failed")
    return private_home, steam_root, command, steam, child_environment


def warm_diagnostic(private_home: Path) -> tuple[str, Path | None]:
    steam_root = private_home / "steamcmd"
    logs = sorted(steam_root.glob("warm-*.log"), key=lambda path: path.stat().st_mtime_ns)
    if not logs:
        return "no-private-warm-log", None
    log_path = logs[-1]
    verify_real_path(log_path, directory=False)
    with log_path.open("rb") as source:
        source.seek(max(0, log_path.stat().st_size - 256 * 1024))
        content = source.read()
    for label, pattern in WARM_DIAGNOSTICS:
        if pattern.search(content):
            return label, log_path
    return "no-allowlisted-startup-marker", log_path


def alert_ready(sound: Path | None) -> None:
    subprocess.run(
        ["notify-send", "--urgency=critical", "SteamCMD ready",
         "Paste a fresh five-character Steam Guard code in the waiting terminal."],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5,
    )
    if sound is not None:
        verify_real_path(sound, directory=False)
        subprocess.run(
            ["pw-play", str(sound)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=15,
        )


def run(repo: Path, private_home: Path, sound: Path | None) -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty() or not Path("/dev/tty").exists():
        raise OSError("interactive-terminal-required")
    home, steam_root, command, steam, child_environment = prepare(repo, private_home)
    username = prompt_secret("STEAM_USERNAME")
    password = prompt_secret("STEAM_PASSWORD")
    auth = load_module("steam_auth", repo / "tools/modsync-ci/steam_auth.py")

    guard = os.environ.get("STEAM_GUARD_CODE", "")
    if not guard:
        print("SteamCMD is ready. A fresh five-character Steam Guard code is needed now.", flush=True)
        alert_ready(sound)
        print("\a", end="", flush=True)
        guard = getpass.getpass("Fresh Steam Guard code: ")
    if not re.fullmatch(r"[A-Za-z0-9]{5}", guard):
        raise ValueError("invalid-steam-guard-code")

    login_script = steam_root / "login.script"
    login_command = (
        "login " + steam.token(username) + " " + steam.token(password) + " " + steam.token(guard)
    )
    private_output = BoundedMemoryLog()
    private_file(
        login_script,
        ("@ShutdownOnFailedCommand 1\n@NoPromptForPassword 1\n" + login_command + "\nquit\n").encode(),
    )
    try:
        private_home_is_valid(home)
        verify_real_path(steam_root, directory=True)
        verify_real_path(command, directory=False)
        steam.run_steam(command, login_script, private_output, child_environment, 180)
        if not private_output.contains_login_success():
            raise RuntimeError("steam-login-unverified")
    finally:
        private_output.clear()
        login_script.unlink(missing_ok=True)

    encoded = auth.encode_secret(auth.capture(home))
    gh_environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in GH_ENVIRONMENT
    }
    subprocess.run(
        ["gh", "variable", "set", "STEAM_AUTH_READY", "--body", "false", "-R", REPOSITORY],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=gh_environment, check=True, timeout=60,
    )
    subprocess.run(
        ["gh", "secret", "set", "STEAM_CONFIG_VDF_GZIP", "-R", REPOSITORY],
        input=encoded.encode("ascii"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=gh_environment, check=True, timeout=60,
    )
    print("SteamCMD login verified and auth state saved to kolto-labs/mod-builds; scheduled acquisition remains disabled.")


def failure_category(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return "github-cli-failed" if error.cmd and error.cmd[0] == "gh" else "steamcmd-failed"
    if isinstance(error, subprocess.TimeoutExpired):
        return "operation-timeout"
    if error.__class__.__name__ == "SteamProcessError":
        return "steamcmd-" + getattr(error, "category", "failed")
    if isinstance(error, PermissionError):
        return "permission-denied"
    if isinstance(error, FileNotFoundError):
        return "required-tool-or-file-missing"
    if isinstance(error, ValueError):
        return str(error) if re.fullmatch(r"[a-z0-9-]{1,48}", str(error)) else "invalid-input"
    if isinstance(error, OSError):
        return str(error) if re.fullmatch(r"[a-z0-9-]{1,48}", str(error)) else "local-io-failed"
    if isinstance(error, RuntimeError):
        return str(error) if re.fullmatch(r"[a-z0-9-]{1,48}", str(error)) else "operation-failed"
    return "unexpected-error"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Explicit local mod-builds checkout")
    parser.add_argument("--private-home", type=Path, required=True, help="Private isolated SteamCMD home")
    parser.add_argument("--prepare-only", action="store_true", help="Warm private SteamCMD without credentials or authentication")
    parser.add_argument("--alert-sound", type=Path, help="Optional local WAV played when the fresh-code prompt is ready")
    args = parser.parse_args()
    try:
        if args.prepare_only:
            try:
                prepare(args.repo, args.private_home)
            except Exception:
                category, log_path = warm_diagnostic(args.private_home.expanduser().absolute())
                suffix = f"; private warm log retained at {log_path}" if log_path else ""
                print(f"Prepare-only failed [{category}]{suffix}.", file=sys.stderr)
                return 1
            print("Private SteamCMD client is prepared and warm; no account authentication was attempted.")
        else:
            run(args.repo, args.private_home, args.alert_sound)
    except Exception as error:
        print("Local SteamCMD bootstrap failed [" + failure_category(error) + "]; credentials and private output suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
