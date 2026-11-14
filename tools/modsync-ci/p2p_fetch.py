#!/usr/bin/env python3
"""Fetch ONE mod archive from the P2P swarm through the Rust ModSync CLI.

Called by cache.py as an optional source. ``fetch`` never raises and returns True only
when a file whose SHA-256 matches the entry's expected hash was moved into place.

Honest limits:
  * A magnet (infohash) must be known in advance. The CLI cannot fetch by SHA-256 alone;
    the magnet, optional torrent_info and peers come from the guide-authored triple.
  * CI runners cannot accept inbound connections, so this is only reliable when a
    long-lived, reachable seeder is listed in ``p2p.peers`` as ``host:port``.
    Runner-to-runner transfer is not possible.
  * Only ``--no-proxy`` (DHT/direct) is used. The default public SOCKS5 proxy route is
    unsuitable for CI.
  * The SHA-256 is the trust anchor: the CLI is asked to verify it, and the output is
    independently re-hashed here before it is moved to its destination.

Entry shape (archive_selections / seed_pins style)::

    {"sha256": "<hex>", "p2p": {"magnet": "magnet:?xt=urn:btih:...",
                                "torrent_info": "<base64, optional>",
                                "peers": ["host:port", ...]}}
"""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

CI_BINARY = Path(".modsync-source/target/debug/modsync")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
PEER_PATTERN = re.compile(r"^[A-Za-z0-9._\-\[\]:]+:[0-9]{1,5}$")
DEFAULT_TIMEOUT = 150


def _block(entry):
    block = entry.get("p2p") if isinstance(entry, dict) else None
    return block if isinstance(block, dict) else None


def _expected_sha256(entry):
    value = entry.get("sha256") if isinstance(entry, dict) else None
    return value.lower() if isinstance(value, str) and SHA256_PATTERN.match(value) else None


def _peers(block):
    peers = block.get("peers") or []
    if not isinstance(peers, list):
        return []
    return [each for each in peers if isinstance(each, str) and not each.startswith("-") and PEER_PATTERN.match(each)]


def _configured(entry):
    block = _block(entry)
    magnet = block.get("magnet") if block else None
    return bool(isinstance(magnet, str) and magnet.startswith("magnet:") and _expected_sha256(entry))


def describe(entry):
    """Return 'p2p-configured' or 'p2p-unconfigured' for reports."""
    return "p2p-configured" if _configured(entry) else "p2p-unconfigured"


def resolve_binary(binary=None, environ=None):
    environ = os.environ if environ is None else environ
    for candidate in (binary, environ.get("MODSYNC_BINARY")):
        if candidate:
            return str(candidate)
    if CI_BINARY.is_file():
        return str(CI_BINARY)
    return None


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log(message):
    print("p2p_fetch: " + " ".join(str(message).split()), file=sys.stderr)


def fetch(entry, dest, *, binary=None, timeout=DEFAULT_TIMEOUT, run=subprocess.run):
    """Fetch the archive for ``entry`` to ``dest``; return True only on a verified move."""
    temp = None
    try:
        if not _configured(entry):
            return False
        program = resolve_binary(binary)
        if not program:
            return False
        block = _block(entry)
        expected = _expected_sha256(entry)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(f".{dest.name}.p2p-{uuid.uuid4().hex}.part")
        command = [program, "torrent-download", block["magnet"], str(temp),
                   "--sha256", expected, "--no-proxy"]
        torrent_info = block.get("torrent_info")
        if isinstance(torrent_info, str) and torrent_info:
            command += ["--torrent-info", torrent_info]
        for peer in _peers(block):
            command += ["--peer", peer]
        result = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            tail = (getattr(result, "stderr", "") or "").strip()[-120:]
            _log(f"{dest.name}: torrent-download exited {result.returncode} {tail}")
            temp.unlink(missing_ok=True)
            return False
        if not temp.is_file():
            _log(f"{dest.name}: torrent-download produced no output")
            return False
        actual = _sha256_of(temp)
        if actual != expected:
            _log(f"{dest.name}: sha256 mismatch, expected {expected[:12]} got {actual[:12]}")
            temp.unlink(missing_ok=True)
            return False
        os.replace(temp, dest)
        return True
    except subprocess.TimeoutExpired:
        _log(f"{Path(dest).name}: timed out after {timeout}s")
    except Exception as error:
        _log(f"{Path(dest).name}: {type(error).__name__}")
    if temp is not None:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return False
