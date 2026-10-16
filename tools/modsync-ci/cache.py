#!/usr/bin/env python3
"""Archive cache shared by installer adapters; Python 3.10+, standard library only."""

import argparse
import copy
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
LOOSE = "file"
LOOSE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
HTML_REJECTED = "not a supported archive (HTML and unknown payloads rejected)"
TOOL_PROVIDERS = ("mega", "google-drive")
PINS_PATH = Path(__file__).with_name("seed_pins.json")
PIN_PROVIDERS = ("nexus", "gamefront")
PIN_PROVENANCE = "operator-local-download"
PIN_EXTENSIONS = ("zip", "7z", "rar", LOOSE)
SHA256 = re.compile(r"[a-f0-9]{64}")
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
    if host == "dropbox.com" or host.endswith(".dropbox.com"):
        return "dropbox", False
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
    if kind == "dropbox":
        return bool(re.match(r"^/(?:s/[^/]+|scl/fi/[^/]+)/[^/]+", parsed.path))
    return False


def dropbox_direct(url):
    parsed = urlsplit(url)
    query = [(name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True)
             if name.lower() not in ("dl", "raw")]
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(query + [("dl", "1")]), ""))


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


def validate(path, timeout, loose=False):
    with path.open("rb") as stream:
        magic = stream.read(512)
    if loose:
        if path.stat().st_size < MIN_TOOL_ARCHIVE_BYTES:
            raise ValueError("loose file below minimum size (likely an error page)")
        if magic.lstrip(b"\xef\xbb\xbf \t\r\n\x0b\x0c").startswith(b"<"):
            raise ValueError(HTML_REJECTED)
        return LOOSE
    if magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        subprocess.run([sys.executable, "-c",
                        "import sys,zipfile; archive=zipfile.ZipFile(sys.argv[1]); "
                        "sys.exit(1 if archive.testzip() else 0)", str(path.resolve())],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout, check=True)
        return "zip"
    kind = "7z" if magic.startswith(b"7z\xbc\xaf\x27\x1c") else "rar" if magic.startswith(b"Rar!\x1a\x07") else None
    if not kind:
        raise ValueError(HTML_REJECTED)
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


TOOL_FILE_ID = {"google-drive": re.compile(r"^/file/d/([A-Za-z0-9_-]+)"),
                "mega": re.compile(r"^/file/([A-Za-z0-9_-]+)")}
TOOL_KEY = re.compile(r"^(google-drive|mega):([A-Za-z0-9_-]+)$")


def tool_key(url):
    kind, _ = provider(url)
    pattern = TOOL_FILE_ID.get(kind)
    match = pattern.match(urlsplit(url).path) if pattern else None
    return kind + ":" + match[1] if match else None


def tool_selection_key(key):
    key = str(key).strip()
    if TOOL_KEY.match(key):
        return key
    normalized = tool_key(key) if key.startswith(("https://", "http://")) else None
    if not normalized:
        raise ValueError("tool selection key must be google-drive:<file id>, mega:<file id> or a file URL")
    return normalized


ROW_ID = re.compile(r"^[0-9]{1,12}$")


def review_selectors(key, value):
    if isinstance(value, str):
        listed = [value]
    elif isinstance(value, dict):
        if "file" in value and "files" in value:
            raise ValueError("selection for " + str(key) + " must use either file or files, not both")
        if "files" in value:
            listed = value["files"]
            if not isinstance(listed, list) or not listed:
                raise ValueError("selection for " + str(key) + ": files must be a non-empty list")
        else:
            listed = [{"file": value.get("file"), **({"r": value["r"]} if "r" in value else {})}]
    else:
        raise ValueError("selection for " + str(key) + " must be a file name or an object")
    selectors, seen = [], set()
    for item in listed:
        if isinstance(item, str):
            item = {"file": item}
        if not isinstance(item, dict) or set(item) - {"file", "r"}:
            raise ValueError("selection for " + str(key) + ": each file must be a name or {file, r}")
        row = item.get("r")
        if row is not None:
            row = str(row) if isinstance(row, int) and not isinstance(row, bool) else row
            if not isinstance(row, str) or not ROW_ID.match(row):
                raise ValueError("selection for " + str(key) + ": r must be a numeric row id")
        name = item.get("file")
        if isinstance(name, str) and name.casefold() in seen:
            raise ValueError("selection for " + str(key) + " names the same file twice")
        seen.add(name.casefold() if isinstance(name, str) else name)
        selectors.append({"file": name, "r": row})
    return selectors


