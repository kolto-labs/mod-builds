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
UNCACHED = "uncached-by-policy"
UNCACHED_SKIPPED = "uncached-skipped"
OVER_LIMIT = "over-cache-limit"
PAGE_UNAVAILABLE = ("landing page returned no download links after retries; "
                    "transient page variance or removed file, not a selection mismatch")
PAGE_RETRY_SECONDS = 4
PAGE_RETRY_CAP_SECONDS = 20
MAX_LINK_BYTES = 20 * 1024 ** 3
NEXUS_REASON = "nexus requires NEXUS_API_KEY (no headless adapter; CAPTCHA is never bypassed); blocked-pending-user-action"
ARTSTORE_TOKEN_ENV = ("ARTSTORE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
SECRET_ENV = ("CACHE_ENCRYPTION_KEY", "DIAGNOSTICS_ENCRYPTION_KEY", "GITHUB_TOKEN", "GH_TOKEN", "ARTSTORE_TOKEN",
              "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
S3_ENV = ("MOD_CACHE_S3_", "AWS_", "RCLONE_CONFIG_")
ARTSTORE_REFRESH_DAYS = 21
ARTSTORE_STATE = {}
STAGE_MAX_BYTES = 3 * 1024 ** 3
STAGE_TOTAL_BYTES = 9 * 1024 ** 3


def scrubbed_env(keep=()):
    """os.environ minus every secret; names or prefixes in keep (rclone's S3_ENV) stay. Child processes get this."""
    keep = tuple(keep)
    return {name: value for name, value in os.environ.items()
            if name.startswith(keep) or not (name in SECRET_ENV or name.startswith(S3_ENV))}


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
    if kind == "http":
        return key_for(url) in load_direct_links()
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


def guide_source(guide):
    source = Path(guide).read_text(encoding="utf-8")
    source = re.sub(r"```.*?```|~~~.*?~~~", "", source, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", source)


def link_urls(source):
    for match in LINK.finditer(source):
        url = html.unescape(next(value for value in match.groups() if value)).strip("<>")
        if url.startswith(("https://", "http://")):
            yield url


def discover(guides):
    found = {}
    for guide in guides:
        for url in link_urls(guide_source(guide)):
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


def referencing_steps(item):
    steps = []
    for guide in item["guides"]:
        section = heading = None
        blocks = [(None, None, [])]
        for line in guide_source(guide).splitlines(keepends=True):
            if line.startswith("# "):
                section = heading = None
            elif line.startswith("## "):
                section, heading = line[3:].strip(), None
            elif line.startswith("### "):
                heading = line[4:].strip()
            else:
                blocks[-1][2].append(line)
                continue
            blocks.append((section, heading, []))
        for section, heading, lines in blocks:
            if item["url"] in set(link_urls("".join(lines))):
                step = {"guide": str(guide), "section": section, "heading": heading}
                if step not in steps:
                    steps.append(step)
    return steps


STEP_SCOPE = "::"


def parse_allowance(value):
    text = str(value)
    if STEP_SCOPE not in text:
        section = text.strip()
        if not section:
            raise ValueError("--allow-uncached-by-policy needs a non-empty section name")
        return section, None
    section, _, heading = text.partition(STEP_SCOPE)
    section, heading = section.strip(), heading.strip()
    if not section or not heading or STEP_SCOPE in heading:
        raise ValueError("--allow-uncached-by-policy step form must be '<## section>::<### heading>': " + text)
    return section, heading


def skippable_steps(item, allowances):
    sections, steps_allowed = set(), set()
    for value in allowances:
        section, heading = parse_allowance(value)
        if heading is None:
            sections.add(section)
        else:
            steps_allowed.add((section, heading))
    steps = referencing_steps(item)
    outside = [step for step in steps if step["heading"] is None or not (
        step["section"] in sections or (step["section"], step["heading"]) in steps_allowed)]
    return steps, outside


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
        try:
            subprocess.run([sys.executable, "-c",
                            "import sys,zipfile; archive=zipfile.ZipFile(sys.argv[1]); "
                            "sys.exit(1 if archive.testzip() else 0)", str(path.resolve())],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=True, env=scrubbed_env())
        except subprocess.CalledProcessError:
            if not integrity_tools(path, "zip"):
                raise
            run_integrity_tools(path, "zip", timeout)
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
                                    stderr=subprocess.DEVNULL, timeout=timeout, env=scrubbed_env())
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


NEXUS_KEY = re.compile(r"^nexus:(?:([A-Za-z0-9_-]+)/)?([0-9]+)$")
NEXUS_MOD = re.compile(r"^/([^/]+)/mods/([0-9]+)(?:/|$)")


def uncached_key(key):
    text = str(key).strip()
    if text.startswith("nexus:"):
        match = NEXUS_KEY.match(text)
        if not match:
            raise ValueError("uncached nexus key must look like nexus:<mod id> or nexus:<game>/<mod id>")
        return "nexus:" + (match[1].lower() + "/" if match[1] else "") + match[2]
    return selection_key(text)


def load_uncached(path=None):
    path = Path(path or SELECTIONS_PATH)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    declared = document.get("uncached", {})
    if not isinstance(declared, dict):
        raise ValueError("uncached must be an object keyed by DeadlyStream file id or nexus:<mod id>")
    uncached = {}
    for key, value in declared.items():
        reason = value.get("reason") if isinstance(value, dict) else None
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("uncached entry for " + str(key) + " must carry a non-empty reason")
        uncached[uncached_key(key)] = reason.strip()
    return uncached


def direct_link_key(key):
    key = str(key).strip()
    try:
        checked_url(key)
    except ValueError:
        raise ValueError("direct link key must be an http(s) guide link URL") from None
    return key_for(key)


def review_direct_link(key, value):
    if not isinstance(value, dict) or set(value) != {"url", "file", "sha256", "size", "reason"}:
        raise ValueError("direct link for " + str(key) + " must be exactly {url, file, sha256, size, reason}")
    url, name = value["url"], value["file"]
    if not isinstance(url, str):
        raise ValueError("direct link for " + str(key) + ": url must be a string")
    parsed = urlsplit(checked_url(url))
    if (parsed.scheme != "https" or parsed.query or parsed.fragment or not ARCHIVE.search(parsed.path)
            or (parsed.hostname or "").lower() != (urlsplit(str(key).strip()).hostname or "").lower()):
        raise ValueError("direct link for " + str(key) + " must be an https archive URL on the guide link's host")
    if not isinstance(name, str) or not ARCHIVE.search(name) or safe_archive_name(
            name, name.rsplit(".", 1)[-1].lower()) != name:
        raise ValueError("direct link for " + str(key) + " must name a filesystem-safe archive file")
    if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
        raise ValueError("direct link for " + str(key) + " needs a lowercase SHA-256")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool) or value["size"] <= 0:
        raise ValueError("direct link for " + str(key) + " needs a positive size")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise ValueError("direct link for " + str(key) + " must carry a non-empty reason")
    return {"url": url, "file": name, "sha256": value["sha256"], "size": value["size"],
            "reason": value["reason"].strip()}


def load_direct_links(path=None):
    path = Path(path or SELECTIONS_PATH)
    if not path.exists():
        return {}
    declared = json.loads(path.read_text(encoding="utf-8")).get("direct_links", {})
    if not isinstance(declared, dict):
        raise ValueError("direct_links must be an object keyed by guide link URL")
    return {direct_link_key(key): review_direct_link(key, value) for key, value in declared.items()}


def reviewed_direct(item, path=None):
    if item["provider"] != "http":
        return None
    return load_direct_links(path).get(item["id"])


def uncached_reason(item, path=None):
    if item["provider"] == "deadlystream":
        match = FILE_ID.search(urlsplit(item["url"]).path)
        return load_uncached(path).get(match[1]) if match else None
    if item["provider"] == "nexus":
        match = NEXUS_MOD.match(urlsplit(item["url"]).path)
        if not match:
            return None
        declared = load_uncached(path)
        return declared.get("nexus:" + match[1].lower() + "/" + match[2]) or declared.get("nexus:" + match[2])
    return None


def uncached_entry(item, reason):
    return {"id": item["id"], "url": clean_url(item["url"]), "provider": item["provider"],
            "status": UNCACHED, "reason": "intentionally not cached by policy: " + reason}


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


class LandingVariance(ValueError):
    """A DeadlyStream landing page without a usable download list; a re-fetch can differ."""


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
    if not pool:
        raise LandingVariance(PAGE_UNAVAILABLE)
    if len(pool) != 1:
        raise LandingVariance("landing page has no unique archive endpoint; "
                              "attachment selection/provider adapter required")
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
    reviewed = reviewed_direct(item)
    if reviewed:
        return reviewed["url"]
    if item["direct"]:
        return item["url"]
    rows = landing_rows(item, args)
    return rows if len(rows) > 1 or (rows[0]["name"] and multi_selectors(item)) else rows[0]["url"]


def landing_rows(item, args):
    """Download rows of the landing page; a DeadlyStream page without any is fetched again, with backoff.

    A failed fetch keeps request's own reason (HTTP status or network request failed) and a reviewed
    selection that matches no listed file keeps choose_downloads's: only a page that lists nothing usable
    is re-fetched, and it ends as PAGE_UNAVAILABLE rather than as a selection or adapter problem.
    """
    attempts = args.retries + 1 if item["provider"] == "deadlystream" else 1
    for attempt in range(attempts):
        with request(item["url"], args) as response:
            if "html" not in response.headers.get("Content-Type", "").lower():
                raise ValueError("landing URL has no explicit archive endpoint")
            try:
                return page_rows(response, item)
            except LandingVariance:
                if attempt + 1 == attempts:
                    raise
        time.sleep(min(PAGE_RETRY_SECONDS * 2 ** attempt, PAGE_RETRY_CAP_SECONDS))


def fetch_with_tool(item, root, args):
    objects = root / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=objects, prefix=".tool-"))
    try:
        command = tool_command(item["provider"], workdir, item["url"])
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=args.download_timeout,
                                    env=scrubbed_env())
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
                        for each in objects],
            **({"p2p": value["p2p"]} if isinstance(value.get("p2p"), dict) else {})}


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
            "evidence": ("conditional-304" if all(each.get("evidence") == "conditional-304" for each in objects)
                         else "download-200" if fresh else "download-without-upstream-metadata"),
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
    policy = uncached_reason(item)
    if policy:
        raise ValueError("intentionally not cached by policy: " + policy)
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
    previous_bundle = old
    old = {} if "objects" in old else old
    endpoint = endpoint or resolve(item, args)
    if isinstance(endpoint, list):
        return fetch_many(item, previous_bundle, root, args, endpoint, depth + 1)
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
                return fetch_many(item, previous_bundle, root, args, rows, depth + 1)
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
            pinned_direct = reviewed_direct(item)
            if pinned_direct:
                if sha != pinned_direct["sha256"] or size != pinned_direct["size"]:
                    raise ValueError("downloaded file does not match the reviewed direct link; "
                                     "re-review archive_selections.json")
                name = pinned_direct["file"]
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
    policy = uncached_reason(item)
    if policy:
        return uncached_entry(item, policy)
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


