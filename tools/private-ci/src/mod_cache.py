#!/usr/bin/env python3
"""Private, content-addressed mod downloads with immutable offline generations.

Only explicitly resolved direct download URLs are accepted. Page URLs, HTML,
authentication challenges, and unresolved inventory items remain blockers.
Nothing in this module uses the public GitHub Actions cache or prints URLs.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import subprocess
import tempfile
import time
from typing import Protocol
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from .executor import relative_path, canonical_path
except ImportError:
    from executor import relative_path, canonical_path

REFRESH_INTERVAL = 14 * 24 * 60 * 60
DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
PUBLIC_BLOCK_FLAGS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")


class CacheError(Exception):
    pass


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def safe_key(key: str) -> str:
    return relative_path(key)


class ObjectStore(Protocol):
    def read(self, key: str) -> bytes | None: ...
    def has(self, key: str) -> bool: ...
    def get_file(self, key: str, destination: Path) -> bool: ...
    def put_file(self, key: str, source: Path, immutable: bool = False) -> None: ...
    def write(self, key: str, data: bytes, immutable: bool = False) -> None: ...
    def refresh_lock(self): ...


class LocalObjectStore:
    """A user-owned private directory, never a public repository cache path."""
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        for name in ("PUBLIC_CI_WORKSPACE", "GITHUB_WORKSPACE"):
            public = os.environ.get(name)
            if public and self.root.is_relative_to(Path(public).resolve()):
                raise CacheError("Private cache cannot reside in the public checkout")
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)

    def path(self, key: str) -> Path:
        path = self.root.joinpath(*safe_key(key).split("/"))
        if not path.resolve().is_relative_to(self.root):
            raise CacheError("Object path escapes the private store")
        return path

    def has(self, key: str) -> bool:
        path = self.path(key)
        return path.is_file() and not path.is_symlink()

    def read(self, key: str) -> bytes | None:
        path = self.path(key)
        if not self.has(key):
            return None
        if path.stat().st_size > 32 * 1024 * 1024:
            raise CacheError("Catalog exceeds metadata bound")
        return path.read_bytes()

    def get_file(self, key: str, destination: Path) -> bool:
        if not self.has(key):
            return False
        shutil.copyfile(self.path(key), destination)
        if os.name != "nt":
            destination.chmod(0o600)
        return True

    def put_file(self, key: str, source: Path, immutable: bool = False) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if immutable and self.has(key):
            if file_digest(target) != file_digest(source):
                raise CacheError("Immutable object already contains different bytes")
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if immutable:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if file_digest(target) != file_digest(source):
                        raise CacheError("Immutable object collision")
            else:
                os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def write(self, key: str, data: bytes, immutable: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix="private-cache-metadata-") as directory:
            source = Path(directory) / "metadata"
            source.write_bytes(data)
            self.put_file(key, source, immutable)

    @contextmanager
    def refresh_lock(self):
        with self.path("refresh.lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                if not lock.read(1):
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)


class AwsCliS3ObjectStore:
    """Optional private S3 backend. Requires all bucket public-access blocks.

    AWS credentials stay in the trusted AWS CLI environment. Every response
    and diagnostic is redirected to private files. A crashed refresh leaves
    a fail-closed lock that the bucket owner must inspect/remove; no unsafe
    automatic stale-lock takeover occurs.
    """
    def __init__(self, bucket: str, prefix: str, private_log: Path, *,
                 endpoint_url: str | None = None, sse: str = "AES256", read_only: bool = False):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise CacheError("Invalid bucket")
        if sse not in {"AES256", "none"}:
            raise CacheError("Invalid provider encryption configuration")
        if endpoint_url:
            parsed = urllib.parse.urlsplit(endpoint_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise CacheError("Private S3 endpoint must be HTTPS without embedded credentials")
        self.bucket, self.prefix = bucket, safe_key(prefix)
        self.endpoint_url, self.sse, self.read_only = endpoint_url, sse, read_only
        self.log = Path(private_log)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.touch(mode=0o600, exist_ok=True)
        result = self._run(["get-public-access-block"])
        if result is not None:
            flags = result.get("PublicAccessBlockConfiguration", {})
            if not all(flags.get(flag) is True for flag in PUBLIC_BLOCK_FLAGS):
                raise CacheError("Bucket public-access protections are incomplete")
        self._verify_privacy()

    def _encryption_args(self):
        return ["--server-side-encryption", "AES256"] if self.sse == "AES256" else []

    def _verify_privacy(self):
        if self.read_only:
            if not self.has("catalog.json"):
                raise CacheError("Readonly cache requires an existing authenticated catalog")
            self._assert_private("catalog.json")
            return
        key = "privacy-probe/" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="private-s3-probe-") as directory:
            body = Path(directory) / "known-probe"
            body.write_bytes(os.urandom(32))
            if self._run(["put-object", "--body", str(body), *self._encryption_args(), "--if-none-match", "*"], key) is None:
                raise CacheError("Private bucket probe could not be written")
            try:
                if not self.has(key):
                    raise CacheError("Private bucket probe existence was not authenticated")
                self._assert_private(key)
            finally:
                if self._run(["delete-object"], key) is None:
                    raise CacheError("Private bucket probe cleanup failed")

    def _assert_private(self, key):
        host = self.endpoint_url.rstrip("/") + "/" + self.bucket if self.endpoint_url else "https://" + self.bucket + ".s3.amazonaws.com"
        url = host + "/" + urllib.parse.quote(self.prefix + "/" + safe_key(key), safe="/")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                raise CacheError("Bucket privacy probe redirected unexpectedly")
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(urllib.request.Request(url, method="HEAD"), timeout=30):
                raise CacheError("Private bucket object is anonymously readable")
        except urllib.error.HTTPError as error:
            if error.code not in {403, 404}:
                raise CacheError("Private bucket anonymity could not be verified") from error
        except urllib.error.URLError as error:
            raise CacheError("Private bucket anonymity could not be verified") from error

    def _run(self, arguments: list[str], key: str | None = None):
        argv = ["aws", "s3api", *arguments, "--bucket", self.bucket, "--output", "json"]
        if self.endpoint_url:
            argv += ["--endpoint-url", self.endpoint_url]
        if key is not None:
            argv += ["--key", self.prefix + "/" + safe_key(key)]
        environment = {name: value for name, value in os.environ.items()
                       if name.upper() in {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"} or name.startswith("AWS_")}
        environment["AWS_PAGER"] = ""
        with tempfile.TemporaryDirectory(prefix="private-s3-response-") as directory:
            response = Path(directory) / "response.json"
            with response.open("wb") as output, self.log.open("ab") as errors:
                result = subprocess.run(argv, env=environment, stdout=output, stderr=errors, timeout=300, check=False)
            if result.returncode:
                return None
            data = response.read_bytes()
            return json.loads(data) if data.strip() else {}

    def has(self, key: str) -> bool:
        return self._run(["head-object"], key) is not None

    def get_file(self, key: str, destination: Path) -> bool:
        result = self._run(["get-object", str(destination)], key)
        if result is None:
            destination.unlink(missing_ok=True)
            return False
        return True

    def read(self, key: str) -> bytes | None:
        with tempfile.TemporaryDirectory(prefix="private-s3-read-") as directory:
            destination = Path(directory) / "metadata"
            if not self.get_file(key, destination):
                return None
            if destination.stat().st_size > 32 * 1024 * 1024:
                raise CacheError("Catalog exceeds metadata bound")
            return destination.read_bytes()

    def put_file(self, key: str, source: Path, immutable: bool = False) -> None:
        if self.read_only:
            raise CacheError("Readonly cache credentials cannot publish objects")
        args = ["put-object", "--body", str(source), *self._encryption_args()]
        if immutable:
            args += ["--if-none-match", "*"]
        if self._run(args, key) is None:
            if immutable:
                with tempfile.TemporaryDirectory(prefix="private-s3-compare-") as directory:
                    existing = Path(directory) / "existing"
                    if self.get_file(key, existing) and file_digest(existing) == file_digest(source):
                        return
            raise CacheError("Private object publication failed")

    def write(self, key: str, data: bytes, immutable: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix="private-s3-write-") as directory:
            source = Path(directory) / "metadata"
            source.write_bytes(data)
            self.put_file(key, source, immutable)

    @contextmanager
    def refresh_lock(self):
        if self.read_only:
            raise CacheError("Readonly cache credentials cannot refresh")
        token = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="private-s3-lock-") as directory:
            lease = Path(directory) / "lock"
            lease.write_bytes(json_bytes({"owner": token, "createdAt": utc(time.time())}))
            result = self._run(["put-object", "--body", str(lease), *self._encryption_args(), "--if-none-match", "*"], "refresh.lock")
            if result is None:
                raise CacheError("Another private refresh owns the lock")
            try:
                yield
            finally:
                if self._run(["delete-object", "--if-match", result["ETag"]], "refresh.lock") is None:
                    raise CacheError("Refresh completed but private lock release failed")


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or not parsed.hostname:
        raise CacheError("Download URL must not contain account credentials")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise CacheError("Direct downloads require HTTPS")


class PrivateRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_url(new_url)
        if urllib.parse.urlsplit(request.full_url).scheme == "https" and urllib.parse.urlsplit(new_url).scheme != "https":
            raise CacheError("HTTPS downloads cannot be downgraded")
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(new_url)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            for name in list(redirected.headers) + list(redirected.unredirected_hdrs):
                if name.lower() in {"authorization", "cookie", "proxy-authorization", "x-api-key"}:
                    redirected.remove_header(name)
        return redirected


def normalize_sources(sources: list[dict]) -> list[dict]:
    if not isinstance(sources, list) or not sources:
        raise CacheError("A complete nonempty resolved source inventory is required")
    normalized, ids = [], set()
    for source in sources:
        if not isinstance(source, dict) or source.get("status") in {"unresolved", "blocked"}:
            raise CacheError("Unresolved guide resources block cache publication")
        item = {key: source[key] for key in ("id", "url", "filename", "provider", "sourceUrl", "revision", "etag", "lastModified", "sha256", "size") if key in source and source[key] is not None}
        if not all(isinstance(item.get(key), str) and item[key] for key in ("id", "url", "filename", "provider")):
            raise CacheError("Resolved sources require stable id, provider, filename, and direct URL")
        if item["id"] in ids:
            raise CacheError("Duplicate stable source id")
        ids.add(item["id"])
        validate_url(item["url"])
        filename = relative_path(item["filename"])
        if "/" in filename:
            raise CacheError("Download filename must be a portable basename")
        item["filename"] = filename
        if "sha256" in item and not re.fullmatch(r"[a-fA-F0-9]{64}", item["sha256"]):
            raise CacheError("Invalid expected digest")
        if "sha256" in item:
            item["sha256"] = item["sha256"].lower()
        if "size" in item and (isinstance(item["size"], bool) or not isinstance(item["size"], int) or item["size"] < 1):
            raise CacheError("Invalid expected download size")
        for key in ("revision", "etag", "lastModified", "sourceUrl"):
            if key in item and not isinstance(item[key], str):
                raise CacheError("Invalid source revision metadata")
        normalized.append(item)
    return normalized


class ModCache:
    def __init__(self, store: ObjectStore, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 minimum_request_interval: float = 0.2, clock=time.time):
        self.store, self.max_bytes, self.interval, self.clock = store, max_bytes, minimum_request_interval, clock
        self.opener = urllib.request.build_opener(PrivateRedirectHandler())
        self.last_request = 0.0

    def request(self, url: str, method: str, headers: dict[str, str] | None = None):
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        try:
            return self.opener.open(urllib.request.Request(url, method=method, headers=headers or {}), timeout=60)
        except urllib.error.HTTPError as error:
            if error.code in (304, 405, 501):
                return error
            raise CacheError("Provider download requires resolution or authorized access") from error

    def generation(self, generation: str, digest: str | None = None) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", generation):
            raise CacheError("Invalid generation")
        data = self.store.read(f"generations/{generation}.json")
        if data is None or (digest and hashlib.sha256(data).hexdigest() != digest):
            raise CacheError("Frozen generation is missing or changed")
        catalog = json.loads(data)
        if catalog.get("generation") != generation or catalog.get("schemaVersion") != 1:
            raise CacheError("Invalid frozen generation")
        normalize_sources(catalog.get("sources"))
        return catalog

    def current(self):
        data = self.store.read("catalog.json")
        if data is None:
            return None, None
        pointer = json.loads(data)
        return pointer, self.generation(pointer["generation"], pointer["generationDigest"])

    def _download(self, source: dict, destination: Path) -> dict:
        with self.request(source["url"], "GET") as response:
            if response.status != 200:
                raise CacheError("Provider returned an incomplete download response")
            kind = response.headers.get("Content-Type", "").lower()
            if "text/html" in kind or "application/xhtml" in kind:
                raise CacheError("Provider returned a page instead of a download")
            length = response.headers.get("Content-Length")
            expected_length = int(length) if length else None
            if expected_length is not None and (expected_length < 1 or expected_length > self.max_bytes):
                raise CacheError("Provider payload exceeds download bounds")
            sha256, size, first = hashlib.sha256(), 0, b""
            with destination.open("xb") as output:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    if not first:
                        first = block[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
                        if first.startswith((b"<html", b"<!doctype html")):
                            raise CacheError("Provider returned an HTML login or download page")
                    size += len(block)
                    if size > self.max_bytes:
                        raise CacheError("Provider payload exceeds download bounds")
                    output.write(block)
                    sha256.update(block)
            digest = sha256.hexdigest()
            if not size or (expected_length is not None and size != expected_length) or (source.get("size") is not None and size != source["size"]):
                raise CacheError("Provider payload size did not verify")
            if source.get("sha256") and source["sha256"] != digest:
                raise CacheError("Provider payload digest did not verify")
            etag = response.headers.get("ETag")
            return {**source, "sha256": digest, "size": size,
                    "etag": etag if etag and not etag.startswith("W/") else None,
                    "lastModified": response.headers.get("Last-Modified"),
                    "downloadedAt": utc(self.clock()), "checkedAt": utc(self.clock())}

    def refresh(self, sources: list[dict], *, force: bool = False) -> dict:
        result = {"ok": False, "generation": None, "generationDigest": None,
                  "changed": [], "unchanged": [], "blockers": [], "skipped": False}
        try:
            sources = normalize_sources(sources)
            fingerprint = hashlib.sha256(json_bytes(sources)).hexdigest()
            with self.store.refresh_lock():
                pointer, previous = self.current()
                if pointer:
                    result.update(generation=pointer["generation"], generationDigest=pointer["generationDigest"])
                now = self.clock()
                old = {entry["id"]: entry for entry in previous["sources"]} if previous else {}
                if not force and pointer and pointer["sourceFingerprint"] == fingerprint and now - datetime.fromisoformat(pointer["lastSuccess"]).timestamp() < REFRESH_INTERVAL:
                    if all(self.store.has(f"cas/{entry['sha256'][:2]}/{entry['sha256']}") for entry in old.values()):
                        result.update(ok=True, skipped=True, unchanged=list(old))
                        return result
                with tempfile.TemporaryDirectory(prefix="private-mod-refresh-") as directory:
                    staging = Path(directory)
                    entries, pending = [], {}
                    for index, source in enumerate(sources):
                        try:
                            cached = old.get(source["id"])
                            same_resource = cached and (cached["url"] == source["url"] or (source.get("revision") and cached.get("revision") == source["revision"] and cached["provider"] == source["provider"]))
                            reusable = same_resource and cached.get("revision") == source.get("revision") and (not source.get("sha256") or source["sha256"] == cached["sha256"]) and (not source.get("size") or source["size"] == cached["size"]) and self.store.has(f"cas/{cached['sha256'][:2]}/{cached['sha256']}")
                            headers = {}
                            if reusable and cached.get("etag"):
                                headers["If-None-Match"] = cached["etag"]
                            if reusable and cached.get("lastModified"):
                                headers["If-Modified-Since"] = cached["lastModified"]
                            unchanged = False
                            with self.request(source["url"], "HEAD", headers) as response:
                                unchanged = bool(reusable and (response.status == 304 or (response.status == 200 and ((cached.get("etag") and response.headers.get("ETag") == cached["etag"]) or (not cached.get("etag") and cached.get("lastModified") and response.headers.get("Last-Modified") == cached["lastModified"])))))
                            if unchanged and response.headers.get("Content-Length"):
                                unchanged = int(response.headers["Content-Length"]) == cached["size"]
                            if unchanged:
                                entry = {**cached, **source, "sha256": cached["sha256"], "size": cached["size"], "checkedAt": utc(now)}
                                result["unchanged"].append(source["id"])
                            else:
                                path = staging / f"download-{index}"
                                entry = self._download(source, path)
                                pending[entry["sha256"]] = path
                                result["changed"].append(source["id"])
                            entries.append(entry)
                        except Exception as error:
                            result["blockers"].append({"id": source["id"], "reason": str(error)})
                    filenames = {}
                    for entry in entries:
                        key = canonical_path(entry["filename"])
                        if key in filenames and filenames[key] != entry["sha256"]:
                            result["blockers"].append({"id": entry["id"], "reason": "Different payloads require the same offline filename"})
                        filenames[key] = entry["sha256"]
                    if result["blockers"]:
                        return result
                    # Publish nothing until every required source has verified.
                    for digest, path in pending.items():
                        self.store.put_file(f"cas/{digest[:2]}/{digest}", path, immutable=True)
                    generation = uuid.uuid4().hex
                    catalog = {"schemaVersion": 1, "generation": generation, "lastSuccess": utc(now),
                               "sourceFingerprint": fingerprint, "sources": entries}
                    data = json_bytes(catalog)
                    digest = hashlib.sha256(data).hexdigest()
                    self.store.write(f"generations/{generation}.json", data, immutable=True)
                    self.store.write("catalog.json", json_bytes({"generation": generation, "generationDigest": digest,
                                     "lastSuccess": catalog["lastSuccess"], "sourceFingerprint": fingerprint}))
                    result.update(ok=True, generation=generation, generationDigest=digest)
                    return result
        except Exception as error:
            result["ok"] = False
            result["blockers"].append({"reason": str(error)})
            return result

    def materialize(self, generation: str, destination: Path, *, generation_digest: str) -> dict:
        """Offline only. Verify all blobs before publishing a fresh install folder."""
        catalog = self.generation(generation, generation_digest)
        destination = Path(destination)
        if destination.exists():
            raise CacheError("Offline download directory must be fresh")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".private-mod-offline-", dir=destination.parent) as directory:
            temporary = Path(directory)
            names = {}
            for entry in catalog["sources"]:
                name = canonical_path(entry["filename"])
                if name in names:
                    if names[name] != entry["sha256"]:
                        raise CacheError("Frozen generation filename collision")
                    continue
                names[name] = entry["sha256"]
                target = temporary / entry["filename"]
                if not self.store.get_file(f"cas/{entry['sha256'][:2]}/{entry['sha256']}", target) or file_digest(target) != (entry["sha256"], entry["size"]):
                    raise CacheError("Frozen archive is missing or failed integrity verification")
            os.replace(temporary, destination)
        return {"generation": generation, "generationDigest": generation_digest, "files": len(names), "offline": True}


def main(argv=None) -> int:
    class QuietParser(argparse.ArgumentParser):
        def error(self, message):
            raise CacheError("Invalid cache invocation")
    parser = QuietParser(description=__doc__)
    parser.add_argument("action", choices=("refresh", "materialize"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--s3-bucket")
    parser.add_argument("--store-config", type=Path)
    parser.add_argument("--s3-prefix", default="modsync-private")
    parser.add_argument("--private-log", type=Path)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--generation")
    parser.add_argument("--generation-digest")
    parser.add_argument("--destination", type=Path)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def cancel_execution(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, cancel_execution)
    try:
        args = parser.parse_args(argv)
        if sum(bool(value) for value in (args.cache, args.s3_bucket, args.store_config)) != 1:
            raise CacheError("Choose one private storage backend")
        storage = json.loads(args.store_config.read_text(encoding="utf-8")) if args.store_config else {"bucket": args.s3_bucket, "prefix": args.s3_prefix}
        if not args.cache and not args.private_log:
            raise CacheError("S3 diagnostics require a private log destination")
        store = LocalObjectStore(args.cache) if args.cache else AwsCliS3ObjectStore(storage["bucket"], storage.get("prefix", "modsync-private"), args.private_log,
                  endpoint_url=storage.get("endpointUrl"), sse=storage.get("sse", "AES256"), read_only=args.action == "materialize")
        cache = ModCache(store)
        if args.action == "refresh":
            if not args.sources:
                raise CacheError("A private complete source inventory is required")
            sources = json.loads(args.sources.read_text(encoding="utf-8"))
            result = cache.refresh(sources.get("sources") if isinstance(sources, dict) else sources, force=args.force)
        else:
            if not args.generation or not args.generation_digest or not args.destination:
                raise CacheError("An immutable generation and private offline destination are required")
            result = {"ok": True, **cache.materialize(args.generation, args.destination, generation_digest=args.generation_digest)}
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_bytes(json_bytes(result))
            if os.name != "nt":
                args.receipt.chmod(0o600)
        print("Private mod cache operation completed." if result["ok"] else "Private mod cache operation blocked.")
        return 0 if result["ok"] else 1
    except KeyboardInterrupt:
        print("Private mod cache operation cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print("Private mod cache operation failed.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