def review_entry(key, value, tool=False):
    selectors = review_selectors(key, value)
    loose = value.get("loose", False) if isinstance(value, dict) else False
    if not isinstance(loose, bool):
        raise ValueError("selection for " + str(key) + ": loose must be true or false")
    if (loose or tool) and (len(selectors) != 1 or selectors[0]["r"] is not None):
        raise ValueError("loose selection for " + str(key) + " must name exactly one file without a row id")
    for selector in selectors:
        name = selector["file"]
        if loose:
            if not isinstance(name, str) or safe_archive_name(name, LOOSE) != name:
                raise ValueError("loose selection for " + str(key) + " must name a plain non-archive file "
                                 "whose name is already filesystem-safe")
        elif tool:
            raise ValueError("tool selection for " + str(key) + " must set loose to true")
        elif not isinstance(name, str) or not ARCHIVE.search(name):
            raise ValueError("archive selection for " + str(key) + " must name an archive file")
    return {"file": selectors[0]["file"], "loose": loose, "files": selectors}


def load_reviewed(path=None):
    path = Path(path or SELECTIONS_PATH)
    reviewed = {"selections": {}, "tools": {}}
    if not path.exists():
        return reviewed
    document = json.loads(path.read_text(encoding="utf-8"))
    for key, value in (document.get("selections") or {}).items():
        reviewed["selections"][selection_key(key)] = review_entry(key, value)
    for key, value in (document.get("tool_selections") or {}).items():
        reviewed["tools"][tool_selection_key(key)] = review_entry(key, value, tool=True)
    return reviewed


def selection_value(entry):
    only = entry["files"][0]
    return only["file"] if len(entry["files"]) == 1 and only["r"] is None else entry["files"]


def load_selections(path=None):
    return {key: selection_value(entry) for key, entry in load_reviewed(path)["selections"].items()}


def reviewed_selectors(item, path=None):
    if item["provider"] != "deadlystream":
        return None
    match = FILE_ID.search(urlsplit(item["url"]).path)
    entry = load_reviewed(path)["selections"].get(match[1]) if match else None
    return entry["files"] if entry else None


def multi_selectors(item, path=None):
    selectors = reviewed_selectors(item, path)
    return selectors if selectors and (len(selectors) > 1 or selectors[0]["r"]) else None


def reviewed_loose(item, path=None):
    if item["provider"] == "deadlystream":
        match = FILE_ID.search(urlsplit(item["url"]).path)
        key, table = (match[1] if match else None), "selections"
    elif item["provider"] in TOOL_PROVIDERS:
        key, table = tool_key(item["url"]), "tools"
    else:
        return None
    entry = load_reviewed(path)[table].get(key) if key else None
    return entry["file"] if entry and entry["loose"] else None


def entry_loose(item, entry, path=None):
    return entry.get("extension") == LOOSE and reviewed_loose(item, path) is not None


def describe_rows(rows):
    shown = ["%s (r=%s)" % (json.dumps(row["name"]) if row["name"] else "unnamed", row["r"]) for row in rows[:12]]
    return "; ".join(shown) + ("; ..." if len(rows) > 12 else "")


def choose_downloads(rows, file_id, selections):
    reviewed = selections.get(file_id)
    if reviewed:
        selectors = [{"file": reviewed, "r": None}] if isinstance(reviewed, str) else reviewed
        chosen = []
        for selector in selectors:
            matches = [row for row in rows if row["name"] == selector["file"]
                       and (selector["r"] is None or row["r"] == selector["r"])]
            if len(matches) != 1:
                raise ValueError("reviewed archive selection for file %s matches %d of the listed files; "
                                 "re-review archive_selections.json; candidates: %s"
                                 % (file_id, len(matches), describe_rows(rows)))
            chosen.append(matches[0])
        return chosen
    if len(rows) == 1:
        return rows
    archives = [row for row in rows if row["name"] and ARCHIVE.search(row["name"])]
    if len(archives) == 1 and all(row["name"] for row in rows):
        return archives
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
        return choose_downloads(rows, file_id, selections)
    downloads = {url for url in candidates if query_of(url).get("do") == ["download"]}
    pool = downloads or candidates
    if len(pool) != 1:
        raise ValueError("landing page has no unique archive endpoint; attachment selection/provider adapter required")
    return [{"name": None, "url": next(iter(pool)), "r": None}]


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


