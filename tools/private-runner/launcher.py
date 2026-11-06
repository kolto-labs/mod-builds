#!/usr/bin/env python3
"""Run a configured trusted private snapshot without printing its output."""
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


def launch(env, run=subprocess.run):
    repo, rev, token, entry = (env.get(k, "") for k in
        ("SOURCE_REPOSITORY", "SOURCE_REVISION", "SOURCE_TOKEN", "SOURCE_ENTRYPOINT"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", rev) or not token:
        raise ValueError("configuration unavailable")
    parts = PurePosixPath(entry).parts
    if not parts or entry.startswith("/") or "\\" in entry or any(p in (".", "..") for p in parts):
        raise ValueError("configuration unavailable")
    with tempfile.TemporaryDirectory(prefix="private-run-", dir=env.get("RUNNER_TEMP")) as temp:
        root = Path(temp)
        os.chmod(root, 0o700)
        archive, source, log = root / "snapshot.zip", root / "source", root / "private.log"
        source.mkdir(mode=0o700)
        fetch_env = {k: env[k] for k in ("PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE") if k in env}
        fetch_env.update(HOME=temp, GH_TOKEN=token)
        with archive.open("xb") as out:
            result = run(["gh", "api", f"repos/{repo}/zipball/{rev}"], env=fetch_env,
                         stdout=out, stderr=subprocess.DEVNULL, timeout=180, check=False)
        if result.returncode or archive.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("snapshot unavailable")
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if not entries or len(entries) > 100000 or sum(i.file_size for i in entries) > 4 * 1024**3:
                raise ValueError("invalid snapshot")
            roots, seen = set(), set()
            for item in entries:
                name = item.filename.rstrip("/")
                fields = name.split("/")
                mode = item.external_attr >> 16
                if "\\" in name or any(p in ("", ".", "..") for p in fields) or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ValueError("invalid snapshot")
                roots.add(fields[0])
                target = "/".join(fields[1:])
                if target and target in seen:
                    raise ValueError("invalid snapshot")
                if target:
                    seen.add(target)
            if len(roots) != 1:
                raise ValueError("invalid snapshot")
            for item in entries:
                target = source.joinpath(*item.filename.rstrip("/").split("/")[1:])
                if target == source:
                    continue
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(item) as src, target.open("xb") as dst:
                        while chunk := src.read(1024 * 1024):
                            dst.write(chunk)
        program = source.joinpath(*parts)
        if not program.is_file() or program.is_symlink():
            raise ValueError("entrypoint unavailable")
        child_env = {k: env[k] for k in ("PATH", "RUNNER_TEMP") if k in env}
        child_env["HOME"] = temp
        with log.open("xb") as output:
            result = run([sys.executable, str(program)], cwd=source, env=child_env,
                         stdout=output, stderr=subprocess.STDOUT, timeout=21600, check=False)
        recipient = env.get("EVIDENCE_RECIPIENT", "")
        if recipient:
            destination = Path(env["RUNNER_TEMP"]) / "evidence.age"
            with destination.open("xb") as encrypted:
                result_age = run(["age", "-r", recipient, str(log)], stdout=encrypted,
                                 stderr=subprocess.DEVNULL, timeout=120, check=False)
            if result_age.returncode:
                destination.unlink(missing_ok=True)
                raise ValueError("evidence unavailable")
        if result.returncode:
            raise ValueError("private execution failed")


if __name__ == "__main__":
    try:
        launch(os.environ)
    except Exception:
        print("Private execution could not be completed.", file=sys.stderr)
        sys.exit(1)
    print("Private execution completed.")
