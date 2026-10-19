#!/usr/bin/env python3
"""Acquire owned Windows KOTOR depots privately with Valve's SteamCMD.

Linux runners need 32-bit libc/libstdc++; macOS acquisition is not supported.
Steam Guard may require an initial interactive authorization or a fresh code.
No login/ownership failure is bypassed and no game executable is launched.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile

APPS = {"k1": 32370, "k2": 208580}
BOOTSTRAPS = {
    "linux": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz",
    "windows": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip",
}
MAX_BOOTSTRAP = 64 * 1024 * 1024
LOGIN_RESULTS = {
    "InvalidPassword": "invalid_login",
    "LoggedInElsewhere": "invalid_login",
    "AccountLogonDenied": "guard_challenge",
    "RateLimitExceeded": "rate_limit",
    "TryAnotherCM": "service_unavailable",
    "ServiceUnavailable": "service_unavailable",
    "InvalidLoginAuthCode": "guard_challenge",
}


class SteamProcessError(RuntimeError):
    def __init__(self, returncode: int, reason: str):
        allowed = {"guard_challenge", "confirmation_required", "invalid_login",
                   "no_subscription", "dependency", "output_capture_failed",
                   "timeout", "failed", "no_cached_credentials"}
        self.returncode = int(returncode)
        self.reason = reason if reason in LOGIN_RESULTS or reason in allowed else "failed"
        self.category = LOGIN_RESULTS.get(self.reason, self.reason)
        super().__init__(f"SteamCMD: {self.reason} (returncode={self.returncode})")


def token(value: str, *, path: bool = False) -> str:
    # Steam's command parser has no portable, documented arbitrary-string escape.
    # Refuse ambiguous values instead of interpolating an extra command.
    if not value or any(ord(c) < 32 or ord(c) == 127 or c == '"' or (c == "\\" and not path) for c in value):
        raise ValueError("Unsupported SteamCMD command value")
    return '"' + value + '"'


def script(game: str, directory: Path, username: str, password: str,
           guard: str | None = None) -> str:
    if game not in APPS:
        raise ValueError("Unsupported game")
    login = "login " + token(username)
    if password:
        login += " " + token(password)
    if guard:
        if not re.fullmatch(r"[A-Za-z0-9]{5}", guard):
            raise ValueError("Invalid Steam Guard code")
        login += " " + token(guard)
    # Valve documents native Windows backslashes for force_install_dir. They are
    # admitted only in a locally generated path, never arbitrary credentials.
    install = token(str(directory.resolve()), path=True)
    return ("@ShutdownOnFailedCommand 1\n@NoPromptForPassword 1\n"
            "@sSteamCmdForcePlatformType windows\n"
            f"force_install_dir {install}\n{login}\n"
            f"app_update {APPS[game]} validate\nquit\n")


def archive_path(value: str, root: Path) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or any(p in ("..", "") for p in path.parts):
        raise ValueError("Unsafe SteamCMD bootstrap entry")
    return root.joinpath(*path.parts)


def bootstrap(host: str, root: Path) -> Path:
    if host not in BOOTSTRAPS:
        raise ValueError("SteamCMD acquisition requires Linux or Windows")
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    archive = root / "bootstrap.download"
    with urllib.request.urlopen(BOOTSTRAPS[host], timeout=60) as response, archive.open("xb") as output:
        if not response.url.startswith("https://steamcdn-a.akamaihd.net/"):
            raise RuntimeError("Unexpected SteamCMD bootstrap endpoint")
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_BOOTSTRAP:
                raise RuntimeError("SteamCMD bootstrap exceeds bounds")
            output.write(chunk)
    if host == "linux":
        with tarfile.open(archive, "r:gz") as source:
            entries = source.getmembers()
            if len(entries) > 1000 or sum(e.size for e in entries) > MAX_BOOTSTRAP * 4:
                raise ValueError("SteamCMD bootstrap exceeds bounds")
            for entry in entries:
                archive_path(entry.name, root)
                if not (entry.isfile() or entry.isdir()):
                    raise ValueError("Unsupported SteamCMD bootstrap entry")
            source.extractall(root, filter="data")
        command = root / "steamcmd.sh"
    else:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) > 1000 or sum(e.file_size for e in entries) > MAX_BOOTSTRAP * 4:
                raise ValueError("SteamCMD bootstrap exceeds bounds")
            for entry in entries:
                archive_path(entry.filename, root)
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Unsupported SteamCMD bootstrap entry")
            source.extractall(root)
        command = root / "steamcmd.exe"
    archive.unlink()
    if not command.is_file():
        raise RuntimeError("SteamCMD bootstrap is incomplete")
    if host == "linux":
        command.chmod(0o700)
    return command


def verify_install(game: str, directory: Path, steam_root: Path) -> None:
    app_id = APPS[game]
    manifests = [directory / "steamapps" / f"appmanifest_{app_id}.acf",
                 steam_root / "steamapps" / f"appmanifest_{app_id}.acf"]
    manifest = next((p for p in manifests if p.is_file()), None)
    if manifest is None or manifest.stat().st_size > 1024 * 1024:
        raise RuntimeError("Steam installation manifest missing")
    text = manifest.read_text(encoding="utf-8")
    fields = re.findall(r'"([^"\r\n]+)"\s*"([^"\r\n]*)"', text)
    def field(name: str) -> str:
        values = [value for key, value in fields if key.lower() == name.lower()]
        if len(values) != 1:
            raise RuntimeError("Invalid Steam installation manifest")
        return values[0]
    if field("appid") != str(app_id) or field("StateFlags") != "4":
        raise RuntimeError("Steam did not finish installing the owned app")
    # Manifest metadata alone is not a usable installer baseline.
    for name in ("chitin.key", "dialog.tlk", "swkotor.exe" if game == "k1" else "swkotor2.exe"):
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Steam game resources are incomplete")
    if not (directory / "modules").is_dir() or not any((directory / "modules").iterdir()):
        raise RuntimeError("Steam game modules are incomplete")


def run_steam(command: Path, script_path: Path, log, environment: dict[str, str], timeout: int) -> int:
    import threading
    import time

    markers = (
        *((name, rb"\b" + name.encode("ascii") + rb"\b") for name in LOGIN_RESULTS),
        ("dependency", rb"(?:libc\.so\.6|libstdc\+\+\.so\.6|ld-linux\.so\.2)[^\r\n]{0,160}(?:cannot open shared object|not found|no such file)|(?:missing|requires?|install)[^\r\n]{0,80}32.bit[^\r\n]{0,80}(?:libraries|libs|libc|libstdc)|error while loading shared libraries|linux32/steamcmd[^\r\n]{0,80}(?:not found|cannot execute|required file not found)"),
        ("guard_challenge", rb"(?:enter|provide|input|type)[^\r\n]{0,100}(?:steam guard|two.factor|authenticator|authentication|access code)"),
        ("guard_challenge", rb"(?:enter|provide|input|type)[^\r\n]{0,60}\bcode\b|(?:steam guard|two.factor|authenticator) code\s*:"),
        ("guard_challenge", rb"(?:steam guard|two.factor|authenticator)[^\r\n]{0,100}(?:code required|enter.*code|waiting for|approval required)"),
        ("confirmation_required", rb"(?:please\s+)?(?:confirm|approve)[^\r\n]{0,100}(?:login|log.on|sign.in|steam mobile|steam app)"),
        ("confirmation_required", rb"waiting for[^\r\n]{0,100}(?:confirmation|approval)"),
        ("invalid_login", rb"invalid (?:password|login|credentials)|invalidpassword|accountlogondenied|incorrect (?:password|login|credentials)|login failure|logon failure"),
        ("no_subscription", rb"no subscription|nosubscription|no licenses|missing subscription"),
        ("no_cached_credentials", b"no cached credentials"),
    )
    patterns = [(category, re.compile(pattern, re.IGNORECASE)) for category, pattern in markers]
    login_success = re.compile(
        rb"Logging in[^\r\n]* to Steam Public\.\.\.OK|Waiting for user info\.\.\.OK|Logged in OK|Update state \([^\r\n]+\)[^\r\n]*progress:",
        re.IGNORECASE,
    )
    confirmation_started = None
    ready = threading.Event()
    finished = threading.Event()
    failures = []
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True, "umask": 0o077}
    process = subprocess.Popen([str(command), "+runscript", str(script_path)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               bufsize=0,
                               cwd=command.parent, env=environment, **options)

    def capture() -> None:
        nonlocal confirmation_started
        pending = b""
        try:
            while chunk := process.stdout.read(4096):
                log.write(chunk)
                log.flush()
                pending = (pending + chunk)[-8192:]
                if not failures:
                    for category, pattern in patterns:
                        if pattern.search(pending):
                            if category == "confirmation_required":
                                if confirmation_started is None:
                                    confirmation_started = time.monotonic()
                                pending = pending[pattern.search(pending).end():]
                                ready.set()
                                continue
                            failures.append(category)
                            ready.set()
                            break
                if login_success.search(pending):
                    confirmation_started = None
                    pending = b""
                    ready.set()
        except Exception:
            failures.append("output_capture_failed")
            ready.set()
        finally:
            process.stdout.close()
            finished.set()
            ready.set()

    reader = threading.Thread(target=capture, daemon=True)
    try:
        reader.start()
        deadline = time.monotonic() + timeout
        while True:
            if failures:
                raise SteamProcessError(process.poll() or -1, failures[0])
            code = process.poll()
            if code is not None and finished.is_set():
                if confirmation_started is not None:
                    raise SteamProcessError(code, "confirmation_required")
                if code:
                    raise SteamProcessError(code, "dependency" if code in (126, 127) else "failed")
                return code
            remaining = deadline - time.monotonic()
            if confirmation_started is not None:
                remaining = min(remaining, confirmation_started + 300 - time.monotonic())
            if remaining <= 0:
                raise SteamProcessError(process.poll() or -1,
                                        "confirmation_required" if confirmation_started is not None else "timeout")
            ready.wait(min(0.1, remaining))
            ready.clear()
    except BaseException as error:
        # Never leave a credential-bearing Steam child running after timeout/interrupt.
        if os.name == "nt":
            subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"],
                           stdout=log, stderr=log, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()
        if reader.ident is not None:
            reader.join(timeout=2)
        if isinstance(error, SteamProcessError):
            raise SteamProcessError(process.returncode, error.reason) from None
        raise


def acquire(workspace: Path, log_path: Path, host: str, environment: dict[str, str],
            timeout: int = 3600) -> dict[str, Path]:
    if host not in BOOTSTRAPS or timeout <= 0 or not environment.get("STEAM_USERNAME"):
        raise ValueError("Unsupported acquisition options or missing credentials")
    if not environment.get("STEAM_PASSWORD") and not environment.get("STEAM_CONFIG_VDF"):
        raise ValueError("A password or saved Steam configuration is required")
    games = {}
    private_directory(log_path.parent)
    private_write(log_path, b"Steam diagnostics are suppressed; see per-game reports.\n")
    for game in APPS:
        destination = workspace / "games" / game
        acquire_game(game, destination, workspace / "steam-state",
                     workspace / f"{game}-acquisition.json", environment, timeout,
                     host=host)
        games[game] = destination
    return games


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Private directory cannot be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def private_write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def inventory(directory: Path) -> dict:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Baseline contains a symlink")
        if path.is_file() and "steamapps" not in path.relative_to(directory).parts:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            result[path.relative_to(directory).as_posix()] = {
                "size": path.stat().st_size, "sha256": digest.hexdigest()}
    return result


def acquire_game(game: str, destination: Path, state_dir: Path, report: Path,
                 environment: dict[str, str], timeout: int = 3600,
                 *, host: str | None = None) -> dict:
    host = host or {"Linux": "linux", "Windows": "windows"}.get(platform.system(), "macos")
    if game not in APPS or host not in BOOTSTRAPS or timeout <= 0:
        raise ValueError("Unsupported acquisition options")
    username = environment.get("STEAM_USERNAME", "")
    password = environment.get("STEAM_PASSWORD", "")
    if not username:
        raise ValueError("Steam acquisition requires STEAM_USERNAME")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination must not exist")
    if state_dir.is_symlink():
        raise ValueError("Private state cannot be a symlink")
    destination = destination.resolve()
    # Keep the caller's portable spelling on Windows; resolving through the
    # Win32 short-name alias makes mocked/private environment paths compare
    # differently even though they refer to the same directory.
    state_dir = Path(os.path.abspath(state_dir))
    if state_dir == destination or state_dir in destination.parents or destination in state_dir.parents:
        raise ValueError("State and destination must be independent")
    private_directory(state_dir)
    report = report.absolute()
    if report == destination or destination in report.parents:
        raise ValueError("Report must be outside destination")
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists() or report.is_symlink():
        raise ValueError("Report must not exist")
    lock = state_dir / "acquisition.lock"
    lock.mkdir(mode=0o700)
    outcome = {"game": game, "app_id": APPS[game], "platform": "windows", "success": False,
               "used_saved_state": False, "password_fallback_used": False}
    try:
        baseline = state_dir / f"baseline-{game}"
        receipt = state_dir / f"baseline-{game}.json"
        steam_home = state_dir / "home"
        private_directory(steam_home)
        root = steam_home / "steamcmd"
        if root.exists():
            private_directory(root)
        encoded = environment.get("STEAM_CONFIG_VDF")
        used_saved_state = bool(encoded or (root / "config/config.vdf").is_file())
        outcome["used_saved_state"] = used_saved_state
        if not password and not used_saved_state:
            raise ValueError("A password or saved Steam configuration is required")
        script_content = script(game, baseline, username,
                                "" if used_saved_state else password,
                                None if used_saved_state else environment.get("STEAM_GUARD_CODE"))
        command = root / ("steamcmd.sh" if host == "linux" else "steamcmd.exe")
        if not command.is_file():
            command = bootstrap(host, root)
        if encoded:
            config = root / "config"
            private_directory(config)
            target = config / "config.vdf"
            decoded = base64.b64decode(encoded, validate=True)
            if not decoded or len(decoded) > 16 * 1024 * 1024:
                raise ValueError("Invalid saved Steam configuration")
            temporary_config = config / "config.restore"
            private_write(temporary_config, decoded)
            temporary_config.replace(target)
        if baseline.exists():
            if not receipt.is_file() or inventory(baseline) != json.loads(receipt.read_text()):
                raise RuntimeError("Cached baseline differs from its acquisition manifest")
        else:
            baseline.mkdir(mode=0o700)
        clean_env = {key: value for key, value in environment.items()
                     if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG"}}
        clean_env.update(HOME=str(steam_home), USERPROFILE=str(steam_home),
                         TMPDIR=str(steam_home), TEMP=str(steam_home), TMP=str(steam_home))
        deadline = time.monotonic() + timeout
        for attempt in range(2):
            with tempfile.TemporaryDirectory(prefix="request-", dir=state_dir) as temporary:
                script_path = Path(temporary) / "acquire.script"
                private_write(script_path, script_content.encode("utf-8"))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SteamProcessError(-1, "timeout")
                try:
                    with open(os.devnull, "wb") as discarded:
                        code = run_steam(command, script_path, discarded, clean_env, remaining)
                    if code:
                        raise SteamProcessError(code, "dependency" if code in (126, 127) else "failed")
                except SteamProcessError as error:
                    if (attempt != 0 or not used_saved_state or not password
                            or error.reason != "no_cached_credentials"):
                        raise
                    script_content = script(game, baseline, username, password,
                                            environment.get("STEAM_GUARD_CODE"))
                    outcome["password_fallback_used"] = True
                    continue
                break
        verify_install(game, baseline, root)
        files = inventory(baseline)
        receipt.unlink(missing_ok=True)
        private_write(receipt, json.dumps(files, sort_keys=True).encode())
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="steam-copy-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "game"
            shutil.copytree(baseline, staged, ignore=shutil.ignore_patterns("steamapps"))
            if inventory(staged) != files:
                raise RuntimeError("Workspace copy verification failed")
            staged.rename(destination)
        outcome.update(success=True, files=files)
    except Exception:
        outcome["error"] = "Acquisition failed; check ownership, Steam Guard, disk space and private state."
        raise
    finally:
        try:
            private_write(report, (json.dumps(outcome, indent=2, sort_keys=True) + "\n").encode())
            descriptor = os.open(state_dir / "events.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as log:
                os.chmod(state_dir / "events.log", 0o600)
                log.write(f"{game}: {'verified' if outcome['success'] else 'failed'}\n")
        finally:
            lock.rmdir()
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--private-log", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    host = {"Linux": "linux", "Windows": "windows"}.get(platform.system(), "macos")
    try:
        acquire(args.workspace, args.private_log, host, dict(os.environ), args.timeout)
    except Exception:
        print("Steam acquisition failed. Inspect private diagnostics; ownership and Steam Guard authorization are required.")
        return 1
    print("Owned Steam game resources acquired privately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