def restore_filename(item, old):
    reviewed = reviewed_loose(item) if old.get("extension") == LOOSE else None
    if "archive_name" in old:
        filename = safe_archive_name(old["archive_name"], old["extension"])
        if filename is None:
            raise ValueError("invalid manifest archive name")
    else:
        filename = archive_name(item, old["extension"], old.get("endpoint") or item["url"],
                                reviewed=reviewed)
    return filename, reviewed


def mod_keys(item):
    return {(step["guide"], step["section"], step["heading"])
            for step in referencing_steps(item) if step["heading"] is not None}


def link_basename_names(url):
    basename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return {name.casefold() for name in (safe_archive_name(basename, kind) for kind in ("zip", "7z", "rar"))
            if name}


def sibling_name_losers(items, manifest_entries, pins):
    members = {}
    for position, item in enumerate(items):
        if uncached_reason(item):
            continue
        old = manifest_entries.get(item["id"])
        natural = link_basename_names(item["url"])
        pinned = is_pinned(item, old, pins)
        names = set()
        for each in entry_objects(old):
            try:
                filename, reviewed = restore_filename(item, each)
            except (KeyError, TypeError, ValueError):
                continue
            if not reviewed:
                names.add(filename.casefold())
        for key in mod_keys(item):
            for name in names:
                members.setdefault((key, name), {})[item["id"]] = (not pinned, name not in natural, position)
    losers = {}
    for (_, name), ranks in members.items():
        if len(ranks) > 1:
            winner = min(ranks, key=ranks.get)
            for item_id in ranks:
                if item_id != winner:
                    losers.setdefault(item_id, set()).add(name)
    return losers


