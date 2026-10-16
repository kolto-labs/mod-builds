#!/usr/bin/env python3
"""Prove ModSync.rs peer-to-peer transfer on loopback with a random fixture."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import zipfile


def run_download(binary, magnet, output, sha256, peer, torrent_info, env):
    command = [str(binary), "torrent-download", magnet, str(output),
               "--sha256", sha256, "--peer", peer, "--no-proxy"]
    if torrent_info:
        command += ["--torrent-info", torrent_info]
    return subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=180)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--size-mb", type=int, default=3)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="p2p-loopback-"))
    env = {key: value for key, value in os.environ.items()
           if not key.lower().endswith("_proxy")}
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        env[name] = str(root / name)
        Path(env[name]).mkdir()

    archive = root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("payload/random.bin", os.urandom(args.size_mb * 1024 * 1024))
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"fixture {archive.stat().st_size} bytes sha256 {expected}", flush=True)

    seed_log = (root / "seed.stderr").open("wb")
    seed = subprocess.Popen([str(args.binary), "torrent-seed", str(archive), "--no-proxy"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=seed_log, text=True, env=env)
    try:
        ready, _, _ = select.select([seed.stdout], [], [], 60)
        line = seed.stdout.readline() if ready else ""
        if not line:
            raise SystemExit("torrent-seed printed no readiness line within 60 seconds")
        info = json.loads(line)
        magnet = info["magnet"]
        if info["sha256"] != expected:
            raise SystemExit("torrent-seed reported a different SHA-256 than the fixture")
        if not magnet.startswith("magnet:?") or ":" not in (info.get("peer") or ""):
            raise SystemExit(f"torrent-seed printed an unexpected line: {line.strip()}")
        peer = "127.0.0.1:" + info["peer"].rsplit(":", 1)[1]
        torrent_info = info.get("torrentInfo")
        print(f"seeding at {peer}", flush=True)

        output = root / "downloaded.zip"
        started = time.monotonic()
        result = run_download(args.binary, magnet, output, expected, peer, torrent_info, env)
        if result.returncode:
            print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
            raise SystemExit(f"torrent-download failed with exit {result.returncode}")
        subprocess.run(["cmp", str(archive), str(output)], check=True)
        print(f"downloaded and byte-identical in {time.monotonic() - started:.1f}s", flush=True)

        refused = root / "refused.zip"
        wrong = hashlib.sha256(b"not the fixture").hexdigest()
        result = run_download(args.binary, magnet, refused, wrong, peer, torrent_info, env)
        if result.returncode == 0 or refused.exists():
            print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
            raise SystemExit("a wrong --sha256 was accepted")
        print(f"wrong --sha256 refused with exit {result.returncode}", flush=True)
    finally:
        if seed.poll() is None:
            seed.send_signal(signal.SIGINT)
            try:
                seed.wait(timeout=20)
            except subprocess.TimeoutExpired:
                seed.kill()
                seed.wait()
        seed_log.close()
        tail = (root / "seed.stderr").read_text(errors="replace").splitlines()[-20:]
        if tail:
            print("::group::torrent-seed stderr (last 20 lines)")
            print("\n".join(tail))
            print("::endgroup::")


if __name__ == "__main__":
    main()
