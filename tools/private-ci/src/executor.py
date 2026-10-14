#!/usr/bin/env python3
"""Run a pinned private snapshot without publishing plaintext build output."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import zipfile


PLATFORMS = ("linux", "windows", "macos")
KINDS = ("cli", "gui")
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_EVIDENCE_FILE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_EVIDENCE_ENTRIES = 10_000
EVIDENCE_ROOT = "artifacts/evidence"
EVIDENCE_SUFFIXES = {".json", ".log", ".txt", ".md", ".png", ".toml", ".html"}
BASELINE_DIRECTORIES = {"steamapps", "games", "baseline", "modules", "override", "game"}
GAME_SUFFIXES = {".bif", ".rim", ".erf", ".mod", ".tlk", ".exe", ".dll", ".wav", ".mp3", ".tpc", ".tga", ".ncs", ".mdl", ".mdx", ".ssf", ".2da"}

# Build tools need their installed paths, not the runner's credentials or
# arbitrary workflow environment. HOME is retained unchanged; gh is pointed
# at a fresh, empty config directory for both download and build operations.
SAFE_ENVIRONMENT = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP", "TMPDIR",
    "LANG", "LANGUAGE", "LC_ALL", "SHELL", "TERM", "CI", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME",
    "ANDROID_HOME", "ANDROID_SDK_ROOT", "DOTNET_ROOT", "PNPM_HOME", "NVM_DIR",
    "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}


@dataclass(frozen=True)
class Config:
    repository: str
    revision: str
    command: str
    artifact_path: str
    timeout_seconds: int


def relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid relative path")
    if any(unicodedata.category(char) == "Cc" or char in '<>"|?*' for char in value) or value.startswith("/"):
        raise ValueError("Invalid relative path")
    parts = (value[:-1] if value.endswith("/") else value).split("/")
    devices = {"CON", "PRN", "AUX", "NUL"} | {prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"}
    if any(part in ("", ".", "..") or part.endswith((".", " ")) or part.split(".", 1)[0].rstrip(" .").upper() in devices for part in parts):
        raise ValueError("Invalid portable relative path")
    return "/".join(parts)


def canonical_path(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def parse_config(raw: str, platform: str, kind: str) -> Config:
    if platform not in PLATFORMS or kind not in KINDS:
        raise ValueError("Invalid execution lane")
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("Invalid configuration")
    repository = config.get("privateRepository")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if any(part in (".", "..") for part in repository.split("/")):
        raise ValueError("Invalid repository")
    revision = config.get("ref")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ValueError("Revision must be an immutable commit")
    commands = config.get("commands")
    lane = commands.get(platform) if isinstance(commands, dict) else None
    command = lane.get(kind) if isinstance(lane, dict) else None
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("Missing lane command")
    minutes = config.get("timeoutMinutes", 45)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 180:
        raise ValueError("Invalid timeout")
    artifacts = relative_path(config.get("artifactPath", EVIDENCE_ROOT))
    if artifacts != EVIDENCE_ROOT:
        raise ValueError("Evidence must use the dedicated receipts directory")
    return Config(repository, revision.lower(), command, artifacts, minutes * 60)


def build_environment(source: dict[str, str], config_dir: Path, revision: str) -> dict[str, str]:
    result = {key: value for key, value in source.items() if key.upper() in SAFE_ENVIRONMENT}
    result["GH_CONFIG_DIR"] = str(config_dir)
    if source.get("GITHUB_WORKSPACE"):
        result["PUBLIC_CI_WORKSPACE"] = source["GITHUB_WORKSPACE"]
    result["PRIVATE_CI_SOURCE_SHA"] = revision
    result["CI"] = "true"
    return result


def download_snapshot(config: Config, token: str, archive: Path, log: Path,
                      environment: dict[str, str]) -> None:
    # gh writes binary stdout directly to the file. No token-bearing git
    # configuration, remote URL, command argument, or credential file exists.
    download_env = dict(environment)
    download_env["GH_TOKEN"] = token
    with archive.open("wb") as output, log.open("ab") as errors:
        completed = subprocess.run(
            ["gh", "api", f"repos/{config.repository}/zipball/{config.revision}"],
            stdout=output, stderr=errors, env=download_env, timeout=180, check=False,
        )
    if completed.returncode or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Snapshot download failed")


def extract_snapshot(archive: Path, destination: Path) -> None:
    """Validate the complete ZIP manifest before extracting any entry."""
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if not entries or len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError("Invalid snapshot bounds")
        plans = []
        roots = set()
        names = set()
        for item in entries:
            # filename is platform-normalized and NUL-truncated by ZipInfo.
            # Reject ambiguous raw header names before those changes hide them.
            name = relative_path(item.orig_filename)
            parts = PurePosixPath(name).parts
            roots.add(parts[0])
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise ValueError("Snapshot links and special files are not allowed")
            if len(parts) == 1:
                if not item.is_dir():
                    raise ValueError("Snapshot must have one root directory")
                continue
            relative = "/".join(parts[1:])
            key = canonical_path(relative)
            if key in names:
                raise ValueError("Duplicate snapshot path")
            names.add(key)
            plans.append((item, destination.joinpath(*parts[1:])))
        if len(roots) != 1:
            raise ValueError("Snapshot must have one root directory")
        # File/directory collisions are rejected before any output is created.
        files = {canonical_path(target.relative_to(destination).as_posix())
                 for item, target in plans if not item.is_dir()}
        for _, target in plans:
            relative = target.relative_to(destination)
            if any(canonical_path(parent.as_posix()) in files for parent in relative.parents if str(parent) != "."):
                raise ValueError("Snapshot file/directory collision")
        destination.mkdir(parents=True)
        for item, target in plans:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as input_file, target.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, 1024 * 1024)
            if os.name != "nt":
                target.chmod(0o700 if (item.external_attr >> 16) & 0o111 else 0o600)


def stop_process_tree(process: subprocess.Popen, log) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"],
                           stdout=log, stderr=log, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # The shell may exit on SIGTERM while a descendant ignores it.
        # Kill remaining members of the owned process group in either case.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def run_command(config: Config, platform: str, workspace: Path, private: Path,
                environment: dict[str, str]) -> tuple[int, bool]:
    script = private / ("command.ps1" if platform == "windows" else "command.sh")
    prefix = "$ErrorActionPreference = 'Stop'\n$PSNativeCommandUseErrorActionPreference = $true\n" if platform == "windows" else ""
    script.write_text(prefix + config.command + "\n", encoding="utf-8")
    if platform == "windows":
        # PowerShell's process exit status must reflect a failed native build.
        with script.open("a", encoding="utf-8") as output:
            output.write("\nif ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE }\n")
        argv = ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)]
    else:
        argv = ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)]
    popen_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with (private / "build.log").open("wb") as log:
        process = subprocess.Popen(argv, cwd=workspace, env=environment,
                                   stdout=log, stderr=subprocess.STDOUT, **popen_options)
        try:
            code = process.wait(timeout=config.timeout_seconds)
            return code if 0 <= code <= 255 else (128 - code if code < 0 else 1), False
        except subprocess.TimeoutExpired:
            stop_process_tree(process, log)
            return 124, True
        except BaseException:
            # Cancellation must not leave the private recipe's owned children
            # running after TemporaryDirectory removes their logs and source.
            stop_process_tree(process, log)
            raise


def validate_trace_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_EVIDENCE_ENTRIES or sum(entry.file_size for entry in entries) > MAX_EVIDENCE_TOTAL_BYTES:
            raise ValueError("Trace exceeds evidence bounds")
        names = set()
        files = set()
        paths = []
        for entry in entries:
            # Apply the same raw-header and canonical collision policy to traces.
            name = relative_path(entry.orig_filename)
            key = canonical_path(name)
            if key in names:
                raise ValueError("Duplicate trace path")
            names.add(key)
            paths.append(PurePosixPath(name))
            if not entry.is_dir():
                files.add(key)
            parts = PurePosixPath(name).parts
            if any(part.casefold() in BASELINE_DIRECTORIES for part in parts[:-1]) or Path(name).suffix.casefold() in GAME_SUFFIXES:
                raise ValueError("Trace contains a prohibited baseline path or file type")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise ValueError("Trace links and special files are not allowed")
        for path in paths:
            if any(canonical_path(parent.as_posix()) in files for parent in path.parents if str(parent) != "."):
                raise ValueError("Trace file/directory collision")


def validate_artifact(item: Path, artifact: Path) -> None:
    parts = item.relative_to(artifact).parts
    directory_parts = parts if item.is_dir() else parts[:-1]
    if any(part.casefold() in BASELINE_DIRECTORIES for part in directory_parts):
        raise ValueError("Game baselines cannot be included in evidence")
    if item.is_file():
        if item.suffix.casefold() in EVIDENCE_SUFFIXES:
            return
        if item.name.casefold() == "trace.zip":
            validate_trace_archive(item)
            return
        raise ValueError("Only receipt, diagnostic, and screenshot evidence is allowed")


def bundle_evidence(private: Path, workspace: Path, config: Config, include_artifacts: bool = True) -> Path:
    archive = private / "evidence.tar.gz"
    artifact = workspace.joinpath(*PurePosixPath(config.artifact_path).parts)
    entries = []
    if include_artifacts and (artifact.exists() or artifact.is_symlink()):
        for item in [artifact, *artifact.rglob("*")]:
            if item.is_symlink() or not (item.is_file() or item.is_dir()):
                raise ValueError("Artifact links and special files are not allowed")
            item.resolve().relative_to(workspace.resolve())
            validate_artifact(item, artifact)
            entries.append(item)
    private_logs = [private / name for name in ("evidence.json", "build.log", "download.log") if (private / name).exists()]
    files = [item for item in entries if item.is_file()] + private_logs
    if len(entries) + len(private_logs) > MAX_EVIDENCE_ENTRIES or any(item.stat().st_size > MAX_EVIDENCE_FILE_BYTES for item in files) or sum(item.stat().st_size for item in files) > MAX_EVIDENCE_TOTAL_BYTES:
        raise ValueError("Evidence exceeds bounded artifact limits")
    with tarfile.open(archive, "w:gz") as output:
        for path in private_logs:
            output.add(path, arcname=path.name, recursive=False)
        for item in entries:
            output.add(item, arcname="artifacts/" + item.relative_to(artifact).as_posix(), recursive=False)
    return archive


def encrypt_evidence(age: Path, recipient: str, archive: Path, output: Path,
                     private: Path, environment: dict[str, str]) -> None:
    encrypted = output / "evidence.age"
    try:
        with (private / "encryption.log").open("wb") as log:
            result = subprocess.run([str(age), "--encrypt", "--recipient", recipient,
                                     "--output", str(encrypted), str(archive)],
                                    stdout=log, stderr=log, env=environment, timeout=180, check=False)
    except BaseException:
        encrypted.unlink(missing_ok=True)
        raise
    if result.returncode or not encrypted.is_file() or not encrypted.stat().st_size:
        encrypted.unlink(missing_ok=True)
        raise RuntimeError("Encryption failed")


def execute(platform: str, kind: str, output: Path, age: Path,
            source_environment: dict[str, str] | None = None) -> int:
    source_environment = dict(os.environ if source_environment is None else source_environment)
    config = parse_config(source_environment.get("PRIVATE_CI_CONFIG", ""), platform, kind)
    token = source_environment.get("PRIVATE_CI_TOKEN", "")
    recipient = source_environment.get("PRIVATE_CI_RECIPIENT", "")
    if not token or not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
        raise ValueError("Missing private execution credentials")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name != "evidence.age" or not path.is_file() for path in output.iterdir()):
        raise ValueError("Output directory must contain ciphertext only")
    (output / "evidence.age").unlink(missing_ok=True)
    age = age.resolve(strict=True)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="private-ci-") as directory:
        private = Path(directory)
        if os.name != "nt":
            private.chmod(0o700)
        config_dir = private / "empty-gh-config"
        config_dir.mkdir()
        environment = build_environment(source_environment, config_dir, config.revision)
        workspace = private / "source"
        code, timed_out, failure = 1, False, None
        try:
            download_snapshot(config, token, private / "source.zip", private / "download.log", environment)
            extract_snapshot(private / "source.zip", workspace)
            code, timed_out = run_command(config, platform, workspace, private, environment)
        except Exception as error:
            failure = str(error)
        evidence = {"schemaVersion": 1, "privateRepository": config.repository,
                    "sourceRevision": config.revision, "platform": platform, "kind": kind,
                    "exitCode": code, "timedOut": timed_out, "failure": failure,
                    "durationSeconds": round(time.time() - started, 3),
                    "limitations": "Public job metadata remains public. Source snapshot contains no git credentials or history."}
        (private / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        try:
            archive = bundle_evidence(private, workspace, config)
        except Exception as error:
            # Invalid artifact paths must not prevent retaining the private logs.
            code = code or 1
            evidence.update(exitCode=code, artifactFailure=str(error))
            (private / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            archive = bundle_evidence(private, workspace, config, include_artifacts=False)
        encrypt_evidence(age, recipient, archive, output, private, environment)
        return code


def main(argv: list[str] | None = None) -> int:
    class QuietParser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError("Invalid invocation")

    parser = QuietParser(description=__doc__)
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--age", type=Path, required=True)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def cancel_execution(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, cancel_execution)
    try:
        args = parser.parse_args(argv)
        code = execute(args.platform, args.kind, args.output, args.age)
    except KeyboardInterrupt:
        print("Private CI execution cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print("Private CI execution failed.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    if code:
        print("Private CI execution failed; encrypted evidence retained.", file=sys.stderr)
    else:
        print("Private CI execution completed.")
    return code


if __name__ == "__main__":
    sys.exit(main())
