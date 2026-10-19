#!/usr/bin/env python3
"""Private, bounded provider metadata resolution; never guesses archive variants.

Outputs are private cache inputs, not proof that payloads were acquired. Signed URLs,
CSRF keys and session headers are intentionally absent from stdout and reprs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from email.message import Message
import hashlib
from html.parser import HTMLParser
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
import urllib.error
import urllib.request

MAX_METADATA = 2 * 1024 * 1024
MAX_LINKS = 32
REFRESH_SECONDS = 14 * 24 * 60 * 60
ARCHIVE = re.compile(r"\.(?:zip|rar|7z|exe|msi|tar|tar\.gz|gz|bz2|xz)$", re.I)
BINARY_TYPES = {"application/zip", "application/x-zip-compressed", "application/vnd.rar", "application/x-rar-compressed",
                "application/x-7z-compressed", "application/octet-stream", "binary/octet-stream", "application/x-msdownload",
                "application/vnd.microsoft.portable-executable", "application/gzip", "application/x-bzip2", "application/x-xz", "application/x-tar"}
DS_HOSTS = {"deadlystream.com", "www.deadlystream.com"}
NEXUS_API = "https://api.nexusmods.com/v1"


class ResolutionBoundary(Exception):
    """Static reason codes only, so transport exceptions cannot leak signed URLs."""


def safe_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ResolutionBoundary("invalid-source-url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ResolutionBoundary("invalid-source-url") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ResolutionBoundary("https-source-without-credentials-required")
    try:
        if port not in (None, 443):
            raise ResolutionBoundary("nonstandard-source-port")
        address = ipaddress.ip_address(parsed.hostname)
        if not address.is_global:
            raise ResolutionBoundary("nonpublic-source-address")
    except ValueError:
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"} or parsed.hostname.lower().endswith(".localhost"):
            raise ResolutionBoundary("nonpublic-source-address")
    return value


def safe_filename(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value in (".", "..") or any(char in value for char in "/\\\x00:") or any(ord(char) < 32 for char in value):
        raise ResolutionBoundary("unsafe-download-filename")
    if not ARCHIVE.search(value):
        raise ResolutionBoundary("archive-filename-required")
    return value


def identity(provider: str, metadata: dict[str, Any]) -> str:
    stable = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return provider + ":" + hashlib.sha256(stable.encode()).hexdigest()


def strong_etag(value: str | None) -> str | None:
    return value if value and not value.lower().startswith("w/") and value.startswith('"') and value.endswith('"') else None


@dataclass(repr=False)
class HttpResponse:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self):
        self.headers = {key.lower(): value for key, value in self.headers.items()}

    def __repr__(self):
        return f"HttpResponse(status={self.status}, private_metadata=True)"


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str] | None):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, request, response, code, message, headers, new_url):
        safe_url(new_url)
        old, new = urlsplit(request.full_url), urlsplit(new_url)
        if self.hosts and new.hostname.lower() not in self.hosts:
            raise ResolutionBoundary("provider-redirect-outside-authorized-hosts")
        protected = any(key.lower() in {"authorization", "apikey", "cookie"} for key, _ in request.header_items())
        if protected and old.hostname.lower() != new.hostname.lower():
            raise ResolutionBoundary("credential-redirect-refused")
        return super().redirect_request(request, response, code, message, headers, new_url)


class HttpClient:
    """A separate cookie jar per provider resolution; archive bodies are never read."""
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.requests = 0

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                allowed_hosts: set[str] | None = None, metadata: bool = False) -> HttpResponse:
        safe_url(url)
        if method not in {"GET", "HEAD"}:
            raise ResolutionBoundary("metadata-method-refused")
        if allowed_hosts and urlsplit(url).hostname.lower() not in allowed_hosts:
            raise ResolutionBoundary("provider-host-refused")
        self.requests += 1
        if self.requests > 12:
            raise ResolutionBoundary("provider-request-limit")
        opener = urllib.request.build_opener(RestrictedRedirect(allowed_hosts), urllib.request.HTTPCookieProcessor(self.cookies))
        request = urllib.request.Request(url, method=method, headers={"User-Agent": "ModSync metadata resolver/1.0", **(headers or {})})
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        except ResolutionBoundary:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            raise ResolutionBoundary("provider-transport-unavailable") from None
        with response:
            final_url = safe_url(response.url)
            kind = response.headers.get_content_type().lower()
            body = b""
            if metadata and method == "GET" and (kind in {"application/json", "text/html", "application/xhtml+xml"} or kind.endswith("+json")):
                body = response.read(MAX_METADATA + 1)
                if len(body) > MAX_METADATA:
                    raise ResolutionBoundary("provider-metadata-limit")
            return HttpResponse(response.status, final_url, dict(response.headers.items()), body)

    def cookie_header(self, url: str) -> str | None:
        request = urllib.request.Request(url)
        self.cookies.add_cookie_header(request)
        return request.get_header("Cookie")


def require_success(response: HttpResponse) -> None:
    if response.status in (401, 403):
        raise ResolutionBoundary("manual-authentication-required")
    if response.status == 429:
        raise ResolutionBoundary("provider-rate-limit")
    if not 200 <= response.status < 300:
        raise ResolutionBoundary("provider-metadata-unavailable")


def json_metadata(response: HttpResponse) -> Any:
    require_success(response)
    if "json" not in response.headers.get("content-type", "").lower():
        raise ResolutionBoundary("provider-json-required")
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeError):
        raise ResolutionBoundary("invalid-provider-metadata") from None


def expected_hash(pin: dict[str, Any]) -> str | None:
    value = pin.get("sha256")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ResolutionBoundary("invalid-pinned-sha256")
    return value.lower()


def payload_metadata(client: HttpClient, resource: dict[str, Any], pin: dict[str, Any], url: str,
                     *, provider_identity: dict[str, Any] | None = None,
                     headers: dict[str, str] | None = None, allowed_hosts: set[str] | None = None) -> dict[str, Any]:
    response = client.request("HEAD", safe_url(url), headers=headers, allowed_hosts=allowed_hosts)
    require_success(response)
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"} or mime.startswith("text/"):
        raise ResolutionBoundary("html-or-text-is-not-an-archive")
    if mime and mime not in BINARY_TYPES:
        raise ResolutionBoundary("unsupported-payload-content-type")
    name = pin.get("filename")
    if not name and response.headers.get("content-disposition"):
        message = Message()
        message["content-disposition"] = response.headers["content-disposition"]
        name = message.get_filename()
    if not name:
        name = urlsplit(response.url).path.rsplit("/", 1)[-1]
    name = safe_filename(name)
    try:
        size = int(response.headers["content-length"]) if response.headers.get("content-length") else None
    except (ValueError, TypeError):
        raise ResolutionBoundary("invalid-payload-size") from None
    if size is not None and size <= 0:
        raise ResolutionBoundary("empty-payload")
    etag = strong_etag(response.headers.get("etag"))
    modified = response.headers.get("last-modified")
    checksum = expected_hash(pin)
    stable = {**(provider_identity or {}), "filename": name, "etag": etag, "lastModified": modified, "size": size}
    if not provider_identity and not (etag or modified or checksum or pin.get("revision")):
        raise ResolutionBoundary("pinned-hash-or-file-version-required")
    if pin.get("revision") is not None and not isinstance(pin["revision"], str):
        raise ResolutionBoundary("invalid-pinned-revision")
    result = {"id": resource["id"], "sourceUrl": resource["url"], "url": response.url,
              "provider": resource.get("provider", "direct"), "filename": name,
              "revision": identity(resource.get("provider", "direct"), {**stable, "pinnedRevision": pin.get("revision"), "sha256": checksum}),
              "status": "resolved"}
    if etag:
        result["etag"] = etag
    if modified:
        result["lastModified"] = modified
    if size is not None:
        result["size"] = size
    if checksum:
        result["sha256"] = checksum
    if headers:
        result["requestHeaders"] = dict(headers)  # Ephemeral private cache transport; never a frozen/public field.
    owner = resource.get("ownerId") or resource.get("owner_id")
    if owner:
        result["ownerId"] = owner
    return result


class DownloadPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.csrf: str | None = None
        self.modified: str | None = None
        self.times: list[str] = []
        self.version: str | None = None
        self._version_depth = 0
        self._version_text: list[str] = []

    def handle_starttag(self, tag, attributes):
        data = dict(attributes)
        if tag == "a" and data.get("href") and ("do=download" in data["href"] or "/files/download/" in data["href"]):
            self.links.append(data["href"])
            if len(self.links) > MAX_LINKS:
                raise ResolutionBoundary("provider-link-limit")
        if tag == "input" and data.get("name", "").lower() == "csrfkey":
            self.csrf = data.get("value")
        if tag == "meta" and data.get("property") in {"article:modified_time", "og:updated_time"}:
            self.modified = data.get("content")
        if tag == "time" and data.get("datetime"):
            self.times.append(data["datetime"])
        if data.get("itemprop") == "version":
            self._version_depth = 1
        elif self._version_depth:
            self._version_depth += 1

    def handle_endtag(self, _tag):
        if self._version_depth:
            self._version_depth -= 1
            if not self._version_depth:
                self.version = "".join(self._version_text).strip()

    def handle_data(self, text):
        if self._version_depth:
            self._version_text.append(text)


def parse_page(response: HttpResponse) -> DownloadPage:
    require_success(response)
    if "html" not in response.headers.get("content-type", "").lower():
        raise ResolutionBoundary("provider-page-required")
    try:
        text = response.body.decode("utf-8")
    except UnicodeError:
        raise ResolutionBoundary("invalid-provider-page") from None
    lowered = text.lower()
    if any(marker in lowered for marker in ("cf-chl-", "g-recaptcha", "hcaptcha", "verify you are human", "captcha challenge", "just a moment...")):
        raise ResolutionBoundary("manual-antibot-or-captcha-required")
    page = DownloadPage()
    page.feed(text)
    if not page.links and any(marker in lowered for marker in ("you must be logged in", "sign in to download", "permission denied")):
        raise ResolutionBoundary("manual-authentication-required")
    return page


def download_id(url: str) -> str | None:
    query = parse_qs(urlsplit(url).query)
    if query.get("r"):
        return query["r"][0]
    match = re.search(r"/files/download/(\d+)", urlsplit(url).path)
    return match.group(1) if match else None


def resolve_deadlystream(client: HttpClient, resource: dict[str, Any], pin: dict[str, Any]) -> dict[str, Any]:
    source = safe_url(resource["url"])
    parsed = urlsplit(source)
    match = re.search(r"/files/file/(\d+)", parsed.path)
    if parsed.hostname.lower() not in DS_HOSTS or not match:
        raise ResolutionBoundary("deadlystream-file-page-required")
    page_url = urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
    response = client.request("GET", page_url, allowed_hosts=DS_HOSTS, metadata=True)
    page = parse_page(response)
    version = {"filePageId": match.group(1), "version": page.version,
               "updated": page.modified}
    links = list(dict.fromkeys(urljoin(response.url, link) for link in page.links))
    if not links and page.csrf:
        links = [page_url + "?" + urlencode({"do": "download", "csrfKey": page.csrf})]
    if not links:
        raise ResolutionBoundary("deadlystream-layout-or-selection-unresolved")
    selected_id = str(pin["downloadId"]) if pin.get("downloadId") is not None else None
    # A selection page is fetched only as bounded markup. Binary bodies are never read.
    candidates: list[str] = []
    for link in links:
        if urlsplit(safe_url(link)).hostname.lower() not in DS_HOSTS:
            raise ResolutionBoundary("provider-redirect-outside-authorized-hosts")
        if selected_id and download_id(link) not in (None, selected_id):
            continue
        probe = client.request("HEAD", link, headers={"Referer": page_url}, allowed_hosts=DS_HOSTS)
        require_success(probe)
        if "html" not in probe.headers.get("content-type", "").lower():
            candidates.append(link)
            continue
        selection = client.request("GET", link, headers={"Referer": page_url}, allowed_hosts=DS_HOSTS, metadata=True)
        chosen = parse_page(selection)
        candidates.extend(urljoin(selection.url, child) for child in chosen.links)
    candidates = list(dict.fromkeys(candidates))
    if selected_id:
        candidates = [url for url in candidates if download_id(url) == selected_id]
    if len(candidates) != 1:
        raise ResolutionBoundary("explicit-deadlystream-download-id-required" if candidates else "deadlystream-selected-file-unavailable")
    chosen = safe_url(candidates[0])
    if urlsplit(chosen).hostname.lower() not in DS_HOSTS:
        raise ResolutionBoundary("provider-redirect-outside-authorized-hosts")
    version["downloadId"] = download_id(chosen)
    headers = {"Referer": page_url}
    cookie = client.cookie_header(chosen)
    if cookie:
        headers["Cookie"] = cookie
    return payload_metadata(client, resource, pin, chosen, provider_identity=version, headers=headers, allowed_hosts=DS_HOSTS)


def resolve_nexus(client: HttpClient, resource: dict[str, Any], pin: dict[str, Any], environment: dict[str, str]) -> dict[str, Any]:
    parsed = urlsplit(safe_url(resource["url"]))
    match = re.fullmatch(r"/([A-Za-z0-9_-]+)/mods/(\d+)/?", parsed.path)
    if parsed.hostname.lower() not in {"nexusmods.com", "www.nexusmods.com"} or not match:
        raise ResolutionBoundary("official-nexus-mod-page-required")
    key = environment.get("NEXUS_API_KEY")
    if not key:
        raise ResolutionBoundary("nexus-api-key-required")
    explicit = pin.get("fileId")
    authored = parse_qs(parsed.query).get("file_id", [None])[0]
    file_id = explicit if explicit is not None else authored
    if file_id is None or not str(file_id).isdigit() or int(file_id) <= 0:
        raise ResolutionBoundary("explicit-nexus-file-id-required")
    headers = {"apikey": key, "accept": "application/json"}
    account = json_metadata(client.request("GET", NEXUS_API + "/users/validate.json", headers=headers,
                                           allowed_hosts={"api.nexusmods.com"}, metadata=True))
    if not isinstance(account, dict) or account.get("is_premium") is not True:
        raise ResolutionBoundary("nexus-premium-download-authorization-required")
    game, mod = match.groups()
    base = f"{NEXUS_API}/games/{game}/mods/{mod}/files"
    listing = json_metadata(client.request("GET", base + ".json", headers=headers,
                                           allowed_hosts={"api.nexusmods.com"}, metadata=True))
    files = listing.get("files", []) if isinstance(listing, dict) else []
    if not isinstance(files, list):
        raise ResolutionBoundary("invalid-provider-metadata")
    choices = [item for item in files if isinstance(item, dict) and item.get("file_id") == int(file_id)]
    if len(choices) != 1:
        raise ResolutionBoundary("pinned-nexus-file-unavailable")
    chosen = choices[0]
    links = json_metadata(client.request("GET", base + f"/{int(file_id)}/download_link.json", headers=headers,
                                         allowed_hosts={"api.nexusmods.com"}, metadata=True))
    if not isinstance(links, list) or not links or not isinstance(links[0], dict) or not isinstance(links[0].get("URI"), str):
        raise ResolutionBoundary("nexus-download-link-unavailable")
    version = {"game": game, "modId": int(mod), "fileId": int(file_id), "version": chosen.get("version"),
               "uploadedTimestamp": chosen.get("uploaded_timestamp"), "fileName": chosen.get("file_name"), "sizeBytes": chosen.get("size_in_bytes")}
    effective = {**pin, "filename": pin.get("filename") or chosen.get("file_name")}
    return payload_metadata(client, resource, effective, safe_url(links[0]["URI"]), provider_identity=version)


def resolve_resource(resource: dict[str, Any], pin: dict[str, Any] | None = None, *,
                     environment: dict[str, str] | None = None, client: HttpClient | None = None) -> dict[str, Any]:
    """Return a concrete private cache input or an explicit unresolved prerequisite."""
    pin = pin or {}
    client = client or HttpClient()
    environment = os.environ if environment is None else environment
    source_id = resource.get("id") or resource.get("ownerId") or resource.get("owner_id")
    provider = resource.get("provider", "direct")
    if not isinstance(source_id, str) or not source_id or not isinstance(resource.get("url"), str):
        raise ValueError("Resource requires a stable identity and published URL")
    resource = {**resource, "id": source_id}
    try:
        expected_hash(pin)
        mapped = pin.get("downloadUrl") or pin.get("url")
        if mapped:
            return payload_metadata(client, resource, pin, safe_url(mapped), provider_identity=None)
        if provider == "nexus":
            return resolve_nexus(client, resource, pin, environment)
        if provider == "deadlystream":
            return resolve_deadlystream(client, resource, pin)
        if provider == "mega":
            raise ResolutionBoundary("official-mega-api-or-sdk-resolver-not-configured")
        if provider in {"direct", "github"}:
            return payload_metadata(client, resource, pin, safe_url(resource["url"]))
        raise ResolutionBoundary("reviewed-provider-download-mapping-required")
    except ResolutionBoundary as boundary:
        result = {"id": source_id, "provider": provider, "status": "unresolved", "reason": str(boundary)}
        owner = resource.get("ownerId") or resource.get("owner_id")
        if owner:
            result["ownerId"] = owner
        return result


def resolve_catalog(inventory: dict[str, Any] | list[dict[str, Any]], pins: dict[str, dict[str, Any]] | None = None, *,
                    environment: dict[str, str] | None = None, client_factory=HttpClient) -> dict[str, Any]:
    resources = inventory.get("resources", []) if isinstance(inventory, dict) else inventory
    if not isinstance(resources, list):
        raise ValueError("Inventory resources must be a list")
    pins = pins or {}
    if not isinstance(pins, dict) or any(not isinstance(value, dict) for value in pins.values()):
        raise ValueError("Provider pins must map identities to objects")
    if any(not isinstance(resource, dict) for resource in resources):
        raise ValueError("Inventory resources must be objects")
    identifiers = [resource.get("id") for resource in resources]
    if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Inventory requires unique stable resource identities")
    candidates = [resource for resource in resources if resource.get("download_candidate", True)]
    entries = [resolve_resource(resource, pins.get(resource.get("id")) or pins.get(resource.get("ownerId")) or {},
                                environment=environment, client=client_factory()) for resource in candidates]
    unresolved = [entry["id"] for entry in entries if entry["status"] != "resolved"]
    return {"schemaVersion": 1, "sources": entries, "refreshAfterSeconds": REFRESH_SECONDS,
            "coverage": {"candidateCount": len(entries), "resolvedCount": len(entries) - len(unresolved),
                         "unresolvedIds": unresolved, "complete": not unresolved},
            "statement": "Provider metadata resolution only. Complete acquisition additionally requires cache payload verification; unresolved prerequisites block full-build coverage."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--pins", type=Path, help="Private explicit file IDs or reviewed download mappings")
    parser.add_argument("--output", type=Path, required=True, help="Private resolver catalog; never publish this file")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    catalog = resolve_catalog(json.loads(args.inventory.read_text()), json.loads(args.pins.read_text()) if args.pins else {})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(catalog, output, indent=2)
        output.write("\n")
    print(json.dumps({"metadataOnly": True, "coverage": catalog["coverage"]}))
    return 2 if args.require_complete and not catalog["coverage"]["complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
