#!/usr/bin/env python3
"""Archive cache shared by installer adapters; Python 3.10+, standard library only."""

import argparse
from email.message import EmailMessage
from email.policy import default as email_policy
import hashlib
from http.client import HTTPException
from http.cookiejar import CookieJar
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
import zipfile


ARCHIVE = re.compile(r"\.(zip|7z|rar)$", re.I)
LINK = re.compile(r'\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)|<((?:https?)://[^>]+)>|^\s*\[[^\]]+\]:\s*(\S+)', re.M)
HTTP = build_opener(HTTPCookieProcessor(CookieJar()))


def clean_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host += ":" + str(parsed.port)
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def key_for(url):
    return hashlib.sha256(url.encode()).hexdigest()


def checked_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("unsupported URL")
    return url


def provider(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if host == "mega.nz" or host.endswith(".mega.nz") or host == "mega.co.nz":
        return "mega", False
    if host == "nexusmods.com" or host.endswith(".nexusmods.com"):
        return "nexus", False
    if host == "deadlystream.com" or host.endswith(".deadlystream.com"):
        direct = (query.get("do") == ["download"] and bool(re.match(r"^/files/file/\d+", parsed.path))) or parsed.path.endswith("/attachment.php")
        return "deadlystream", direct
    if host in ("drive.google.com", "www.drive.google.com"):
        return "google-drive", False
    if host == "gamefront.com" or host.endswith(".gamefront.com"):
        return "gamefront", False
    return "http", bool(ARCHIVE.search(parsed.path))


def archive_link(url):
    parsed = urlsplit(url)
    kind, direct = provider(url)
    if direct:
        return True
    if kind == "deadlystream":
        return bool(re.match(r"^/files/file/\d+(?:[-/]|$)", parsed.path))
    if kind == "nexus":
        return bool(re.match(r"^/[^/]+/mods/\d+(?:/|$)", parsed.path))
    if kind == "mega":
        return parsed.path.startswith(("/file/", "/folder/")) or parsed.fragment.startswith(("!", "F!"))
    if kind == "google-drive":
        return parsed.path.startswith("/file/d/")
    if kind == "gamefront":
        return bool(re.match(r"^/games/[^/]+/file/[^/]+", parsed.path))
    return False


def endpoint_id(url):
    parsed = urlsplit(url)
    query = sorted((name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True)
                   if name.lower() != "csrfkey")
    return key_for(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))


def version_id(url):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    for name in ("file_id", "fileid", "id", "version", "attachmentid"):
        if name in query:
            return name + ":" + query[name][0]
    match = re.search(r"/releases/download/([^/]+)/", parsed.path)
    return match[1] if match else None


def discover(guides):
    found = {}
    for guide in guides:
        source = Path(guide).read_text(encoding="utf-8")
        source = re.sub(r"```.*?```|~~~.*?~~~", "", source, flags=re.S)
        source = re.sub(r"`[^`\n]*`", "", source)
        for match in LINK.finditer(source):
            url = html.unescape(next(value for value in match.groups() if value)).strip("<>")
            if not url.startswith(("https://", "http://")):
                continue
            checked_url(url)
            if not archive_link(url):
                continue
            kind, direct = provider(url)
            identity = key_for(url)
            if identity not in found:
                found[identity] = {"id": identity, "url": url, "provider": kind,
                                   "direct": direct, "guides": []}
            if str(guide) not in found[identity]["guides"]:
                found[identity]["guides"].append(str(guide))
    return list(found.values())


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".json-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate(path, timeout):
    with path.open("rb") as stream:
        magic = stream.read(512)
    if magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        subprocess.run([sys.executable, "-c",
                        "import sys,zipfile; archive=zipfile.ZipFile(sys.argv[1]); "
                        "sys.exit(1 if archive.testzip() else 0)", str(path.resolve())],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout, check=True)
        return "zip"
    kind = "7z" if magic.startswith(b"7z\xbc\xaf\x27\x1c") else "rar" if magic.startswith(b"Rar!\x1a\x07") else None
    if not kind:
        raise ValueError("not a supported archive (HTML and unknown payloads rejected)")
    executable = shutil.which("7zz") or shutil.which("7z")
    if not executable:
        raise ValueError("7z or 7zz required for RAR/7z integrity")
    subprocess.run([executable, "t", "-bd", "-y", "-p", str(path.resolve())],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=timeout, check=True)
    return kind


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.extend(value for name, value in attrs if name == "href" and value)


