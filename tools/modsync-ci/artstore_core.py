#!/usr/bin/env python3
"""Content-addressed blobs split into independently sealed parts, plus the index that tracks them."""

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


MAGIC = b"MODSYNC-ARTSTORE-1\n"
KDF_LABEL = b"artstore"
SALT_SIZE = 32
MAC_SIZE = 32
MIN_CIPHERTEXT = 32
CHUNK = 1024 * 1024
PBKDF2_ROUNDS = 200000
KEY_ENV = "CACHE_ENCRYPTION_KEY"
PART_SIZE_DEFAULT = 256 * 1024 * 1024
INDEX_SCHEMA = 1
PACK_PREFIX = "artstore-pack-"
INDEX_PREFIX = "artstore-index-"
DAY_SECONDS = 86400
PART_SUFFIX = ".part"


def _keys(salt, key_env):
    secret = os.environ.get(key_env, "")
    if len(secret.encode()) < 32:
        raise ValueError("%s must contain at least 32 bytes" % key_env)
    master = hashlib.pbkdf2_hmac("sha256", secret.encode(), KDF_LABEL + b"\x00" + salt,
                                 PBKDF2_ROUNDS)
    return (hmac.digest(master, KDF_LABEL + b"/encryption", "sha256").hex(),
            hmac.digest(master, KDF_LABEL + b"/authentication", "sha256"))


def _openssl(password, incoming, outgoing, key_env, decrypt=False):
    environment = dict(os.environ)
    environment.pop(key_env, None)
    environment.pop(KEY_ENV, None)
    environment["MODSYNC_ARTSTORE_PASSWORD"] = password
    command = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(PBKDF2_ROUNDS),
               "-md", "sha256", "-pass", "env:MODSYNC_ARTSTORE_PASSWORD"]
    if decrypt:
        command.append("-d")
    return subprocess.Popen(command, stdin=incoming, stdout=outgoing,
                            stderr=subprocess.DEVNULL, env=environment)


def _pump(process, chunks):
    try:
        try:
            for chunk in chunks:
                process.stdin.write(chunk)
            process.stdin.close()
        except BrokenPipeError:
            pass
        return process.wait()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass


def _file_chunks(handle):
    for chunk in iter(lambda: handle.read(CHUNK), b""):
        yield chunk


def _read_exact(handle, length):
    while length:
        chunk = handle.read(min(CHUNK, length))
        if not chunk:
            raise ValueError("Truncated part")
        length -= len(chunk)
        yield chunk


def _seal_chunks(chunks, directory, name, key_env):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(SALT_SIZE)
    password, authentication = _keys(salt, key_env)
    header = MAGIC + salt
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".artstore-")
    try:
        with os.fdopen(descriptor, "w+b") as envelope:
            envelope.write(header)
            envelope.flush()
            process = _openssl(password, subprocess.PIPE, envelope, key_env)
            if _pump(process, chunks) != 0:
                raise ValueError("Encryption failed")
            mac = hmac.new(authentication, header, "sha256")
            digest = hashlib.sha256(header)
            envelope.seek(len(header))
            for chunk in _file_chunks(envelope):
                mac.update(chunk)
                digest.update(chunk)
            tag = mac.digest()
            digest.update(tag)
            envelope.seek(0, os.SEEK_END)
            envelope.write(tag)
            envelope.flush()
            os.fsync(envelope.fileno())
            stored_size = envelope.tell()
        stored_sha256 = digest.hexdigest()
        final = directory / (name or stored_sha256 + PART_SUFFIX)
        os.replace(temporary, final)
        return {"stored_sha256": stored_sha256, "stored_size": stored_size}
    finally:
        Path(temporary).unlink(missing_ok=True)


def seal_part(src, dst, key_env=KEY_ENV):
    destination = Path(dst)
    with Path(src).open("rb") as handle:
        return _seal_chunks(_file_chunks(handle), destination.parent, destination.name, key_env)


def _decrypt_to(source, destination, key_env):
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = len(MAGIC) + SALT_SIZE
    with source.open("rb") as envelope:
        header = envelope.read(prefix)
        if len(header) != prefix or not header.startswith(MAGIC):
            raise ValueError("Invalid part")
        password, authentication = _keys(header[len(MAGIC):], key_env)
        length = os.fstat(envelope.fileno()).st_size - prefix - MAC_SIZE
        if length < MIN_CIPHERTEXT or length % 16:
            raise ValueError("Truncated part")
        mac = hmac.new(authentication, header, "sha256")
        for chunk in _read_exact(envelope, length):
            mac.update(chunk)
        tag = envelope.read(MAC_SIZE)
        if not hmac.compare_digest(mac.digest(), tag) or envelope.read(1):
            raise ValueError("Part authentication failed")
        envelope.seek(prefix)
        recheck = hmac.new(authentication, header, "sha256")

        def authenticated():
            for chunk in _read_exact(envelope, length):
                recheck.update(chunk)
                yield chunk

        descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".artstore-")
        try:
            with os.fdopen(descriptor, "wb", buffering=0) as plaintext:
                process = _openssl(password, subprocess.PIPE, plaintext, key_env, decrypt=True)
                status = _pump(process, authenticated())
                if not hmac.compare_digest(recheck.digest(), tag):
                    raise ValueError("Part changed during decryption")
                if status != 0:
                    raise ValueError("Decryption failed")
                os.fsync(plaintext.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)


