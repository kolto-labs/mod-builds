"""Install official, checksum-pinned age binaries without package-manager hooks."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile

VERSION = "1.2.1"


def install(directory: Path, system: str | None = None, machine: str | None = None) -> Path:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in {"linux", "darwin", "windows"} or arch is None or (system == "windows" and arch != "amd64"):
        raise ValueError("Unsupported encryption tool platform")
    suffix = ".zip" if system == "windows" else ".tar.gz"
    asset = f"age-v{VERSION}-{system}-{arch}{suffix}"
    hashes = json.loads(Path(__file__).with_name("age-checksums.json").read_text())
    url = f"https://github.com/FiloSottile/age/releases/download/v{VERSION}/{asset}"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != hashes[asset]:
        raise ValueError("Encryption tool checksum verification failed")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Encryption tool directory cannot be a symbolic link")
    names = ["age.exe", "age-keygen.exe"] if system == "windows" else ["age", "age-keygen"]
    for name in names:
        member = "age/" + name
        if suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                payload = archive.read(member)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                info = archive.getmember(member)
                if not info.isfile() or info.size > 32 * 1024 * 1024:
                    raise ValueError("Unexpected encryption tool archive entry")
                stream = archive.extractfile(info)
                assert stream is not None
                payload = stream.read()
        destination = directory / name
        if destination.is_symlink():
            raise ValueError("Encryption tool target cannot be a symbolic link")
        destination.write_bytes(payload)
        if system != "windows":
            os.chmod(destination, 0o755)
    return directory / names[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        install(args.directory)
    except Exception:
        print("Encryption tool installation failed; no private workload was started.")
        return 1
    print(f"Verified encryption tool v{VERSION} is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