def request(url, args, headers=None):
    checked_url(url)
    for attempt in range(args.retries + 1):
        try:
            response = HTTP.open(Request(url, headers={"User-Agent": "modsync-cache/1", **(headers or {})}),
                               timeout=args.timeout)
            checked_url(response.geturl())
            return response
        except HTTPError as error:
            if error.code == 304:
                return error
            error.close()
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == args.retries:
                raise ValueError("HTTP " + str(error.code)) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            if attempt == args.retries:
                raise ValueError("network request failed") from None
        time.sleep(min(2 ** attempt, 8))


def page_endpoint(response, item):
    body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("landing page too large")
    source = body.decode("utf-8", errors="replace")
    if re.search(r"cf-turnstile|g-recaptcha|h-captcha|/cdn-cgi/challenge-platform|<title>\s*Just a moment", source, re.I):
        raise ValueError("provider challenge/CAPTCHA; blocked-pending-user-action")
    parser = Links()
    parser.feed(source)
    candidates = set()
    origin = urlsplit(item["url"])
    for href in parser.urls:
        url = urljoin(response.geturl(), html.unescape(href))
        parsed = urlsplit(url)
        kind, direct = provider(url)
        if not direct or kind != item["provider"]:
            continue
        if kind == "deadlystream" and (parsed.hostname != origin.hostname or
                (not parsed.path.endswith("/attachment.php") and parsed.path.rstrip("/") != origin.path.rstrip("/"))):
            continue
        candidates.add(checked_url(url))
    attachments = {url for url in candidates if "r" in parse_qs(urlsplit(url).query) or
                   urlsplit(url).path.endswith("/attachment.php")}
    candidates = attachments or candidates
    if len(candidates) != 1:
        raise ValueError("landing page has no unique archive endpoint; attachment selection/provider adapter required")
    return candidates.pop()


def resolve(item, args):
    if item["provider"] in ("mega", "nexus", "google-drive", "gamefront"):
        raise ValueError(item["provider"] + " requires an authenticated/provider adapter")
    if item["direct"]:
        return item["url"]
    with request(item["url"], args) as response:
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("landing URL has no explicit archive endpoint")
        return page_endpoint(response, item)


def object_path(root, entry):
    sha = entry.get("sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("invalid manifest SHA256")
    return root / "objects" / sha


def local_valid(root, entry):
    try:
        path = object_path(root, entry)
        return path.is_file() and path.stat().st_size == entry["size"] and digest(path) == entry["sha256"]
    except (ValueError, KeyError, OSError):
        return False


def safe_archive_name(value, extension):
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFC", value).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character if character.isalnum() or character in " ._-()[]+" else "_"
                   for character in name).strip(" .")
    if not name or len(name.encode("utf-8")) > 240 or not ARCHIVE.search(name):
        return None
    stem, suffix = name.rsplit(".", 1)
    if not stem.strip(" ._") or suffix.lower() != extension:
        return None
    if stem.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(
            prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10))}:
        name = "_" + name
    return name


def archive_name(item, extension, url, disposition=None):
    if disposition:
        try:
            message = EmailMessage(policy=email_policy)
            message["Content-Disposition"] = disposition
            name = safe_archive_name(message.get_filename(), extension)
            if name:
                return name
        except (ValueError, TypeError, IndexError):
            pass
    name = safe_archive_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1]), extension)
    if name:
        return name
    basename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    basename = re.sub(r"[^a-zA-Z0-9_.-]+", "_", unicodedata.normalize("NFC", basename)).strip(" ._")
    basename = basename[:80] or "archive"
    return basename + "-" + item["id"] + "." + extension


