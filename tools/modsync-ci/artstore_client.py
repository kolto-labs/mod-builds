"""GitHub Actions artifacts REST client for the artstore blob store (stdlib only).

Credentials are only ever sent to the API origin. Signed blob URLs returned by
the ``/zip`` redirect are fetched without an Authorization header and are
treated as secrets (never included in exception text).
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

__all__ = [
    "ArtifactError",
    "ArtifactExpired",
    "ArtifactMissing",
    "RateLimited",
    "DigestMismatch",
    "ArtifactsClient",
    "RangedFile",
    "make_opener",
    "from_environment",
]

DEFAULT_API = "https://api.github.com"
COPY_CHUNK = 4 * 1024 * 1024

_API_VERSION = "2022-11-28"
_USER_AGENT = "modsync-artstore-client/1"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RERESOLVE_STATUSES = frozenset({401, 403, 404, 410})
_RETRY_STATUSES = frozenset({500, 502, 503, 504})
_ERROR_BODY_LIMIT = 65536


class ArtifactError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ArtifactExpired(ArtifactError):
    pass


class ArtifactMissing(ArtifactError):
    pass


class RateLimited(ArtifactError):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message, status)
        self.retry_after = retry_after


class DigestMismatch(ArtifactError):
    pass


class _Retryable(Exception):
    def __init__(self, message, status=None, rejected=False):
        super().__init__(message)
        self.status = status
        self.rejected = rejected


class _Secret:
    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("secret is not serializable")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_opener(trust_env=True):
    handlers = [_NoRedirect()]
    if not trust_env:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class _Reply:
    def __init__(self, status, headers, stream, body=b""):
        self.status = status
        self.headers = headers
        self.body = body
        self._stream = stream

    def read(self, amount=-1):
        if self._stream is None:
            data, self.body = self.body, b""
            return data
        if amount is None or amount < 0:
            return self._stream.read()
        return self._stream.read(amount)

    def close(self):
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.close()
            self._stream = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _int_or_none(value):
    if value is not None and str(value).strip().isdigit():
        return int(str(value).strip())
    return None


def _parse_retry_after(value, now):
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, moment.timestamp() - now)


def _parse_time(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _parse_digest(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if not _DIGEST_RE.match(text):
        raise ArtifactError("expect_digest must look like sha256:<64 hex chars>")
    return text


@contextlib.contextmanager
def _atomic_file(dest_path):
    dest = os.path.abspath(os.fspath(dest_path))
    directory = os.path.dirname(dest)
    os.makedirs(directory, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        prefix="." + os.path.basename(dest) + ".", suffix=".part", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            yield handle
        os.replace(temp_path, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


class ArtifactsClient:
    def __init__(
        self,
        repo,
        token,
        api=DEFAULT_API,
        opener=None,
        timeout=60,
        *,
        max_retries=4,
        max_wait=120.0,
        backoff_base=1.0,
        per_page=100,
        max_pages=200,
    ):
        if not isinstance(repo, str) or not _REPO_RE.match(repo):
            raise ValueError("repo must look like 'owner/name'")
        if not token:
            raise ValueError("a token is required")
        self.repo = repo
        self.api = str(api).rstrip("/")
        self._token = _Secret(token)
        self._opener = opener if opener is not None else make_opener()
        self._timeout = timeout
        self._max_retries = int(max_retries)
        self._max_wait = float(max_wait)
        self._backoff_base = float(backoff_base)
        self._per_page = max(1, min(int(per_page), 100))
        self._max_pages = int(max_pages)
        self._reresolve_limit = 2
        self.rate_limit_remaining = None
        self.requests_made = 0

    def __repr__(self):
        return f"ArtifactsClient(repo={self.repo!r}, api={self.api!r})"

    __str__ = __repr__

    def _scrub(self, text):
        secret = self._token.reveal()
        return text.replace(secret, "***") if secret else text

    def _sleep(self, seconds):
        time.sleep(seconds)

    def _backoff(self, attempt):
        return min(self._backoff_base * (2 ** attempt), self._max_wait)

    def _looks_rate_limited(self, reply):
        headers = reply.headers
        if headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0":
            return True
        text = reply.body.decode("utf-8", "replace").lower()
        return "rate limit" in text or "abuse" in text

    def _limit_wait(self, headers, attempt):
        now = time.time()
        retry_after = headers.get("Retry-After")
        if retry_after:
            parsed = _parse_retry_after(retry_after, now)
            if parsed is not None:
                return parsed
        if headers.get("X-RateLimit-Remaining") == "0":
            reset = _int_or_none(headers.get("X-RateLimit-Reset"))
            if reset is not None:
                return max(0.0, reset - now) + 1.0
        return self._backoff(attempt)

    def _request(self, method, url, *, headers=None, auth=False):
        request_headers = {"User-Agent": _USER_AGENT}
        if auth:
            if not url.startswith(self.api + "/"):
                raise ArtifactError("refusing to send credentials to a non-API URL")
            request_headers["Accept"] = "application/vnd.github+json"
            request_headers["X-GitHub-Api-Version"] = _API_VERSION
            request_headers["Authorization"] = "Bearer " + self._token.reveal()
        if headers:
            request_headers.update(headers)
        attempt = 0
        while True:
            request = urllib.request.Request(url, method=method, headers=request_headers)
            failure = None
            stream = None
            status = None
            try:
                stream = self._opener.open(request, timeout=self._timeout)
                status = getattr(stream, "status", None) or stream.getcode()
            except urllib.error.HTTPError as exc:
                stream = exc
                status = exc.code
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                failure = exc
            self.requests_made += 1
            if failure is not None:
                if attempt >= self._max_retries:
                    detail = getattr(failure, "reason", None) or failure
                    raise ArtifactError(
                        self._scrub(f"network error: {type(failure).__name__}: {detail}")
                    ) from None
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            response_headers = stream.headers
            if auth:
                remaining = _int_or_none(response_headers.get("X-RateLimit-Remaining"))
                if remaining is not None:
                    self.rate_limit_remaining = remaining
            if status < 300:
                return _Reply(status, response_headers, stream)
            try:
                body = stream.read(_ERROR_BODY_LIMIT)
            except (OSError, http.client.HTTPException):
                body = b""
            finally:
                with contextlib.suppress(Exception):
                    stream.close()
            reply = _Reply(status, response_headers, None, body)
            if status < 400:
                return reply
            limited = status == 429 or (
                status == 403 and auth and self._looks_rate_limited(reply)
            )
            if limited:
                wait = self._limit_wait(response_headers, attempt)
            elif status in _RETRY_STATUSES:
                wait = self._backoff(attempt)
            else:
                return reply
            if attempt >= self._max_retries or wait > self._max_wait:
                if limited:
                    raise RateLimited(
                        f"GitHub rate limit reached (HTTP {status}); retry after {wait:.0f}s",
                        status=status,
                        retry_after=wait,
                    )
                return reply
            self._sleep(wait)
            attempt += 1

    def _message(self, reply):
        try:
            payload = json.loads(reply.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ""
        if isinstance(payload, dict) and payload.get("message"):
            return self._scrub(str(payload["message"]))[:200]
        return ""

    def _error_for(self, reply, what):
        status = reply.status
        message = self._message(reply)
        text = f"{what}: HTTP {status}"
        if message:
            text += f" ({message})"
        if status in (401, 403):
            text += " - check that the token is valid and has the required Actions permission"
        if status == 404:
            return ArtifactMissing(text, status)
        if status == 410:
            return ArtifactExpired(text, status)
        return ArtifactError(text, status)

    def _json(self, reply, what):
        try:
            return json.loads(reply.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ArtifactError(f"{what}: response was not valid JSON") from None

    def _artifact_url(self, artifact_id, suffix=""):
        return f"{self.api}/repos/{self.repo}/actions/artifacts/{int(artifact_id)}{suffix}"

    def list_artifacts(self, prefix=None, include_expired=False, name=None):
        found = {}
        page = 1
        while page <= self._max_pages:
            params = [("per_page", self._per_page), ("page", page)]
            if name:
                params.append(("name", name))
            url = (
                f"{self.api}/repos/{self.repo}/actions/artifacts?"
                + urllib.parse.urlencode(params)
            )
            with self._request("GET", url, auth=True) as reply:
                if reply.status != 200:
                    raise self._error_for(reply, "list artifacts")
                payload = self._json(reply, "list artifacts")
            batch = payload.get("artifacts") or []
            for artifact in batch:
                found[artifact.get("id")] = artifact
            total = payload.get("total_count")
            if len(batch) < self._per_page:
                break
            if isinstance(total, int) and len(found) >= total:
                break
            page += 1
        now = datetime.now(timezone.utc)
        result = []
        for artifact in found.values():
            if prefix and not str(artifact.get("name", "")).startswith(prefix):
                continue
            if name and artifact.get("name") != name:
                continue
            if not include_expired:
                if artifact.get("expired"):
                    continue
                expires_at = _parse_time(artifact.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    continue
            result.append(artifact)
        result.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or 0), reverse=True)
        return result

    def resolve_url(self, artifact_id):
        url = self._artifact_url(artifact_id, "/zip")
        with self._request("GET", url, auth=True) as reply:
            if reply.status in _REDIRECT_STATUSES:
                location = reply.headers.get("Location")
                if not location:
                    raise ArtifactError("artifact redirect carried no Location header", reply.status)
                return urllib.parse.urljoin(url, location)
            if reply.status < 300:
                raise ArtifactError(
                    "expected a redirect to the blob URL; the opener must not follow redirects",
                    reply.status,
                )
            raise self._error_for(reply, "resolve artifact")

    def download_zip(self, artifact_id, dest_path, expect_digest=None):
        expected = _parse_digest(expect_digest)
        attempts = self._reresolve_limit + 1
        for attempt in range(attempts):
            url = self.resolve_url(artifact_id)
            try:
                return self._stream_blob(url, dest_path, expected, artifact_id)
            except _Retryable as exc:
                if attempt + 1 >= attempts:
                    raise ArtifactError(
                        f"download of artifact {int(artifact_id)} failed: {exc}", exc.status
                    ) from None
                if not exc.rejected:
                    self._sleep(self._backoff(attempt))
        raise ArtifactError("download failed")

    def _stream_blob(self, url, dest_path, expected, artifact_id):
        with self._request("GET", url) as reply:
            if reply.status in _RERESOLVE_STATUSES:
                raise _Retryable(f"blob URL rejected (HTTP {reply.status})", reply.status, True)
            if reply.status != 200:
                raise self._error_for(reply, "download artifact")
            declared = _int_or_none(reply.headers.get("Content-Length"))
            hasher = hashlib.sha256()
            total = 0
            with _atomic_file(dest_path) as handle:
                while True:
                    try:
                        chunk = reply.read(COPY_CHUNK)
                    except (OSError, http.client.HTTPException) as exc:
                        raise _Retryable(
                            f"connection lost mid-download ({type(exc).__name__})"
                        ) from None
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
                if declared is not None and total != declared:
                    raise _Retryable("truncated download")
                digest = "sha256:" + hasher.hexdigest()
                if expected is not None and digest != expected:
                    raise DigestMismatch(
                        f"digest mismatch for artifact {int(artifact_id)}: "
                        f"expected {expected}, got {digest}"
                    )
            return digest

    def open_ranged(self, artifact_id, **options):
        return RangedFile(self, artifact_id, **options)

    def read_member(self, artifact_id, member_name, dest_path, expect_size=None):
        ranged = RangedFile(self, artifact_id)
        try:
            with zipfile.ZipFile(ranged) as archive:
                try:
                    info = archive.getinfo(member_name)
                except KeyError:
                    raise ArtifactMissing(
                        f"member {member_name!r} not found in artifact {int(artifact_id)}"
                    ) from None
                self._check_member(info, expect_size)
                if info.compress_type == zipfile.ZIP_STORED and not info.flag_bits & 0x1:
                    return self._copy_member(archive, info, dest_path)
        except zipfile.BadZipFile as exc:
            raise ArtifactError(f"artifact is not a readable zip: {exc}") from None
        finally:
            ranged.close()
        return self._read_member_by_download(artifact_id, member_name, dest_path, expect_size)

    def _check_member(self, info, expect_size):
        if info.is_dir():
            raise ArtifactError(f"member {info.filename!r} is a directory")
        if info.flag_bits & 0x1:
            raise ArtifactError(f"member {info.filename!r} is encrypted")
        if expect_size is not None and info.file_size != expect_size:
            raise ArtifactError(
                f"member {info.filename!r} is {info.file_size} bytes, expected {expect_size}"
            )

    def _copy_member(self, archive, info, dest_path):
        written = 0
        try:
            with archive.open(info) as source, _atomic_file(dest_path) as handle:
                while True:
                    chunk = source.read(COPY_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                if written != info.file_size:
                    raise ArtifactError(
                        f"member {info.filename!r} yielded {written} bytes, expected {info.file_size}"
                    )
        except zipfile.BadZipFile as exc:
            raise ArtifactError(f"member {info.filename!r} failed validation: {exc}") from None
        return written

    def _read_member_by_download(self, artifact_id, member_name, dest_path, expect_size):
        directory = os.path.dirname(os.path.abspath(os.fspath(dest_path)))
        os.makedirs(directory, exist_ok=True)
        scratch = tempfile.mkdtemp(prefix=".artstore-", dir=directory)
        try:
            zip_path = os.path.join(scratch, "artifact.zip")
            self.download_zip(artifact_id, zip_path)
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    try:
                        info = archive.getinfo(member_name)
                    except KeyError:
                        raise ArtifactMissing(
                            f"member {member_name!r} not found in artifact {int(artifact_id)}"
                        ) from None
                    self._check_member(info, expect_size)
                    return self._copy_member(archive, info, dest_path)
            except zipfile.BadZipFile as exc:
                raise ArtifactError(f"artifact is not a readable zip: {exc}") from None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def delete_artifact(self, artifact_id):
        url = self._artifact_url(artifact_id)
        with self._request("DELETE", url, auth=True) as reply:
            if reply.status in (200, 202, 204):
                return None
            raise self._error_for(reply, "delete artifact")


class RangedFile(io.RawIOBase):
    def __init__(
        self,
        client,
        artifact_id,
        *,
        size=None,
        read_ahead=64 * 1024,
        max_fetch=8 * 1024 * 1024,
        cache_windows=4,
        cache_limit=1024 * 1024,
        url_ttl=480.0,
    ):
        super().__init__()
        self._client = client
        self._artifact_id = int(artifact_id)
        self._size = size
        self._read_ahead = max(1, int(read_ahead))
        self._max_fetch = max(self._read_ahead, int(max_fetch))
        self._cache_windows = max(1, int(cache_windows))
        self._cache_limit = int(cache_limit)
        self._url_ttl = float(url_ttl)
        self._url = None
        self._url_at = 0.0
        self._pos = 0
        self._windows = []
        self._lock = threading.RLock()
        self.requests = 0
        self.resolves = 0
        self.bytes_fetched = 0

    @property
    def size(self):
        with self._lock:
            return self._ensure_size()

    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return False

    def tell(self):
        self._require_open()
        return self._pos

    def _require_open(self):
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def seek(self, offset, whence=io.SEEK_SET):
        with self._lock:
            self._require_open()
            if whence == io.SEEK_SET:
                target = offset
            elif whence == io.SEEK_CUR:
                target = self._pos + offset
            elif whence == io.SEEK_END:
                target = self._ensure_size() + offset
            else:
                raise ValueError(f"invalid whence: {whence}")
            if target < 0:
                raise ValueError("negative seek position")
            self._pos = target
            return self._pos

    def read(self, size=-1):
        with self._lock:
            self._require_open()
            total = self._ensure_size()
            available = max(0, total - self._pos)
            remaining = available if size is None or size < 0 else min(size, available)
            parts = []
            while remaining > 0:
                chunk = self._read_at(self._pos, remaining)
                if not chunk:
                    break
                parts.append(chunk)
                self._pos += len(chunk)
                remaining -= len(chunk)
            return b"".join(parts)

    def readall(self):
        return self.read(-1)

    def readinto(self, buffer):
        view = memoryview(buffer).cast("B")
        data = self.read(len(view))
        view[: len(data)] = data
        return len(data)

    def close(self):
        self._windows = []
        super().close()

    def _ensure_url(self):
        expired = time.monotonic() - self._url_at > self._url_ttl
        if self._url is None or expired:
            self._url = self._client.resolve_url(self._artifact_id)
            self._url_at = time.monotonic()
            self.resolves += 1
        return self._url

    def _ensure_size(self):
        if self._size is None:
            self._fetch(0, 0)
            if self._size is None:
                raise ArtifactError("server did not report the artifact size")
        return self._size

    def _fetch(self, start, end):
        attempts = self._client._reresolve_limit + 1
        for attempt in range(attempts):
            url = self._ensure_url()
            last = attempt + 1 >= attempts
            reply = self._client._request(
                "GET", url, headers={"Range": f"bytes={start}-{end}"}
            )
            with reply:
                status = reply.status
                self.requests += 1
                if status in _RERESOLVE_STATUSES:
                    self._url = None
                    if last:
                        raise ArtifactError(
                            f"blob URL rejected after re-resolving (HTTP {status})", status
                        )
                    continue
                if status == 416:
                    match = re.search(r"bytes\s+\*/(\d+)", reply.headers.get("Content-Range", ""))
                    if match and self._size is None:
                        self._size = int(match.group(1))
                    return b""
                if status not in (200, 206):
                    raise self._client._error_for(reply, "ranged read")
                try:
                    if status == 206:
                        data = reply.read()
                        match = _CONTENT_RANGE_RE.match(reply.headers.get("Content-Range", ""))
                        if match and match.group(3).isdigit() and self._size is None:
                            self._size = int(match.group(3))
                    else:
                        declared = _int_or_none(reply.headers.get("Content-Length"))
                        if declared is not None and self._size is None:
                            self._size = declared
                        data = reply.read(end + 1)[start : end + 1]
                except (OSError, http.client.HTTPException) as exc:
                    if last:
                        raise ArtifactError(
                            f"connection lost during ranged read ({type(exc).__name__})"
                        ) from None
                    self._client._sleep(self._client._backoff(attempt))
                    continue
                self.bytes_fetched += len(data)
                return data
        raise ArtifactError("ranged read failed")

    def _from_windows(self, pos, want):
        for index, (start, data) in enumerate(self._windows):
            if start <= pos < start + len(data):
                if index:
                    self._windows.insert(0, self._windows.pop(index))
                return data[pos - start : pos - start + want]
        return b""

    def _read_at(self, pos, want):
        cached = self._from_windows(pos, want)
        if cached:
            return cached
        total = self._size
        fetch_len = min(max(want, self._read_ahead), self._max_fetch)
        start = pos
        if not self._windows and want < self._read_ahead and pos + fetch_len >= total:
            start = max(0, total - self._read_ahead)
        end = min(total, start + max(fetch_len, 1)) - 1
        if start > end:
            return b""
        data = self._fetch(start, end)
        if len(data) <= self._cache_limit:
            self._windows.insert(0, (start, data))
            del self._windows[self._cache_windows :]
        chunk = data[pos - start : pos - start + want]
        if not chunk:
            raise ArtifactError("ranged read returned no data before end of artifact")
        return chunk


def from_environment(environ=None, **options):
    env = os.environ if environ is None else environ
    token = None
    for key in ("ARTSTORE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = (env.get(key) or "").strip()
        if value:
            token = value
            break
    repo = (env.get("GITHUB_REPOSITORY") or "").strip()
    if not token or not _REPO_RE.match(repo):
        return None
    api = options.pop("api", None) or (env.get("GITHUB_API_URL") or "").strip() or DEFAULT_API
    return ArtifactsClient(repo, token, api=api, **options)
