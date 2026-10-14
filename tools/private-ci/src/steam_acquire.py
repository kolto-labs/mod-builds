#!/usr/bin/env python3
"""Acquire owned Windows KOTOR depots privately with Valve's SteamCMD.

Linux runners need 32-bit libc/libstdc++; macOS acquisition is not supported.
Steam Guard may require an initial interactive authorization or a fresh code.
No login/ownership failure is bypassed and no game executable is launched.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

APPS = {"k1": 32370, "k2": 208580}
BOOTSTRAPS = {
    "linux": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz",
    "windows": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip",
}
MAX_BOOTSTRAP = 64 * 1024 * 1024


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
    login = "login " + token(username) + " " + token(password)
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
    for name in ("chitin.key", "dialog.tlk"):
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Steam game resources are incomplete")
    if not (directory / "modules").is_dir() or not any((directory / "modules").iterdir()):
        raise RuntimeError("Steam game modules are incomplete")


def run_steam(command: Path, script_path: Path, log, environment: dict[str, str], timeout: int) -> int:
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen([str(command), "+runscript", str(script_path)],
                               stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                               cwd=command.parent, env=environment, **options)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
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
        raise


def acquire(workspace: Path, log_path: Path, host: str, environment: dict[str, str],
            timeout: int = 3600) -> dict[str, Path]:
    username, password = environment.get("STEAM_USERNAME"), environment.get("STEAM_PASSWORD")
    if not username or not password:
        raise ValueError("Steam acquisition requires STEAM_USERNAME and STEAM_PASSWORD")
    if host not in BOOTSTRAPS:
        raise ValueError("SteamCMD acquisition requires Linux or Windows")
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
    games = {game: workspace / "games" / game for game in APPS}
    # Validate every script before any network activity.
    # The first login authorizes this private SteamCMD cache. Reuse that cache for
    # the second app; a short-lived Guard code may expire during the first download.
    scripts = {game: script(game, path, username, password,
                           environment.get("STEAM_GUARD_CODE") if index == 0 else None)
               for index, (game, path) in enumerate(games.items())}
    if any(path.exists() for path in games.values()):
        raise ValueError("Steam acquisition requires fresh game directories")
    log_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # O_EXCL refuses symlink/log reuse; never send Steam output to public stdout.
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    clean_env = {key: value for key, value in environment.items()
                 if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG"}}
    with os.fdopen(log_fd, "wb") as log, tempfile.TemporaryDirectory(prefix="steam-private-", dir=workspace) as temporary:
        private = Path(temporary)
        # Steam authentication caches and stdout are private ephemeral inputs.
        clean_env.update(HOME=str(private), USERPROFILE=str(private), TMPDIR=str(private))
        command = bootstrap(host, private / "steamcmd")
        for game, path in games.items():
            path.mkdir(parents=True, mode=0o700)
            script_path = private / "acquire.script"
            fd = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                    output.write(scripts[game])
                code = run_steam(command, script_path, log, clean_env, timeout)
            finally:
                script_path.unlink(missing_ok=True)
            if code:
                raise RuntimeError("Steam acquisition failed; inspect the private log")
            verify_install(game, path, command.parent)
    return games


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--private-log", required=True, type=Path)
    args = parser.parse_args()
    host = {"Linux": "linux", "Windows": "windows"}.get(platform.system(), "macos")
    try:
        acquire(args.workspace, args.private_log, host, dict(os.environ))
    except Exception:
        print("Steam acquisition failed. Inspect private diagnostics; ownership and Steam Guard authorization are required.")
        return 1
    print("Owned Steam game resources acquired privately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