def refresh_one(item, old, root, args, endpoint=None, depth=0):
    endpoint = endpoint or resolve(item, args)
    endpoint_key = endpoint_id(endpoint)
    valid = local_valid(root, old)
    headers = {"Referer": item["url"]} if item["provider"] == "deadlystream" else {}
    if valid and old.get("endpoint_id") == endpoint_key:
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        if old.get("last_modified"):
            headers["If-Modified-Since"] = old["last_modified"]
    with request(endpoint, args, headers) as response:
        now = time.time()
        if response.code == 304:
            if not any(name in headers for name in ("If-None-Match", "If-Modified-Since")) or not valid:
                raise ValueError("unexpected 304 without a verified cached object")
            validate(object_path(root, old), args.integrity_timeout)
            name = archive_name(item, old["extension"], response.geturl(),
                                response.headers.get("Content-Disposition"))
            return {**old, "archive_name": old.get("archive_name", name),
                    "checked_at": now, "status": "fresh", "evidence": "conditional-304"}
        if response.code != 200:
            raise ValueError("expected complete HTTP 200 archive")
        if "html" in response.headers.get("Content-Type", "").lower():
            if item["provider"] != "deadlystream" or depth >= 2:
                raise ValueError("archive endpoint returned HTML; provider adapter required")
            attachment = page_endpoint(response, item)
            if endpoint_id(attachment) == endpoint_key:
                raise ValueError("download endpoint did not resolve to an attachment; provider adapter required")
            response.close()
            return refresh_one(item, old, root, args, attachment, depth + 1)
        descriptor, temporary = tempfile.mkstemp(dir=root / "objects", prefix=".download-")
        path = Path(temporary)
        try:
            size = 0
            started = time.monotonic()
            with os.fdopen(descriptor, "wb") as stream:
                while True:
                    if time.monotonic() - started > args.download_timeout:
                        raise ValueError("total download timeout")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > args.max_bytes:
                        raise ValueError("archive exceeds size limit")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            expected = response.headers.get("Content-Length")
            if expected is not None and size != int(expected):
                raise ValueError("truncated archive")
            kind = validate(path, args.integrity_timeout)
            sha = digest(path)
            if not (valid and old.get("sha256") == sha):
                os.replace(path, root / "objects" / sha)
            etag = response.headers.get("ETag")
            modified = response.headers.get("Last-Modified")
            version = version_id(response.geturl())
            known = bool(etag or modified)
            return {"sha256": sha, "size": size, "extension": kind,
                    "archive_name": archive_name(item, kind, response.geturl(),
                                                 response.headers.get("Content-Disposition")),
                    "endpoint_id": endpoint_key, "endpoint": clean_url(response.geturl()),
                    "etag": etag, "last_modified": modified, "version_id": version,
                    "checked_at": time.time(), "status": "fresh" if known else "verified",
                    "metadata_unknown": not known,
                    "evidence": "download-200" if known else "download-without-upstream-metadata"}
        finally:
            path.unlink(missing_ok=True)