def page_rows(response, item, selections=None):
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
    if re.search(r"cf-turnstile|g-recaptcha(?!-response)|h-captcha|/cdn-cgi/challenge-platform|<title>\s*Just a moment", source, re.I):
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
    return [{"name": None, "url": candidates.pop(), "r": None}]


def page_endpoint(response, item, selections=None):
    rows = page_rows(response, item, selections)
    if len(rows) != 1:
        raise ValueError("reviewed selection lists several files; fetch them with page_rows")
    return rows[0]["url"]


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
    if item["provider"] == "dropbox":
        return dropbox_direct(item["url"])
    if item["provider"] in TOOL_PROVIDERS:
        tool_base(item["provider"])
        return item["url"]
    if item["direct"]:
        return item["url"]
    with request(item["url"], args) as response:
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("landing URL has no explicit archive endpoint")
        rows = page_rows(response, item)
        return rows if len(rows) > 1 or (rows[0]["name"] and multi_selectors(item)) else rows[0]["url"]


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
        reviewed = reviewed_loose(item)
        kind = validate(path, args.integrity_timeout, loose=bool(reviewed))
        if reviewed:
            if safe_archive_name(path.name, LOOSE) != reviewed:
                raise ValueError("downloaded file name does not match the reviewed loose selection")
            name = reviewed
        else:
            name = safe_archive_name(path.name, kind) or archive_name(item, kind, item["url"])
        sha = digest(path)
        os.replace(path, objects / sha)
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


def entry_objects(entry):
    if not isinstance(entry, dict):
        return []
    if "objects" in entry:
        objects = entry["objects"]
        return objects if isinstance(objects, list) and objects and all(isinstance(each, dict) for each in objects) else []
    return [entry] if entry.get("sha256") else []


def object_valid(root, entry):
    try:
        path = object_path(root, entry)
        return path.is_file() and path.stat().st_size == entry["size"] and digest(path) == entry["sha256"]
    except (ValueError, KeyError, OSError):
        return False


def local_valid(root, entry):
    objects = entry_objects(entry)
    return bool(objects) and all(object_valid(root, each) for each in objects)


def pin_entry(identity, value):
    if not isinstance(value, dict) or value.get("provider") not in PIN_PROVIDERS:
        return None
    if value.get("provenance") != PIN_PROVENANCE or not isinstance(value.get("url"), str):
        return None
    if key_for(value["url"]) != identity:
        return None
    objects = value.get("objects")
    if not isinstance(objects, list) or not objects:
        return None
    for each in objects:
        if (not isinstance(each, dict) or not isinstance(each.get("sha256"), str)
                or not SHA256.fullmatch(each["sha256"]) or not isinstance(each.get("size"), int)
                or isinstance(each["size"], bool) or each["size"] < 0
                or each.get("extension") not in PIN_EXTENSIONS
                or not isinstance(each.get("archive_name"), str) or not each["archive_name"]):
            return None
    return {"url": value["url"], "provider": value["provider"], "provenance": value["provenance"],
            "objects": [{key: each[key] for key in ("archive_name", "sha256", "size", "extension")}
                        for each in objects]}


def load_pins(path=None):
    path = Path(path or PINS_PATH)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("schema") != 1
            or not isinstance(document.get("entries"), dict)):
        raise ValueError("unsupported seed pins schema")
    pins = {}
    for identity, value in document["entries"].items():
        pin = pin_entry(identity, value)
        if pin:
            pins[identity] = pin
    return pins


def is_pinned(item, entry, pins=None):
    if not isinstance(entry, dict) or entry.get("status") != "pinned" or entry.get("evidence") != "operator-seed":
        return False
    if item.get("provider") not in PIN_PROVIDERS:
        return False
    try:
        pin = (load_pins() if pins is None else pins).get(item["id"])
    except (ValueError, OSError):
        return False
    if not pin or pin["provider"] != item["provider"]:
        return False
    objects = entry_objects(entry)
    if not objects:
        return False
    if {each.get("sha256") for each in objects} != {each["sha256"] for each in pin["objects"]}:
        return False
    return objects_match_selection(item, entry)