def open_part(src, dst, key_env=KEY_ENV):
    _decrypt_to(src, dst, key_env)


def split_and_seal(path, outdir, part_size=PART_SIZE_DEFAULT, key_env=KEY_ENV):
    source = Path(path)
    if source.suffix.lower() == ".bik":
        raise ValueError("Refusing to store .bik files")
    if not source.is_file():
        raise ValueError("Source must be a regular file")
    if not isinstance(part_size, int) or isinstance(part_size, bool) or part_size < 1:
        raise ValueError("part_size must be a positive integer")
    outdir = Path(outdir)
    whole = hashlib.sha256()
    total = 0
    parts = []
    with source.open("rb") as handle:
        while True:
            first = handle.read(min(CHUNK, part_size))
            if not first:
                break
            counted = {"size": 0}

            def chunks(first=first, counted=counted):
                chunk = first
                while chunk:
                    whole.update(chunk)
                    counted["size"] += len(chunk)
                    yield chunk
                    chunk = handle.read(min(CHUNK, part_size - counted["size"])) \
                        if counted["size"] < part_size else b""

            sealed = _seal_chunks(chunks(), outdir, None, key_env)
            total += counted["size"]
            parts.append({"n": len(parts), "size": counted["size"],
                          "stored_sha256": sealed["stored_sha256"],
                          "stored_size": sealed["stored_size"]})
    return {"sha256": whole.hexdigest(), "size": total, "parts": parts}


def join_parts(part_paths_in_order, dest, expect_sha256, expect_size, sealed=False,
               key_env=KEY_ENV):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    descriptor, temporary = tempfile.mkstemp(dir=dest.parent, prefix=".artstore-")

    def append(plain_path, out):
        nonlocal total
        with Path(plain_path).open("rb") as handle:
            for chunk in _file_chunks(handle):
                total += len(chunk)
                if total > expect_size:
                    raise ValueError("Joined size exceeds expected size")
                digest.update(chunk)
                out.write(chunk)

    try:
        with os.fdopen(descriptor, "wb") as out:
            for part in part_paths_in_order:
                if sealed:
                    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".artstore-") as scratch:
                        plain = Path(scratch) / "part"
                        _decrypt_to(part, plain, key_env)
                        append(plain, out)
                else:
                    append(part, out)
            out.flush()
            os.fsync(out.fileno())
        if total != expect_size:
            raise ValueError("Joined size mismatch")
        if not hmac.compare_digest(digest.hexdigest(), str(expect_sha256).lower()):
            raise ValueError("Joined sha256 mismatch")
        os.replace(temporary, dest)
    finally:
        Path(temporary).unlink(missing_ok=True)


def new_index():
    return {"schema": INDEX_SCHEMA, "blobs": {}, "packs": {}}


def _check_index(index):
    if (not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA
            or not isinstance(index.get("blobs"), dict)
            or not isinstance(index.get("packs"), dict)):
        raise ValueError("Unsupported or malformed artstore index")
    return index


def deferred_map(index):
    """{sha256: {since, size}} of blobs an earlier run staged but could not upload; malformed rows are ignored."""
    raw = index.get("deferred") if isinstance(index, dict) else None
    result = {}
    if isinstance(raw, dict):
        for sha256, record in raw.items():
            since = record.get("since") if isinstance(record, dict) else None
            if (isinstance(sha256, str) and len(sha256) == 64 and isinstance(since, (int, float))
                    and not isinstance(since, bool)):
                size = record.get("size")
                result[sha256.lower()] = {"since": int(since),
                                          "size": size if isinstance(size, int) and size >= 0 else 0}
    return result


def set_deferred(index, mapping):
    _check_index(index)
    if mapping:
        index["deferred"] = {sha256: {"since": int(record["since"]), "size": int(record.get("size") or 0)}
                             for sha256, record in sorted(mapping.items())}
    else:
        index.pop("deferred", None)
    return index