def rclone(args, direction, root):
    if not args.remote:
        return
    command = ["rclone", "copy", args.remote if direction == "pull" else str(root),
               str(root) if direction == "pull" else args.remote,
               "--exclude", "**/.download-*", "--include", "/manifest.json",
               "--include", "/objects/**", "--retries", "2", "--contimeout", "20s",
               "--timeout", "60s"]
    subprocess.run(command, check=True, timeout=args.backend_timeout,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "refresh", "restore"))
    parser.add_argument("--guide", action="append", help="repeatable full.md path (default both games)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".modsync-cache"))
    parser.add_argument("--destination", type=Path, help="archive directory for either installer")
    parser.add_argument("--report", type=Path, help="also write sanitized JSON report")
    parser.add_argument("--remote", help="optional rclone remote:path; trusted operator input only")
    parser.add_argument("--force", action="store_true", help="refresh even inside freshness interval")
    parser.add_argument("--max-age-days", type=float, default=14)
    parser.add_argument("--timeout", type=float, default=30, help="socket timeout seconds")
    parser.add_argument("--download-timeout", type=float, default=1800)
    parser.add_argument("--integrity-timeout", type=float, default=300)
    parser.add_argument("--backend-timeout", type=float, default=1800)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 ** 3)
    args = parser.parse_args(argv)
    if not 0 <= args.retries <= 5 or not 0 < args.max_age_days <= 14:
        parser.error("retries must be 0..5; max-age-days must be >0 and <=14")
    if any(value <= 0 for value in (args.timeout, args.download_timeout, args.integrity_timeout,
                                    args.backend_timeout, args.max_bytes)):
        parser.error("timeouts and max-bytes must be positive")
    if args.command == "restore" and args.destination is None:
        parser.error("restore requires --destination")
    guides = args.guide or ["content/k1/full.md", "content/k2/full.md"]
    items = discover(guides)
    if not items:
        raise ValueError("no HTTP Markdown links discovered")
    report = {"schema": 1, "command": args.command, "entries": []}
    if args.command == "discover":
        report["entries"] = [{**item, "url": clean_url(item["url"])} for item in items]
    else:
        root = args.cache_dir.resolve()
        (root / "objects").mkdir(parents=True, exist_ok=True)
        rclone(args, "pull", root)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema": 1, "entries": {}}
        if manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict):
            raise ValueError("unsupported manifest schema")
        restored_names = {}
        for item in items:
            old = manifest["entries"].get(item["id"], {})
            entry = {"id": item["id"], "url": clean_url(item["url"]), "provider": item["provider"]}
            try:
                valid = local_valid(root, old)
                age = time.time() - old.get("checked_at", 0)
                upstream_checked = old.get("status") == "fresh" or (
                    old.get("status") == "verified" and
                    old.get("evidence") == "download-without-upstream-metadata")
                fresh = valid and upstream_checked and 0 <= age < args.max_age_days * 86400
                if args.command == "refresh":
                    cached = old if fresh and not args.force else refresh_one(item, old, root, args)
                    manifest["entries"][item["id"]] = cached
                    entry.update(cached)
                    atomic_json(manifest_path, manifest)
                else:
                    if not fresh:
                        raise ValueError("cache missing, corrupt, stale or upstream verification failed; run refresh")
                    source = object_path(root, old)
                    validate(source, args.integrity_timeout)
                    args.destination.mkdir(parents=True, exist_ok=True)
                    if old.get("extension") not in ("zip", "7z", "rar"):
                        raise ValueError("invalid manifest archive extension")
                    if "archive_name" in old:
                        filename = safe_archive_name(old["archive_name"], old["extension"])
                        if filename is None:
                            raise ValueError("invalid manifest archive name")
                    else:
                        filename = archive_name(item, old["extension"], old.get("endpoint") or item["url"])
                    name_key = filename.casefold()
                    if name_key in restored_names and restored_names[name_key] != item["id"]:
                        raise ValueError("archive filename collision")
                    restored_names[name_key] = item["id"]
                    matches = [path for path in args.destination.iterdir()
                               if unicodedata.normalize("NFC", path.name).casefold() == name_key]
                    if matches:
                        if (len(matches) != 1 or matches[0].name != filename or matches[0].is_symlink()
                                or not matches[0].is_file() or digest(matches[0]) != old["sha256"]):
                            raise ValueError("archive filename collision")
                    descriptor, temporary = tempfile.mkstemp(dir=args.destination, prefix=".restore-")
                    try:
                        with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, output)
                            output.flush()
                            os.fsync(output.fileno())
                        if digest(Path(temporary)) != old["sha256"]:
                            raise ValueError("restore SHA256 mismatch")
                        if not matches:
                            os.link(temporary, args.destination / filename)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                    entry.update(status="restored", sha256=old["sha256"], file=filename,
                                 metadata_unknown=old.get("metadata_unknown", not bool(
                                     old.get("etag") or old.get("last_modified"))))
            except (ValueError, OSError, HTTPException, subprocess.SubprocessError, zipfile.BadZipFile) as error:
                entry.update(status="unresolved", reason=safe_reason(error))
                if args.command == "refresh" and old:
                    manifest["entries"][item["id"]] = {**old, "status": "stale-unknown"}
                    atomic_json(manifest_path, manifest)
            report["entries"].append(entry)
        if args.command == "refresh":
            rclone(args, "push", root)
    report["ok"] = all(entry.get("status") in ("fresh", "verified", "restored") for entry in report["entries"]) if args.command != "discover" else True
    if args.report:
        atomic_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def safe_reason(error):
    if isinstance(error, ValueError):
        return str(error) if not re.search(r"https?://", str(error)) else "invalid upstream response"
    return type(error).__name__


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as failure:
        print(json.dumps({"ok": False, "reason": safe_reason(failure)}))
        sys.exit(1)
