#!/usr/bin/env python3
"""Archive cache shared by installer adapters; Python 3.10+, standard library only."""

import argparse
from email.message import EmailMessage
from email.policy import default as email_policy
import hashlib
import importlib.util
from http.client import HTTPException
from http.cookiejar import CookieJar
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import stat
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
DISK_RESERVE_BYTES = 3 * 1024 ** 3
MIN_TOOL_ARCHIVE_BYTES = 64
TOOL_PROVIDERS = ("mega", "google-drive")
NEXUS_REASON = "nexus requires NEXUS_API_KEY (no headless adapter; CAPTCHA is never bypassed); blocked-pending-user-action"


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
    return run_integrity_tools(path, kind, timeout)


def integrity_tools(path, kind):
    target = str(path.resolve())
    tools, seen = [], set()
    unrar = shutil.which("unrar") if kind == "rar" else None
    if unrar:
        tools.append(("unrar", [unrar, "t", "-idq", "-y", "-p-", target]))
    for name in ("7zz", "7z"):
        executable = shutil.which(name)
        if executable and os.path.realpath(executable) not in seen:
            seen.add(os.path.realpath(executable))
            tools.append((name, [executable, "t", "-bd", "-y", "-p", target]))
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        tools.append(("bsdtar", [bsdtar, "-xOf", target]))
    return tools


def run_integrity_tools(path, kind, timeout):
    tools = integrity_tools(path, kind)
    if not tools:
        raise ValueError("7z or 7zz required for RAR/7z integrity")
    failed = []
    for name, command in tools:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=timeout)
        except OSError:
            continue
        if result.returncode == 0:
            return kind
        if result.returncode > 0 and kind != "rar":
            raise subprocess.CalledProcessError(result.returncode, [name])
        failed.append(name)
    if not failed:
        raise ValueError("7z or 7zz required for RAR/7z integrity")
    raise ValueError("archive integrity check failed in every available tool (" + ", ".join(failed) + ")")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.extend(value for name, value in attrs if name == "href" and value)