def bundle(objects):
    fresh = all(each.get("status") == "fresh" for each in objects)
    return {"objects": objects, "size": sum(each["size"] for each in objects),
            "checked_at": min(each["checked_at"] for each in objects),
            "status": "fresh" if fresh else "verified",
            "evidence": "download-200" if fresh else "download-without-upstream-metadata",
            "metadata_unknown": any(each.get("metadata_unknown", True) for each in objects)}


def objects_match_selection(item, entry, path=None):
    selectors = multi_selectors(item, path)
    if not selectors:
        return True
    objects = entry_objects(entry)
    if len(objects) != len(selectors):
        return False
    for selector in selectors:
        matches = [each for each in objects if each.get("listed_name") == selector["file"]
                   and (selector["r"] is None or each.get("row") == selector["r"])]
        if len(matches) != 1:
            return False
    return True


def safe_archive_name(value, extension):
    if not isinstance(value, str):
        return None
    loose = extension == LOOSE
    name = unicodedata.normalize("NFC", value).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character if character.isalnum() or character in " ._-()[]+" else "_"
                   for character in name).strip(" .")
    if not name or len(name.encode("utf-8")) > 240:
        return None
    if loose and (ARCHIVE.search(name) or not LOOSE_SUFFIX.search(name)):
        return None
    if not loose and not ARCHIVE.search(name):
        return None
    stem, suffix = name.rsplit(".", 1)
    if not stem.strip(" ._") or (not loose and suffix.lower() != extension):
        return None
    if stem.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(
            prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10))}:
        name = "_" + name
    return name


def disposition_name(disposition, extension):
    if disposition:
        try:
            message = EmailMessage(policy=email_policy)
            message["Content-Disposition"] = disposition
            return safe_archive_name(message.get_filename(), extension)
        except (ValueError, TypeError, IndexError):
            pass
    return None


def archive_name(item, extension, url, disposition=None, reviewed=None):
    if reviewed:
        offered = disposition_name(disposition, LOOSE)
        if offered and offered != reviewed:
            raise ValueError("downloaded file name does not match the reviewed loose selection")
        return reviewed
    name = disposition_name(disposition, extension)
    if name:
        return name
    name = safe_archive_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1]), extension)
    if name:
        return name
    basename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    basename = re.sub(r"[^a-zA-Z0-9_.-]+", "_", unicodedata.normalize("NFC", basename)).strip(" ._")
    basename = basename[:80] or "archive"
    return basename + "-" + item["id"] + "." + extension


def refresh_one(item, old, root, args, endpoint=None, depth=0):
    result = fetch_item(item, old, root, args, endpoint, depth)
    if not objects_match_selection(item, result):
        raise ValueError("fetched files do not match the reviewed multi-file selection; "
                         "re-review archive_selections.json")
    return result


def fetch_many(item, old, root, args, rows, depth):
    previous = {each.get("endpoint_id"): each for each in entry_objects(old)}
    budget = args.max_bytes
    objects = []
    for row in rows:
        limited = copy.copy(args)
        limited.max_bytes = budget
        fetched = fetch_item(item, previous.get(endpoint_id(row["url"]), {}), root, limited, row["url"], depth)
        budget -= fetched["size"]
        objects.append({**fetched, "listed_name": row["name"], "row": row["r"]})
    return bundle(objects)


CONTENT_RANGE = re.compile(r"^bytes 0-([0-9]+)/([0-9]+)$")


def announced_size(headers):
    length = headers.get("Content-Length")
    if length is not None:
        return int(length)
    match = CONTENT_RANGE.match((headers.get("Content-Range") or "").strip())
    return int(match[2]) if match and int(match[1]) + 1 == int(match[2]) else None