def dump_index(index, path):
    _check_index(index)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".artstore-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(index, handle, sort_keys=True, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_index(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return _check_index(json.load(handle))


def add_pack(index, pack_name, blobs, member_map=None, created_at=None, expires_at=None,
             artifact_id=None):
    _check_index(index)
    if not pack_name:
        raise ValueError("pack_name is required")
    records = {}
    stored = {}
    for blob in blobs:
        parts = []
        for part in blob["parts"]:
            if member_map is not None and part["stored_sha256"] not in member_map:
                raise ValueError("Part missing from pack member map")
            stored[part["stored_sha256"]] = part["stored_size"]
            parts.append({"n": part["n"], "size": part["size"],
                          "stored_sha256": part["stored_sha256"],
                          "stored_size": part["stored_size"], "pack": pack_name})
        records[blob["sha256"]] = {"sha256": blob["sha256"], "size": blob["size"],
                                   "parts": parts}
    index["blobs"].update(records)
    index["packs"][pack_name] = {
        "artifact_id": artifact_id,
        "created_at": int(time.time()) if created_at is None else int(created_at),
        "expires_at": None if expires_at is None else int(expires_at),
        "size": sum(stored.values()),
    }
    return index


def set_pack_artifact(index, pack_name, artifact_id, expires_at):
    _check_index(index)
    if pack_name not in index["packs"]:
        raise ValueError("Unknown pack: %s" % pack_name)
    index["packs"][pack_name]["artifact_id"] = artifact_id
    index["packs"][pack_name]["expires_at"] = None if expires_at is None else int(expires_at)
    return index


def _canonical(value):
    return json.dumps(value, sort_keys=True, default=str)


def _pack_rank(entry):
    return (entry.get("artifact_id") is not None, entry.get("created_at") or 0,
            entry.get("expires_at") or 0, entry.get("size") or 0, _canonical(entry))


def _blob_rank(record, packs, now_epoch):
    entries = [packs.get(part["pack"]) for part in record["parts"]]
    complete = all(entry is not None and entry.get("artifact_id") is not None
                   for entry in entries)
    earliest = min(((entry or {}).get("expires_at") or 0 for entry in entries), default=0)
    created = max(((entry or {}).get("created_at") or 0 for entry in entries), default=0)
    alive = True if now_epoch is None else earliest > now_epoch
    return (alive, complete, earliest, created, _canonical(record))


def merge_indexes(list_of_indexes, now_epoch=None):
    merged = new_index()
    candidates = {}
    for index in list_of_indexes:
        _check_index(index)
        for name, entry in index["packs"].items():
            current = merged["packs"].get(name)
            if current is None or _pack_rank(entry) > _pack_rank(current):
                merged["packs"][name] = copy.deepcopy(entry)
        for sha256, record in index["blobs"].items():
            candidates.setdefault(sha256, []).append(record)
    for sha256, records in candidates.items():
        best = max(records, key=lambda record: _blob_rank(record, merged["packs"], now_epoch))
        merged["blobs"][sha256] = copy.deepcopy(best)
    deferred = {}
    for index in list_of_indexes:
        for sha256, record in deferred_map(index).items():
            if sha256 not in merged["blobs"] and (
                    sha256 not in deferred or record["since"] < deferred[sha256]["since"]):
                deferred[sha256] = record
    return set_deferred(merged, deferred)


def lookup(index, sha256):
    record = _check_index(index)["blobs"].get(str(sha256).lower())
    return copy.deepcopy(record) if record is not None else None


def blobs_needing_refresh(index, now_epoch, within_days):
    _check_index(index)
    threshold = now_epoch + within_days * DAY_SECONDS
    needing = []
    for sha256 in sorted(index["blobs"]):
        for pack_name in sorted({part["pack"] for part in index["blobs"][sha256]["parts"]}):
            entry = index["packs"].get(pack_name)
            expires_at = None if entry is None else entry.get("expires_at")
            if entry is None or (expires_at is not None and expires_at <= threshold):
                needing.append(sha256)
                break
    return needing


def blobs_missing(index, wanted_sha256s):
    _check_index(index)
    missing = []
    for sha256 in wanted_sha256s:
        sha256 = str(sha256).lower()
        if sha256 not in index["blobs"] and sha256 not in missing:
            missing.append(sha256)
    return missing


def drop_expired(index, now_epoch):
    _check_index(index)
    expired = {name for name, entry in index["packs"].items()
               if entry.get("expires_at") is not None and entry["expires_at"] <= now_epoch}
    for name in expired:
        del index["packs"][name]
    for sha256 in [sha256 for sha256, record in index["blobs"].items()
                   if any(part["pack"] in expired for part in record["parts"])]:
        del index["blobs"][sha256]
    return index