class DownloadRows(HTMLParser):
    """Collects (file name, download href) rows from a DeadlyStream file-selection page."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.title_tag = None
        self.title_depth = 0

    def finish_row(self):
        if self.row is not None and self.row["href"]:
            self.rows.append({"name": " ".join("".join(self.row["name"]).split()) or None,
                              "href": self.row["href"]})
        self.row = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "li" and "ipsDataItem" in classes:
            self.finish_row()
            self.row = {"name": [], "href": None}
            self.title_tag = None
        elif self.row is not None:
            if self.title_tag is None and "ipsDataItem_title" in classes:
                self.title_tag, self.title_depth = tag, 1
            elif self.title_tag == tag:
                self.title_depth += 1
            if tag == "a" and values.get("href") and not self.row["href"]:
                query = parse_qs(urlsplit(html.unescape(values["href"])).query)
                if query.get("do") == ["download"] and "r" in query:
                    self.row["href"] = values["href"]

    def handle_endtag(self, tag):
        if self.title_tag == tag:
            self.title_depth -= 1
            if self.title_depth == 0:
                self.title_tag = None

    def handle_data(self, data):
        if self.row is not None and self.title_tag is not None:
            self.row["name"].append(data)

    def close(self):
        super().close()
        self.finish_row()


SELECTIONS_PATH = Path(__file__).with_name("archive_selections.json")
FILE_ID = re.compile(r"/files/file/([0-9]+)(?:[-/]|$)")


def selection_key(key):
    key = str(key).strip()
    if key.isdigit():
        return key
    match = FILE_ID.search(urlsplit(key).path) if key.startswith(("https://", "http://")) else None
    if not match:
        raise ValueError("archive selection key must be a DeadlyStream file id or file page URL")
    return match[1]


def load_selections(path=None):
    path = Path(path or SELECTIONS_PATH)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    selections = {}
    for key, value in (document.get("selections") or {}).items():
        name = value if isinstance(value, str) else (value or {}).get("file")
        if not isinstance(name, str) or not ARCHIVE.search(name):
            raise ValueError("archive selection for " + str(key) + " must name an archive file")
        selections[selection_key(key)] = name
    return selections


def describe_rows(rows):
    shown = ["%s (r=%s)" % (json.dumps(row["name"]) if row["name"] else "unnamed", row["r"]) for row in rows[:12]]
    return "; ".join(shown) + ("; ..." if len(rows) > 12 else "")


def choose_download(rows, file_id, selections):
    reviewed = selections.get(file_id)
    if reviewed:
        matches = [row for row in rows if row["name"] == reviewed]
        if len(matches) == 1:
            return matches[0]["url"]
        raise ValueError("reviewed archive selection for file %s matches %d of the listed files; "
                         "re-review archive_selections.json; candidates: %s" % (file_id, len(matches), describe_rows(rows)))
    if len(rows) == 1:
        return rows[0]["url"]
    archives = [row for row in rows if row["name"] and ARCHIVE.search(row["name"])]
    if len(archives) == 1 and all(row["name"] for row in rows):
        return archives[0]["url"]
    raise ValueError("landing page lists multiple files and no reviewed selection exists for file %s; "
                     "add an exact file name to archive_selections.json; candidates: %s"
                     % (file_id, describe_rows(rows)))


def deadlystream_endpoint(source, base, candidates, file_id, selections):
    def query_of(url):
        return parse_qs(urlsplit(url).query)

    selection = {url for url in candidates if query_of(url).get("do") == ["download"] and "r" in query_of(url)}
    if selection:
        named = DownloadRows()
        named.feed(source)
        named.close()
        by_url = {}
        for row in named.rows:
            url = urljoin(base, html.unescape(row["href"]))
            if url in selection:
                by_url[url] = {"name": row["name"], "url": url}
        rows = [{**by_url.get(url, {"name": None, "url": url}), "r": query_of(url)["r"][0]}
                for url in sorted(selection, key=lambda url: (len(query_of(url)["r"][0]), query_of(url)["r"][0]))]
        return choose_download(rows, file_id, selections)
    downloads = {url for url in candidates if query_of(url).get("do") == ["download"]}
    pool = downloads or candidates
    if len(pool) != 1:
        raise ValueError("landing page has no unique archive endpoint; attachment selection/provider adapter required")
    return next(iter(pool))


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


def page_endpoint(response, item, selections=None):
    origin = urlsplit(item["url"])
    if item["provider"] == "deadlystream":
        canonical = urlsplit(checked_url(response.geturl()))
        for parsed, ports in ((origin, {"http": 80, "https": 443}), (canonical, {"https": 443})):
            checked_url(parsed.geturl())
            if (parsed.scheme not in ports or parsed.hostname not in ("deadlystream.com", "www.deadlystream.com")
                    or parsed.port not in (None, ports[parsed.scheme])):
                raise ValueError("unsupported provider redirect")
        original_file = re.match(r"^/files/file/([0-9]+)(?:[-/]|$)", origin.path)
        canonical_file = re.match(r"^/files/file/([0-9]+)(?:[-/]|$)", canonical.path)
        if not original_file or not canonical_file or original_file[1] != canonical_file[1]:
            raise ValueError("provider redirect changed file identity")
        origin = canonical
    body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("landing page too large")
    source = body.decode("utf-8", errors="replace")
    if re.search(r"cf-turnstile|g-recaptcha|h-captcha|/cdn-cgi/challenge-platform|<title>\s*Just a moment", source, re.I):
        raise ValueError("provider challenge/CAPTCHA; blocked-pending-user-action")
    parser = Links()
    parser.feed(source)
    candidates = set()
    for href in parser.urls:
        url = urljoin(response.geturl(), html.unescape(href))
        parsed = urlsplit(url)
        kind, direct = provider(url)
        if not direct or kind != item["provider"]:
            continue
        if kind == "deadlystream" and (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.hostname != origin.hostname or
                (not parsed.path.endswith("/attachment.php") and parsed.path.rstrip("/") != origin.path.rstrip("/"))):
            continue
        candidates.add(checked_url(url))
    if item["provider"] == "deadlystream":
        selections = load_selections() if selections is None else selections
        file_id = FILE_ID.match(origin.path)[1]
        return deadlystream_endpoint(source, response.geturl(), candidates, file_id, selections)
    if len(candidates) != 1:
        raise ValueError("landing page has no unique archive endpoint; attachment selection/provider adapter required")
    return candidates.pop()


def tool_base(provider_name):
    if provider_name == "mega":
        for name, prefix in (("megatools", ["dl"]), ("megadl", [])):
            executable = shutil.which(name)
            if executable:
                return [executable, *prefix, "--path"]
        raise ValueError("mega download requires megatools (megatools or megadl) on PATH; not installed")
    if provider_name == "google-drive":
        executable = shutil.which("gdown")
        if executable:
            return [executable, "--quiet", "-O"]
        if importlib.util.find_spec("gdown"):
            return [sys.executable, "-m", "gdown", "--quiet", "-O"]
        raise ValueError("google-drive download requires gdown (pip install gdown); not installed")
    raise ValueError(provider_name + " has no tool adapter")


def tool_command(provider_name, directory, url):
    target = str(directory) + (os.sep if provider_name == "google-drive" else "")
    return [*tool_base(provider_name), target, url]


def resolve(item, args):
    if item["provider"] == "nexus":
        raise ValueError(NEXUS_REASON)
    if item["provider"] == "gamefront":
        raise ValueError("gamefront requires an authenticated/provider adapter")
    if item["provider"] in TOOL_PROVIDERS:
        tool_base(item["provider"])
        return item["url"]
    if item["direct"]:
        return item["url"]
    with request(item["url"], args) as response:
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("landing URL has no explicit archive endpoint")
        return page_endpoint(response, item)


def fetch_with_tool(item, root, args):
    objects = root / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=objects, prefix=".tool-"))
    try:
        command = tool_command(item["provider"], workdir, item["url"])
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=args.download_timeout)
        except subprocess.TimeoutExpired:
            raise ValueError("total download timeout") from None
        except OSError:
            raise ValueError(item["provider"] + " download tool could not be executed") from None
        if result.returncode != 0:
            raise ValueError(item["provider"] + " download tool failed (exit " + str(result.returncode) + ")")
        files = [candidate for candidate in workdir.rglob("*") if candidate.is_file() or candidate.is_symlink()]
        if len(files) != 1 or files[0].is_symlink():
            raise ValueError(item["provider"] + " download did not yield exactly one regular file")
        path = files[0]
        size = path.stat().st_size
        if size < MIN_TOOL_ARCHIVE_BYTES:
            raise ValueError("downloaded file below minimum archive size (likely an error page)")
        if size > args.max_bytes:
            raise ValueError("archive exceeds size limit")
        kind = validate(path, args.integrity_timeout)
        sha = digest(path)
        os.replace(path, objects / sha)
        name = safe_archive_name(path.name, kind) or archive_name(item, kind, item["url"])
        return {"sha256": sha, "size": size, "extension": kind, "archive_name": name,
                "endpoint_id": item["id"], "endpoint": clean_url(item["url"]),
                "etag": None, "last_modified": None, "version_id": None,
                "checked_at": time.time(), "status": "verified", "metadata_unknown": True,
                "evidence": "download-without-upstream-metadata"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
    if item["provider"] in TOOL_PROVIDERS:
        return fetch_with_tool(item, root, args)
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


def existing_archive_path(archive_root, filename):
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ValueError("unsafe archive path")
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError("unsafe archive path")
    root = archive_root.resolve(strict=True)
    path = root / filename
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ValueError("archive missing") from None
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
        raise ValueError("archive must be a regular file without links")
    return path


def import_existing(item, resource_entries, archive_root, root, args):
    matches = [(key, value) for key, value in resource_entries.items()
               if isinstance(value, dict)
               and isinstance(value.get("HandlerMetadata"), dict)
               and value["HandlerMetadata"].get("PrimaryUrl") == item["url"]]
    if len(matches) != 1:
        return {"id": item["id"], "status": "ambiguous-provider-url" if matches else "unmatched"}
    _, resource_entry = matches[0]
    files = resource_entry.get("Files")
    if not isinstance(files, dict) or len(files) != 1:
        return {"id": item["id"], "status": "ambiguous-variant"}
    filename = next(iter(files))
    try:
        source = existing_archive_path(archive_root, filename)
        extension = source.suffix.lower().lstrip(".")
        if safe_archive_name(filename, extension) != filename:
            raise ValueError("unsafe archive name")
    except (OSError, ValueError) as error:
        return {"id": item["id"], "status": "unresolved", "reason": safe_reason(error)}
    entry = {"id": item["id"], "archive_name": filename, "status": "planned"}
    manifest_path = root / "manifest.json"
    manifest = args.import_manifest
    old = manifest.get(item["id"])
    if item["id"] in manifest and not args.apply:
        entry["status"] = "existing-association"
        return entry
    if not args.apply:
        return entry
    try:
        expected_size = source.stat().st_size
        if expected_size < 0 or expected_size > args.max_bytes:
            raise ValueError("archive exceeds size limit")
    except OSError as error:
        return {"id": item["id"], "status": "unresolved", "reason": safe_reason(error)}
    objects = root / "objects"
    if objects.is_symlink() or (objects.exists() and not objects.is_dir()):
        return {"id": item["id"], "status": "unresolved", "reason": "unsafe cache object path"}
    objects.mkdir(parents=True, exist_ok=True)
    if expected_size + DISK_RESERVE_BYTES > shutil.disk_usage(objects).free:
        return {"id": item["id"], "status": "unresolved", "reason": "insufficient disk space"}
    descriptor, temporary = tempfile.mkstemp(dir=objects, prefix=".import-")
    staged = Path(temporary)
    digest_state = hashlib.sha256()
    size = 0
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, flags)
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
                raise ValueError("archive must be a regular file without links")
            if source_stat.st_size != expected_size:
                raise ValueError("archive changed before import")
            with os.fdopen(source_fd, "rb") as incoming, os.fdopen(descriptor, "wb") as output:
                source_fd = -1
                descriptor = -1
                while True:
                    chunk = incoming.read(min(1024 * 1024, expected_size - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size or size > args.max_bytes:
                        raise ValueError("archive exceeds size limit")
                    digest_state.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if source_fd >= 0:
                os.close(source_fd)
        if size != expected_size:
            raise ValueError("archive changed during import")
        kind = validate(staged, args.integrity_timeout)
        sha = digest_state.hexdigest()
        object_file = objects / sha
        if item["id"] in manifest:
            if not isinstance(old, dict) or old.get("sha256") != sha or old.get("size") != size:
                entry.update(status="manifest-conflict", sha256=sha, size=size)
                return entry
            if object_file.is_symlink():
                entry.update(status="manifest-conflict", sha256=sha, size=size)
                return entry
            if object_file.exists():
                if not local_valid(root, old):
                    entry.update(status="manifest-conflict", sha256=sha, size=size)
                    return entry
            else:
                os.link(staged, object_file)
            entry.update(status="already-present", sha256=sha, size=size)
            return entry
        try:
            os.link(staged, object_file)
        except FileExistsError:
            if object_file.is_symlink() or not object_file.is_file() or object_file.stat().st_size != size:
                raise ValueError("cache object conflict")
            if digest(object_file) != sha:
                raise ValueError("cache object conflict")
            validate(object_file, args.integrity_timeout)
        imported = {"sha256": sha, "size": size, "extension": kind,
                    "archive_name": filename, "status": "stale-unknown",
                    "metadata_unknown": True, "evidence": "local-import",
                    "imported_at": time.time()}
        manifest[item["id"]] = imported
        try:
            args.import_manifest_document["entries"] = manifest
            atomic_json(manifest_path, args.import_manifest_document)
        except OSError:
            manifest.pop(item["id"], None)
            raise
        entry.update({"status": imported["status"], "sha256": sha, "size": size})
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        entry.update(status="unresolved", reason=safe_reason(error))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)
    return entry


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
    parser.add_argument("command", choices=("discover", "refresh", "restore", "import-existing"))
    parser.add_argument("--guide", action="append", help="repeatable full.md path (default both games)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".modsync-cache"))
    parser.add_argument("--destination", type=Path, help="archive directory for either installer")
    parser.add_argument("--resource-index", type=Path, help="resource index for import-existing")
    parser.add_argument("--archive-dir", type=Path, help="existing archive directory for import-existing")
    parser.add_argument("--max-items", type=int, default=20,
                        help="maximum exact-URL matched candidates to inspect (1..500)")
    parser.add_argument("--offset", type=int, default=0,
                        help="start at this exact-URL matched candidate index")
    parser.add_argument("--apply", action="store_true", help="copy and record import-existing matches")
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
    if args.command == "import-existing":
        if args.resource_index is None or args.archive_dir is None:
            parser.error("import-existing requires --resource-index and --archive-dir")
        if not 1 <= args.max_items <= 500:
            parser.error("import-existing --max-items must be 1..500")
        if args.offset < 0:
            parser.error("import-existing --offset must be >=0")
        if args.remote:
            parser.error("import-existing does not support --remote")
    elif args.apply:
        parser.error("--apply is only valid with import-existing")
    guides = args.guide or ["content/k1/full.md", "content/k2/full.md"]
    items = discover(guides)
    if not items:
        raise ValueError("no HTTP Markdown links discovered")
    report = {"schema": 1, "command": args.command, "entries": []}
    if args.command == "discover":
        report["entries"] = [{**item, "url": clean_url(item["url"])} for item in items]
    elif args.command == "import-existing":
        archive_root = args.archive_dir.resolve(strict=True)
        resource_index = json.loads(args.resource_index.read_text(encoding="utf-8"))
        resource_entries = resource_index.get("entries") if isinstance(resource_index, dict) else None
        if not isinstance(resource_entries, dict):
            raise ValueError("invalid resource index")
        root = args.cache_dir.resolve()
        manifest_path = root / "manifest.json"
        manifest = {"schema": 1, "entries": {}}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict):
            raise ValueError("unsupported manifest schema")
        manifest.setdefault("entries", {})
        matched_items = [item for item in items if any(
            isinstance(value, dict)
            and isinstance(value.get("HandlerMetadata"), dict)
            and value["HandlerMetadata"].get("PrimaryUrl") == item["url"]
            for value in resource_entries.values())]
        args.import_manifest = manifest["entries"]
        args.import_manifest_document = manifest
        start = min(args.offset, len(matched_items))
        selected = matched_items[start:start + args.max_items]
        report["entries"] = [import_existing(item, resource_entries, archive_root, root, args)
                              for item in selected]
        report["candidate_count"] = len(matched_items)
        report["next_offset"] = start + len(selected)
        report["unmatched_urls"] = len(items) - len(matched_items)
        report["truncated"] = report["next_offset"] < len(matched_items)
        report["ok"] = all(entry["status"] in (
            "planned", "existing-association", "already-present", "stale-unknown")
                           for entry in report["entries"])
    else:
        root = args.cache_dir.resolve()
        (root / "objects").mkdir(parents=True, exist_ok=True)
        rclone(args, "pull", root)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema": 1, "entries": {}}
        if manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict):
            raise ValueError("unsupported manifest schema")
        restored_names = {}
        repaired = False
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
                        if not old.get("sha256"):
                            raise ValueError("cache missing; archive refresh job must populate it")
                        old = refresh_one(item, old, root, args)
                        manifest["entries"][item["id"]] = old
                        atomic_json(manifest_path, manifest)
                        repaired = True
                        if old.get("status") not in ("fresh", "verified") or not local_valid(root, old):
                            raise ValueError("archive refresh did not produce a verified object")
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
        elif repaired:
            rclone(args, "push", root)
    if args.command != "import-existing":
        report["ok"] = all(entry.get("status") in ("fresh", "verified", "restored")
                           for entry in report["entries"]) if args.command != "discover" else True
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