def fetch_item(item, old, root, args, endpoint=None, depth=0):
    old = {} if "objects" in old else old
    endpoint = endpoint or resolve(item, args)
    if isinstance(endpoint, list):
        return fetch_many(item, old, root, args, endpoint, depth + 1)
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
            loose = entry_loose(item, old)
            validate(object_path(root, old), args.integrity_timeout, loose=loose)
            name = archive_name(item, old["extension"], response.geturl(),
                                response.headers.get("Content-Disposition"),
                                reviewed_loose(item) if loose else None)
            return {**old, "archive_name": old.get("archive_name", name),
                    "checked_at": now, "status": "fresh", "evidence": "conditional-304"}
        if response.code != 200:
            raise ValueError("expected complete HTTP 200 archive")
        if "html" in response.headers.get("Content-Type", "").lower():
            if item["provider"] != "deadlystream" or depth >= 2:
                raise ValueError("archive endpoint returned HTML; provider adapter required")
            rows = page_rows(response, item)
            if len(rows) > 1 or multi_selectors(item):
                response.close()
                return fetch_many(item, old, root, args, rows, depth + 1)
            attachment = rows[0]["url"]
            if endpoint_id(attachment) == endpoint_key:
                raise ValueError("download endpoint did not resolve to an attachment; provider adapter required")
            response.close()
            return fetch_item(item, old, root, args, attachment, depth + 1)
        expected = announced_size(response.headers)
        if expected is not None and expected > args.max_bytes:
            raise ValueError("archive exceeds size limit")
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
            if expected is not None and size != expected:
                raise ValueError("truncated archive")
            reviewed = reviewed_loose(item)
            kind = validate(path, args.integrity_timeout, loose=bool(reviewed))
            name = archive_name(item, kind, response.geturl(),
                                response.headers.get("Content-Disposition"), reviewed)
            sha = digest(path)
            if not (valid and old.get("sha256") == sha):
                os.replace(path, root / "objects" / sha)
            etag = response.headers.get("ETag")
            modified = response.headers.get("Last-Modified")
            version = version_id(response.geturl())
            known = bool(etag or modified)
            return {"sha256": sha, "size": size, "extension": kind,
                    "archive_name": name,
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


def import_object(item, filename, source, loose, root, args, old_object, known, expected_sha256=None):
    expected_size = source.stat().st_size
    if expected_size < 0 or expected_size > args.max_bytes:
        raise ValueError("archive exceeds size limit")
    objects = root / "objects"
    if objects.is_symlink() or (objects.exists() and not objects.is_dir()):
        raise ValueError("unsafe cache object path")
    objects.mkdir(parents=True, exist_ok=True)
    if expected_size + DISK_RESERVE_BYTES > shutil.disk_usage(objects).free:
        raise ValueError("insufficient disk space")
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
        sha = digest_state.hexdigest()
        if expected_sha256 is not None and sha != expected_sha256:
            return "pin-mismatch", None, sha, size
        kind = validate(staged, args.integrity_timeout, loose=loose)
        object_file = objects / sha
        if known:
            if (not isinstance(old_object, dict) or old_object.get("sha256") != sha or old_object.get("size") != size
                    or object_file.is_symlink()):
                return "manifest-conflict", None, sha, size
            if object_file.exists():
                if not object_valid(root, old_object):
                    return "manifest-conflict", None, sha, size
            else:
                os.link(staged, object_file)
            return "already-present", None, sha, size
        try:
            os.link(staged, object_file)
        except FileExistsError:
            if object_file.is_symlink() or not object_file.is_file() or object_file.stat().st_size != size:
                raise ValueError("cache object conflict")
            if digest(object_file) != sha:
                raise ValueError("cache object conflict")
            validate(object_file, args.integrity_timeout, loose=loose)
        pinned = expected_sha256 is not None
        return "imported", {"sha256": sha, "size": size, "extension": kind,
                            "archive_name": filename, "status": "pinned" if pinned else "stale-unknown",
                            "metadata_unknown": True,
                            "evidence": "operator-seed" if pinned else "local-import",
                            "imported_at": time.time()}, sha, size
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)


def expected_for(expected_sha256, filename):
    if isinstance(expected_sha256, dict):
        return expected_sha256.get(filename)
    return expected_sha256