def disambiguated_name(filename, item):
    stem, suffix = filename.rsplit(".", 1)
    name = safe_archive_name(stem + "-" + item["id"][:8] + "." + suffix, suffix.lower())
    if name is None:
        raise ValueError("archive filename collision")
    return name


def restore_object(item, old, root, args, restored_names, taken, created, renamed=None):
    source = object_path(root, old)
    if old.get("extension") not in ("zip", "7z", "rar", LOOSE):
        raise ValueError("invalid manifest archive extension")
    reviewed = reviewed_loose(item) if old["extension"] == LOOSE else None
    if old["extension"] == LOOSE and not reviewed:
        raise ValueError("loose file is not a reviewed selection")
    validate(source, args.integrity_timeout, loose=bool(reviewed))
    args.destination.mkdir(parents=True, exist_ok=True)
    filename, reviewed = restore_filename(item, old)
    if reviewed and filename != reviewed:
        raise ValueError("loose file name does not match the reviewed selection")
    if not reviewed and filename.casefold() in (renamed or ()):
        filename = disambiguated_name(filename, item)
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


def warn(message):
    print("::warning::" + message, file=sys.stderr)


def notice(message):
    print("::notice::" + message, file=sys.stderr)


def remote_copy(args, source, destination):
    """Copy one remote file. False when it is absent; an unreachable remote is warned about once, then skipped."""
    if not args.remote:
        return False
    command = ["rclone", "copyto", source, str(destination), "--retries", "1",
               "--contimeout", "20s", "--timeout", "60s"]
    try:
        subprocess.run(command, check=True, timeout=args.backend_timeout, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=scrubbed_env(S3_ENV))
        return Path(destination).is_file()
    except subprocess.CalledProcessError as error:
        if error.returncode in (3, 4):
            return False
    except (subprocess.SubprocessError, OSError):
        pass
    warn("archive remote unreachable; continuing without it")
    args.remote = None
    return False


