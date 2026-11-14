#!/usr/bin/env python3
"""Prove ModSync.rs peer-to-peer transfer on loopback with a random fixture.

A byte-identical output alone does not prove a transfer: it says nothing about where the
bytes came from. So the fixture is minted into a torrent once, then shared by a seeder
running under that authored magnet, which reports how many bytes it uploaded when it
stops. Each download must be matched by the seeder uploading at least the whole
fixture, and the wrong-hash download must fail on the SHA-256 check after a real
transfer, not for any other reason.
"""

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

# High enough that the cap never slows a few MiB on loopback.
UPLOAD_KIBPS = "1048576"


def run_download(binary, magnet, output, sha256, peer, env):
    command = [str(binary), "torrent-download", magnet, str(output),
               "--sha256", sha256, "--peer", peer, "--no-proxy"]
    return subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=180)


def stop(process):
    """SIGINT the process and return what it printed on stdout afterwards."""
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        remaining, _ = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        remaining, _ = process.communicate()
        raise SystemExit("torrent-seed did not stop within 30 seconds of SIGINT")
    return remaining or ""


def start_seed(binary, args, log, env):
    seed = subprocess.Popen([str(binary), "torrent-seed", *args, "--no-proxy"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=log, text=True, env=env)
    ready, _, _ = select.select([seed.stdout], [], [], 60)
    line = seed.stdout.readline() if ready else ""
    if not line:
        stop(seed)
        raise SystemExit("torrent-seed printed no readiness line within 60 seconds")
    return seed, json.loads(line)


class AuthoredSeed:
    """Shares the fixture under the minted magnet with no ratio or time limit."""

    def __init__(self, binary, archive, magnet, expected, log, env):
        self.process, info = start_seed(
            binary, [str(archive), "--authored-magnet", magnet, "--share-ratio", "0",
                     "--share-hours", "0", "--upload-kibps", UPLOAD_KIBPS], log, env)
        if info.get("magnet") != magnet or info.get("sha256") != expected:
            stop(self.process)
            raise SystemExit(f"authored torrent-seed printed an unexpected line: {info}")
        if ":" not in (info.get("peer") or ""):
            stop(self.process)
            raise SystemExit(f"authored torrent-seed is not listening: {info}")
        self.peer = "127.0.0.1:" + info["peer"].rsplit(":", 1)[1]

    def uploaded(self):
        """Stop sharing and return the bytes the seeder reports it uploaded."""
        lines = [line for line in stop(self.process).splitlines() if line.strip()]
        if self.process.returncode:
            raise SystemExit(f"authored torrent-seed exited {self.process.returncode}: {lines}")
        outcome = json.loads(lines[-1]) if lines else {}
        if outcome.get("outcome") != "stopped" or outcome.get("reason") != "cancelled":
            raise SystemExit(f"authored torrent-seed ended unexpectedly: {outcome}")
        return int(outcome["uploadedBytes"])

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


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
    size = archive.stat().st_size
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"fixture {size} bytes sha256 {expected}", flush=True)

    seed_log_path = root / "seed.stderr"
    seed_log = seed_log_path.open("wb")
    seed = None
    try:
        # Mint the torrent, then stop: only the authored seeder below may serve it.
        minted, info = start_seed(args.binary, [str(archive)], seed_log, env)
        stop(minted)
        magnet = info.get("magnet") or ""
        if info.get("sha256") != expected:
            raise SystemExit("torrent-seed reported a different SHA-256 than the fixture")
        if not magnet.startswith("magnet:?"):
            raise SystemExit(f"torrent-seed printed an unexpected line: {info}")

        seed = AuthoredSeed(args.binary, archive, magnet, expected, seed_log, env)
        print(f"seeding at {seed.peer}", flush=True)
        output = root / "downloaded.zip"
        started = time.monotonic()
        result = run_download(args.binary, magnet, output, expected, seed.peer, env)
        elapsed = time.monotonic() - started
        if result.returncode:
            print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
            raise SystemExit(f"torrent-download failed with exit {result.returncode}")
        subprocess.run(["cmp", str(archive), str(output)], check=True)
        uploaded = seed.uploaded()
        if uploaded < size:
            raise SystemExit(f"the seeder uploaded {uploaded} of {size} bytes; "
                             "the download did not come from the peer")
        print(f"downloaded and byte-identical in {elapsed:.1f}s; "
              f"seeder uploaded {uploaded} bytes", flush=True)

        seed = AuthoredSeed(args.binary, archive, magnet, expected, seed_log, env)
        refused = root / "refused.zip"
        wrong = hashlib.sha256(b"not the fixture").hexdigest()
        result = run_download(args.binary, magnet, refused, wrong, seed.peer, env)
        uploaded = seed.uploaded()
        if result.returncode == 0 or refused.exists():
            print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
            raise SystemExit("a wrong --sha256 was accepted")
        if "SHA-256 mismatch" not in result.stderr:
            print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
            raise SystemExit(f"torrent-download exited {result.returncode} "
                             "but not because of the SHA-256 check")
        if uploaded < size:
            raise SystemExit(f"the seeder uploaded {uploaded} of {size} bytes; "
                             "the wrong --sha256 was refused without a transfer")
        print(f"wrong --sha256 refused after a {uploaded}-byte transfer", flush=True)
    finally:
        if seed is not None:
            seed.kill()
        seed_log.close()
        tail = seed_log_path.read_text(errors="replace").splitlines()[-20:]
        if tail:
            print("::group::torrent-seed stderr (last 20 lines)")
            print("\n".join(tail))
            print("::endgroup::")


if __name__ == "__main__":
    main()