def import_existing(item, resource_entries, archive_root, root, args, expected_sha256=None):
    matches = [(key, value) for key, value in resource_entries.items()
               if isinstance(value, dict)
               and isinstance(value.get("HandlerMetadata"), dict)
               and value["HandlerMetadata"].get("PrimaryUrl") == item["url"]]
    if len(matches) != 1:
        return {"id": item["id"], "status": "ambiguous-provider-url" if matches else "unmatched"}
    _, resource_entry = matches[0]
    files = resource_entry.get("Files")
    selectors = multi_selectors(item)
    if not isinstance(files, dict):
        return {"id": item["id"], "status": "ambiguous-variant"}
    if selectors:
        filenames = [selector["file"] for selector in selectors]
        if any(selector["r"] for selector in selectors) or sorted(files) != sorted(filenames):
            return {"id": item["id"], "status": "ambiguous-variant",
                    "reason": "local files cannot prove the reviewed multi-file selection"}
    elif len(files) == 1:
        filenames = list(files)
    else:
        return {"id": item["id"], "status": "ambiguous-variant"}
    planned = []
    try:
        for filename in filenames:
            source = existing_archive_path(archive_root, filename)
            loose = reviewed_loose(item) == filename
            extension = LOOSE if loose else source.suffix.lower().lstrip(".")
            if safe_archive_name(filename, extension) != filename:
                raise ValueError("unsafe archive name")
            planned.append((filename, source, loose))
    except (OSError, ValueError) as error:
        return {"id": item["id"], "status": "unresolved", "reason": safe_reason(error)}
    entry = {"id": item["id"], "status": "planned"}
    entry.update({"archive_names": filenames} if selectors else {"archive_name": filenames[0]})
    pinned = expected_sha256 is not None
    if pinned:
        try:
            for filename, source, loose in planned:
                expected = expected_for(expected_sha256, filename)
                if expected is None or digest(source) != expected:
                    entry["status"] = "pin-mismatch"
                    return entry
        except OSError as error:
            return {"id": item["id"], "status": "unresolved", "reason": safe_reason(error)}
    manifest_path = root / "manifest.json"
    manifest = args.import_manifest
    known = item["id"] in manifest
    old = manifest.get(item["id"])
    if known and not args.apply:
        entry["status"] = "existing-association"
        return entry
    if not args.apply:
        return entry
    try:
        outcomes = []
        for filename, source, loose in planned:
            candidates = entry_objects(old) if known else []
            if selectors:
                old_object = next((each for each in candidates if each.get("listed_name") == filename), None)
            else:
                old_object = candidates[0] if len(candidates) == 1 else None
            outcomes.append(import_object(item, filename, source, loose, root, args, old_object, known,
                                          expected_for(expected_sha256, filename) if pinned else None)
                            + (filename,))
        mismatches = [outcome for outcome in outcomes if outcome[0] == "pin-mismatch"]
        conflicts = [outcome for outcome in outcomes if outcome[0] == "manifest-conflict"]
        if mismatches:
            entry.update(status="pin-mismatch", sha256=mismatches[0][2], size=mismatches[0][3])
        elif conflicts:
            entry.update(status="manifest-conflict", sha256=conflicts[0][2], size=conflicts[0][3])
        elif known:
            entry.update(status="already-present", sha256=outcomes[0][2], size=outcomes[0][3])
        else:
            imported = [{**outcome[1], "listed_name": outcome[4], "row": None} if selectors else outcome[1]
                        for outcome in outcomes]
            document = imported[0] if not selectors else {
                "objects": imported, "size": sum(each["size"] for each in imported),
                "status": "pinned" if pinned else "stale-unknown", "metadata_unknown": True,
                "evidence": "operator-seed" if pinned else "local-import",
                "imported_at": time.time()}
            manifest[item["id"]] = document
            try:
                args.import_manifest_document["entries"] = manifest
                atomic_json(manifest_path, args.import_manifest_document)
            except OSError:
                manifest.pop(item["id"], None)
                raise
            entry.update(status=document["status"], sha256=outcomes[0][2], size=sum(each[3] for each in outcomes))
        if selectors:
            entry.pop("sha256", None)
            entry["objects"] = [{"archive_name": outcome[4], "sha256": outcome[2], "size": outcome[3]}
                                for outcome in outcomes]
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        entry.update(status="unresolved", reason=safe_reason(error))
    return entry