def pull_manifest(args, root, manifest):
    """Merge newer entries from the optional remote manifest (it holds the operator-seeded pins)."""
    temporary = root / ".remote-manifest.json"
    try:
        if not remote_copy(args, args.remote + "/manifest.json", temporary):
            return
        remote = json.loads(temporary.read_text(encoding="utf-8"))
        entries = remote["entries"] if remote.get("schema") == 1 else {}
        for key, entry in entries.items():
            have = manifest["entries"].get(key)
            if isinstance(entry, dict) and (not isinstance(have, dict)
                                            or entry.get("checked_at", 0) > have.get("checked_at", 0)):
                manifest["entries"][key] = entry
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        warn("remote manifest unreadable; ignoring it")
    finally:
        temporary.unlink(missing_ok=True)


def try_p2p(item, dest):
    """P2P hook: fill dest with the archive whose SHA-256 is item["sha256"] from the swarm.

    False when there is no p2p_fetch helper or the item carries no magnet (item["p2p"]).
    The caller re-verifies whatever lands in dest.
    """
    helper = Path(__file__).with_name("p2p_fetch.py")
    if not helper.is_file() or not isinstance(item.get("p2p"), dict):
        return False
    live, safe = dict(os.environ), scrubbed_env()
    try:
        os.environ.clear()
        os.environ.update(safe)
        spec = importlib.util.spec_from_file_location("p2p_fetch", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return bool(module.fetch({"sha256": item.get("sha256"), "p2p": item["p2p"]}, Path(dest)))
    except Exception:
        warn("p2p helper failed; continuing without it")
        return False
    finally:
        os.environ.clear()
        os.environ.update(live)


def artstore_token():
    return any(os.environ.get(name) for name in ARTSTORE_TOKEN_ENV)


def artstore_module():
    """The sibling artstore.py, loaded once; None (with one message) when it is absent or broken."""
    if "module" not in ARTSTORE_STATE:
        helper = Path(__file__).with_name("artstore.py")
        module = None
        if not helper.is_file():
            notice("artifact store unavailable; continuing without it")
        else:
            try:
                spec = importlib.util.spec_from_file_location("artstore", helper)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not callable(getattr(module, "fetch", None)):
                    raise AttributeError("fetch")
            except Exception:
                warn("artifact store helper failed; continuing without it")
                module = None
        ARTSTORE_STATE["module"] = module
    return ARTSTORE_STATE["module"]


def artstore_session():
    """{client, index} for this process: the store's index is loaded once and shared by every fetch.

    None (remembered, with one message) when the store has no token or repository, or its index cannot be read.
    """
    if "session" not in ARTSTORE_STATE:
        session = None
        module = artstore_module()
        if module is not None:
            try:
                client = module.client_mod.from_environment()
                if client is None:
                    notice("artifact store unavailable (no token or repository); continuing without it")
                else:
                    session = {"client": client, "index": module.load_index(client)}
            except Exception:
                warn("artifact store index unavailable; continuing without it")
        ARTSTORE_STATE["session"] = session
    return ARTSTORE_STATE["session"]


def is_bik(stored):
    return any(isinstance(name, str) and name.lower().endswith(".bik")
               for name in (stored.get("archive_name"), stored.get("listed_name")))


def try_artstore(stored, dest, args):
    """Artifact-store hook: fill dest with the blob whose SHA-256 is stored["sha256"]; False on any miss.

    Skipped unless --artstore is on, and never used for .bik. The caller re-verifies whatever lands in dest.
    """
    if not getattr(args, "artstore", False) or is_bik(stored):
        return False
    module = artstore_module()
    if module is None:
        args.artstore = False
        return False
    session = artstore_session()
    if session is None:
        args.artstore = False
        return False
    try:
        if module.fetch(stored["sha256"], Path(dest), client=session["client"], index=session["index"]):
            return True
    except Exception:
        warn("artifact store failed; continuing without it")
        args.artstore = False
    Path(dest).unlink(missing_ok=True)
    return False


def stage_needed(shas):
    """The SHA-256s in shas the artifact store lacks or will soon expire; None when the store is unavailable."""
    session = artstore_session()
    if session is None:
        return None
    module, index = artstore_module(), session["index"]
    wanted = set(module.core.blobs_missing(index, sorted(shas)))
    wanted.update(module.refresh_plan(index, time.time(), ARTSTORE_REFRESH_DAYS))
    return wanted & set(shas)


def stage_candidates(root, manifest, stage_dir=None):
    """[{path, sha256}] for each live non-.bik cached object the artifact store lacks or will soon expire.

    An object is looked up in root/objects, then in stage_dir (the stage-only copies refresh keeps out of the cache).
    """
    if artstore_session() is None:
        return []
    try:
        found = {}
        for entry in manifest["entries"].values():
            for stored in entry_objects(entry):
                if is_bik(stored):
                    continue
                for path in (object_path(root, stored), *([stage_dir / stored["sha256"]] if stage_dir else [])):
                    if path.is_file() and path.stat().st_size == stored["size"]:
                        found[stored["sha256"]] = path
                        break
        wanted = stage_needed(found) or set()
        return [{"path": str(found[sha]), "sha256": sha} for sha in sorted(wanted)]
    except Exception:
        warn("artifact store index unreadable; nothing staged")
        return []


def stage_pending(old, keep, args):
    """The objects of old outside keep that the artifact store still needs; None when old cannot be staged.

    None when any object outside keep is .bik, malformed or larger than --stage-max-bytes, or the store is unavailable.
    """
    outside = [each for each in entry_objects(old) if each.get("sha256") not in keep]
    for each in outside:
        sha, size = each.get("sha256"), each.get("size")
        if (is_bik(each) or not isinstance(sha, str) or not SHA256.fullmatch(sha)
                or not isinstance(size, int) or size > args.stage_max_bytes):
            return None
    try:
        wanted = stage_needed({each["sha256"] for each in outside})
    except Exception:
        return None
    return None if wanted is None else [each for each in outside if each["sha256"] in wanted]


def reserve_stage(old, keep, args, state):
    """Reserve stage-only room for the objects of old outside keep: {sha256: size} of the newly reserved ones.

    None when old cannot be staged or the run's --stage-total-bytes would be exceeded; {} when everything it
    needs is already reserved. state is {"hold": set of SHA-256s, "left": bytes}.
    """
    pending = stage_pending(old, keep, args)
    if pending is None:
        return None
    fresh = {each["sha256"]: each["size"] for each in pending if each["sha256"] not in state["hold"]}
    if sum(fresh.values()) > state["left"]:
        return None
    state["hold"].update(fresh)
    state["left"] -= sum(fresh.values())
    return fresh


def release_stage(fresh, state):
    state["hold"].difference_update(fresh)
    state["left"] += sum(fresh.values())


def verified_object(item, stored, root, args):
    path = object_path(root, stored)
    try:
        if object_valid(root, stored):
            validate(path, args.integrity_timeout, loose=entry_loose(item, stored))
            return True
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        pass
    path.unlink(missing_ok=True)
    return False


def recover_object(item, stored, root, args, pins):
    """Fetch one object the manifest knows by hash: the artifact store, the optional remote, then P2P.

    Returns the tier that supplied it ("artstore", "remote" or "p2p"), or "" when none did.
    """
    path = object_path(root, stored)
    if stored["size"] <= args.max_bytes and try_artstore(stored, path, args) and verified_object(
            item, stored, root, args):
        return "artstore"
    if args.remote and stored["size"] <= args.max_bytes and remote_copy(
            args, args.remote + "/objects/" + path.name, path) and verified_object(item, stored, root, args):
        return "remote"
    block = item.get("p2p") or pins.get(item["id"], {}).get("p2p")
    peer = {**item, "sha256": stored["sha256"], "size": stored["size"], "p2p": block}
    return "p2p" if try_p2p(peer, path) and verified_object(item, stored, root, args) else ""


def recover_objects(item, old, root, args, pins):
    """The source label ("artstore" or "recovered") when every object is back, else ""."""
    objects = entry_objects(old)
    if not objects:
        return ""
    (root / "objects").mkdir(parents=True, exist_ok=True)
    tiers = set()
    try:
        for stored in objects:
            if not object_valid(root, stored):
                tier = recover_object(item, stored, root, args, pins)
                if not tier:
                    return ""
                tiers.add(tier)
    except (ValueError, KeyError, TypeError):
        return ""
    return "artstore" if tiers == {"artstore"} else "recovered"


def pin_as_entry(pin):
    """A reviewed pin knows the hashes, so a cold cache can still ask the remote or the swarm for them."""
    objects = [{**each, "status": "pinned", "evidence": "operator-seed", "metadata_unknown": True,
                "checked_at": 0} for each in pin["objects"]]
    return {"objects": objects, "size": sum(each["size"] for each in objects), "checked_at": 0,
            "status": "pinned", "evidence": "operator-seed", "metadata_unknown": True}


def has_validators(entry):
    return any(each.get("etag") or each.get("last_modified") for each in entry_objects(entry))


def upstream_current(entry, max_age_days):
    checked = entry.get("status") == "fresh" or (
        entry.get("status") == "verified" and entry.get("evidence") == "download-without-upstream-metadata")
    return checked and 0 <= time.time() - entry.get("checked_at", 0) < max_age_days * 86400


def download(item, old, root, args):
    result = refresh_one(item, old, root, args)
    if result.get("status") not in ("fresh", "verified") or not local_valid(root, result):
        raise ValueError("archive refresh did not produce a verified object")
    return result


def acquire(item, old, root, args, pins):
    """Return (entry, source): a verified local copy of one archive, downloading only what changed.

    Valid local object: reused when pinned, or when it has no validators and was checked inside
    --max-age-days; otherwise one conditional request (304 reuses it). A missing, corrupt or changed
    object comes from the artifact store, the optional remote, then P2P (all only for a hash the manifest
    knows; source "artstore" or "recovered"), then the website. When the website cannot be reached (restore and refresh alike, unless --force), the
    last valid local copy is returned unchanged with source "stale-local": its manifest entry keeps
    its old checked_at and validators, so the next run asks the website again.
    """
    if not (local_valid(root, old) and objects_match_selection(item, old)):
        source = recover_objects(item, old, root, args, pins)
        if source and objects_match_selection(item, old):
            return old, source
        return download(item, old, root, args), "website"
    if is_pinned(item, old, pins) or (
            not args.force and not has_validators(old) and upstream_current(old, args.max_age_days)):
        return old, "local"
    try:
        result = download(item, old, root, args)
    except (ValueError, OSError, HTTPException, subprocess.SubprocessError, zipfile.BadZipFile):
        if args.force:
            raise
        warn("upstream could not be checked for " + item["id"][:8] + "; using the last cached copy")
        return old, "stale-local"
    return result, "revalidated" if result.get("evidence") == "conditional-304" else "website"


def kept_objects(manifest, limit):
    """SHA-256s that fit in limit bytes: pinned first, then smallest. .bik is never kept."""
    sizes, pinned = {}, set()
    for entry in manifest["entries"].values():
        for stored in entry_objects(entry):
            if (isinstance(stored.get("sha256"), str) and SHA256.fullmatch(stored["sha256"])
                    and isinstance(stored.get("size"), int) and not is_bik(stored)):
                sizes[stored["sha256"]] = stored["size"]
                if entry.get("status") == "pinned":
                    pinned.add(stored["sha256"])
    keep, total = set(), 0
    for sha, size in sorted(sizes.items(), key=lambda pair: (pair[0] not in pinned, pair[1])):
        if total + size <= limit:
            keep.add(sha)
            total += size
    return keep


def prune(root, manifest, limit, hold=(), stage_dir=None):
    """Delete every object outside the kept set; the ones named in hold move to stage_dir instead.

    stage_dir must be outside root: it is the stage-only copy that never reaches the Actions cache.
    """
    keep = kept_objects(manifest, limit)
    for path in (root / "objects").iterdir():
        if path.is_file() and SHA256.fullmatch(path.name) and path.name not in keep:
            if stage_dir is not None and path.name in hold:
                stage_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(stage_dir / path.name))
            else:
                path.unlink()


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
    parser.add_argument("--archive-map", type=Path,
                        help="restore only: write {guide link URL: restored filename(s)} for the installer's "
                             "--archive-map; restored links only")
    parser.add_argument("--remote", help="optional read-only rclone remote:path: an extra source for objects the "
                                         "manifest knows by hash; unreachable means skipped; trusted operator input only")
    parser.add_argument("--artstore", action=argparse.BooleanOptionalAction, default=None,
                        help="restore/refresh: try the artifact store for objects the manifest knows by hash, "
                             "before --remote and P2P (default: on when ARTSTORE_TOKEN, GITHUB_TOKEN or GH_TOKEN is set)")
    parser.add_argument("--emit-stage-manifest", type=Path, metavar="FILE",
                        help="restore/refresh: write [{path, sha256}] for cached non-.bik objects the artifact "
                             "store lacks or will soon expire")
    parser.add_argument("--stage-max-bytes", type=int, default=STAGE_MAX_BYTES,
                        help="refresh with --emit-stage-manifest and --cache-limit-bytes: the largest single object "
                             "(default 3 GiB, one artifact pack) fetched only to be staged when it does not fit the cache")
    parser.add_argument("--stage-total-bytes", type=int, default=STAGE_TOTAL_BYTES,
                        help="refresh: most bytes of such stage-only objects per run (default 9 GiB, the three upload packs)")
    parser.add_argument("--stage-dir", type=Path,
                        help="refresh: where stage-only objects wait for the stage manifest (default: "
                             "<cache-dir>.stage-only, a sibling that is never part of the saved cache)")
    parser.add_argument("--cache-limit-bytes", type=int,
                        help="refresh only: keep only the objects in --cache-dir that fit in this many bytes "
                             "(pinned first, then smallest) and skip links whose objects do not; sized for the "
                             "10 GB Actions cache. Lanes fetch what is not kept; with --emit-stage-manifest "
                             "such objects are still fetched into --stage-dir to be staged")
    parser.add_argument("--force", action="store_true", help="refresh even inside freshness interval")
    parser.add_argument("--max-age-days", type=float, default=14)
    parser.add_argument("--timeout", type=float, default=30, help="socket timeout seconds")
    parser.add_argument("--download-timeout", type=float, default=1800)
    parser.add_argument("--integrity-timeout", type=float, default=300)
    parser.add_argument("--backend-timeout", type=float, default=1800)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-bytes", type=int, default=MAX_LINK_BYTES)
    parser.add_argument("--allow-uncached-by-policy", action="append", default=[], metavar="SECTION[::STEP]",
                        help="restore only: accept an uncached-by-policy link when every guide step that "
                             "references it is covered by this value (repeatable): a '##' section name "
                             "covers all of its '###' steps, '<## section>::<### heading>' covers only "
                             "that one step; the lane must not install what is named")
    args = parser.parse_args(argv)
    if args.allow_uncached_by_policy and args.command != "restore":
        parser.error("--allow-uncached-by-policy is only valid with restore")
    for allowance in args.allow_uncached_by_policy:
        try:
            parse_allowance(allowance)
        except ValueError as error:
            parser.error(str(error))
    if args.archive_map and args.command != "restore":
        parser.error("--archive-map is only valid with restore")
    if args.emit_stage_manifest and args.command not in ("restore", "refresh"):
        parser.error("--emit-stage-manifest is only valid with restore or refresh")
    if args.stage_dir is not None:
        cache_root, stage_root = args.cache_dir.resolve(), args.stage_dir.resolve()
        if stage_root == cache_root or cache_root in stage_root.parents:
            parser.error("--stage-dir must be outside --cache-dir")
    if args.stage_max_bytes <= 0 or args.stage_total_bytes < 0:
        parser.error("stage-max-bytes must be positive and stage-total-bytes must not be negative")
    if args.artstore is None:
        args.artstore = artstore_token()
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
            "planned", "existing-association", "already-present", "stale-unknown", "pinned", UNCACHED)
                           for entry in report["entries"])
    else:
        root = args.cache_dir.resolve()
        (root / "objects").mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema": 1, "entries": {}}
        if manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict):
            raise ValueError("unsupported manifest schema")
        if args.remote:
            pull_manifest(args, root, manifest)
        restored_names = {}
        try:
            pins = load_pins()
        except (ValueError, OSError):
            pins = {}
        losers = sibling_name_losers(items, manifest["entries"], pins) if args.command == "restore" else {}
        limit = args.cache_limit_bytes if args.command == "refresh" else None
        keep = kept_objects(manifest, limit) if limit is not None else None
        stage_dir = (args.stage_dir or root.parent / (root.name + ".stage-only")).resolve()
        stage_on = keep is not None and bool(args.emit_stage_manifest)
        stage = {"hold": set(), "left": args.stage_total_bytes}
        for item in items:
            old = manifest["entries"].get(item["id"]) or (
                pin_as_entry(pins[item["id"]]) if item["id"] in pins else {})
            entry = {"id": item["id"], "url": clean_url(item["url"]), "provider": item["provider"]}
            policy = uncached_reason(item)
            if policy:
                entry = uncached_entry(item, policy)
                if args.command == "restore" and args.allow_uncached_by_policy:
                    steps, outside = skippable_steps(item, args.allow_uncached_by_policy)
                    if steps and not outside:
                        entry.update(status=UNCACHED_SKIPPED, skipped_steps=steps)
                        report.setdefault("skipped_steps", []).extend(
                            {**step, "url": entry["url"], "reason": entry["reason"]} for step in steps)
                    else:
                        entry["reason"] += "; referenced outside the allowed sections or steps: " + json.dumps(
                            outside or "no guide step", sort_keys=True)
                report["entries"].append(entry)
                if args.command == "refresh" and manifest["entries"].pop(item["id"], None) is not None:
                    atomic_json(manifest_path, manifest)
                continue
            reserved = {}
            try:
                if keep is not None and entry_objects(old) and not all(
                        each.get("sha256") in keep for each in entry_objects(old)):
                    reserved = reserve_stage(old, keep, args, stage) if stage_on else None
                    if not reserved:
                        entry.update(status=OVER_LIMIT, reason="does not fit --cache-limit-bytes; fetched by the lanes")
                        report["entries"].append(entry)
                        continue
                before = manifest["entries"].get(item["id"])
                old, source = acquire(item, old, root, args, pins)
                entry["source"] = source
                pinned = is_pinned(item, old, pins)
                if old != before:
                    manifest["entries"][item["id"]] = old
                    atomic_json(manifest_path, manifest)
                if args.command == "refresh":
                    entry.update(old)
                    if reserved:
                        entry["stage_only"] = True
                    elif stage_on:
                        reserve_stage(old, kept_objects(manifest, limit), args, stage)
                else:
                    taken, created, restored = set(), [], []
                    try:
                        for each in entry_objects(old):
                            restored.append({"file": restore_object(item, each, root, args, restored_names,
                                                                    taken, created, losers.get(item["id"])),
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
                if limit is not None:
                    prune(root, manifest, limit, stage["hold"], stage_dir)
            except (ValueError, OSError, HTTPException, subprocess.SubprocessError, zipfile.BadZipFile) as error:
                if reserved:
                    release_stage(reserved, stage)
                    entry.update(status=OVER_LIMIT, reason="stage-only fetch failed: " + safe_reason(error))
                    report["entries"].append(entry)
                    prune(root, manifest, limit, stage["hold"], stage_dir)
                    continue
                entry.update(status="unresolved", reason=safe_reason(error))
                if args.command == "refresh" and old and not is_pinned(item, old, pins):
                    manifest["entries"][item["id"]] = {**old, "status": "stale-unknown"}
                    atomic_json(manifest_path, manifest)
            report["entries"].append(entry)
        if limit is not None:
            prune(root, manifest, limit, stage["hold"], stage_dir)
        if args.emit_stage_manifest:
            atomic_json(args.emit_stage_manifest, stage_candidates(root, manifest, stage_dir))
        sources = [each["source"] for each in report["entries"] if "source" in each]
        report["sources"] = {name: sources.count(name) for name in sorted(set(sources))}
    if args.command != "import-existing":
        stale = sum(1 for each in report["entries"] if each.get("status") == "stale-unknown")
        if stale and args.command == "refresh":
            warn("%d archive(s) are stale-unknown (upstream metadata unavailable); the last cached copy was kept" % stale)
        accepted = ("fresh", "verified", "restored", "pinned") + (
            (UNCACHED, OVER_LIMIT, "stale-unknown") if args.command == "refresh" else (UNCACHED_SKIPPED,) if args.command == "restore" else ())
        report["ok"] = all(entry.get("status") in accepted
                           for entry in report["entries"]) if args.command != "discover" else True
    if args.report:
        atomic_json(args.report, report)
    if args.archive_map:
        atomic_json(args.archive_map, archive_map(report))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def archive_map(report):
    mapping = {}
    for entry in report["entries"]:
        if entry.get("status") != "restored":
            continue
        names = [each["file"] for each in entry["files"]] if "files" in entry else [entry["file"]]
        merged = mapping.setdefault(entry["url"], [])
        merged.extend(name for name in names if name not in merged)
    return {url: names[0] if len(names) == 1 else names for url, names in mapping.items()}


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