def restore_object(item, old, root, args, restored_names, taken, created):
    source = object_path(root, old)
    if old.get("extension") not in ("zip", "7z", "rar", LOOSE):
        raise ValueError("invalid manifest archive extension")
    reviewed = reviewed_loose(item) if old["extension"] == LOOSE else None
    if old["extension"] == LOOSE and not reviewed:
        raise ValueError("loose file is not a reviewed selection")
    validate(source, args.integrity_timeout, loose=bool(reviewed))
    args.destination.mkdir(parents=True, exist_ok=True)
    if "archive_name" in old:
        filename = safe_archive_name(old["archive_name"], old["extension"])
        if filename is None:
            raise ValueError("invalid manifest archive name")
    else:
        filename = archive_name(item, old["extension"], old.get("endpoint") or item["url"],
                                reviewed=reviewed)
    if reviewed and filename != reviewed:
        raise ValueError("loose file name does not match the reviewed selection")
    name_key = filename.casefold()
    if name_key in taken or (name_key in restored_names and restored_names[name_key] != item["id"]):
        raise ValueError("archive filename collision")
    taken.add(name_key)
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
            created.append(args.destination / filename)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return filename


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
    parser.add_argument("--expected-sha256", action="append", default=[], metavar="FILENAME=SHA256",
                        help="import-existing: pin this archive file to an exact SHA256 (repeatable)")
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
    expected_hashes = {}
    for pair in args.expected_sha256:
        name, _, sha = pair.rpartition("=")
        if args.command != "import-existing" or not name or not SHA256.fullmatch(sha):
            parser.error("--expected-sha256 must be FILENAME=SHA256 and is only valid with import-existing")
        expected_hashes[name] = sha
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
        report["entries"] = [import_existing(item, resource_entries, archive_root, root, args,
                                                    expected_hashes or None)
                              for item in selected]
        report["candidate_count"] = len(matched_items)
        report["next_offset"] = start + len(selected)
        report["unmatched_urls"] = len(items) - len(matched_items)
        report["truncated"] = report["next_offset"] < len(matched_items)
        report["ok"] = all(entry["status"] in (
            "planned", "existing-association", "already-present", "stale-unknown", "pinned")
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
        try:
            pins = load_pins()
        except (ValueError, OSError):
            pins = {}
        for item in items:
            old = manifest["entries"].get(item["id"], {})
            entry = {"id": item["id"], "url": clean_url(item["url"]), "provider": item["provider"]}
            try:
                valid = local_valid(root, old)
                age = time.time() - old.get("checked_at", 0)
                upstream_checked = old.get("status") == "fresh" or (
                    old.get("status") == "verified" and
                    old.get("evidence") == "download-without-upstream-metadata")
                pinned = valid and is_pinned(item, old, pins)
                fresh = pinned or (valid and upstream_checked and 0 <= age < args.max_age_days * 86400
                                   and objects_match_selection(item, old))
                if args.command == "refresh":
                    cached = old if pinned or (fresh and not args.force) else refresh_one(item, old, root, args)
                    manifest["entries"][item["id"]] = cached
                    entry.update(cached)
                    atomic_json(manifest_path, manifest)
                else:
                    if not fresh:
                        if not entry_objects(old):
                            raise ValueError("cache missing; archive refresh job must populate it")
                        old = refresh_one(item, old, root, args)
                        manifest["entries"][item["id"]] = old
                        atomic_json(manifest_path, manifest)
                        repaired = True
                        if old.get("status") not in ("fresh", "verified") or not local_valid(root, old):
                            raise ValueError("archive refresh did not produce a verified object")
                    taken, created, restored = set(), [], []
                    try:
                        for each in entry_objects(old):
                            restored.append({"file": restore_object(item, each, root, args, restored_names,
                                                                    taken, created),
                                             "sha256": each["sha256"]})
                    except Exception:
                        for path in created:
                            path.unlink(missing_ok=True)
                        raise
                    entry.update(status="restored", metadata_unknown=old.get("metadata_unknown", not bool(
                        old.get("etag") or old.get("last_modified"))))
                    if pinned:
                        entry.update(pinned=True, metadata_unknown=True)
                    if "objects" in old:
                        entry["files"] = restored
                    else:
                        entry.update(sha256=restored[0]["sha256"], file=restored[0]["file"])
            except (ValueError, OSError, HTTPException, subprocess.SubprocessError, zipfile.BadZipFile) as error:
                entry.update(status="unresolved", reason=safe_reason(error))
                if args.command == "refresh" and old and not is_pinned(item, old, pins):
                    manifest["entries"][item["id"]] = {**old, "status": "stale-unknown"}
                    atomic_json(manifest_path, manifest)
            report["entries"].append(entry)
        if args.command == "refresh":
            rclone(args, "push", root)
        elif repaired:
            rclone(args, "push", root)
    if args.command != "import-existing":
        report["ok"] = all(entry.get("status") in ("fresh", "verified", "restored", "pinned")
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
