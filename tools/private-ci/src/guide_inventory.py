#!/usr/bin/env python3
"""Lossless public guide inventory. Never downloads resources or handles credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

URL = re.compile(r"https?://[^\s<>\"`]+", re.I)
MARKDOWN_LINK = re.compile(r"\[([^\]\n]*)\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
ARCHIVE = re.compile(r"\.(?:zip|rar|7z|exe|msi|tar|gz|bz2|xz)(?:$|[?#])", re.I)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DOWNLOAD_HINT = re.compile(r"download|mirror|patch|extract|install.{0,100}(?:from|link)", re.I)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def classify_url(url: str) -> dict[str, Any]:
    """Classify a published location without asserting that a page is a payload."""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    if parts.scheme not in ("http", "https"):
        return {"provider": "internal", "kind": "internal_reference", "download_candidate": False}
    if host.endswith("nexusmods.com") and (host == "nexusmods.com" or host.endswith(".nexusmods.com")):
        return {"provider": "nexus", "kind": "payload_candidate" if ARCHIVE.search(url) else "mod_page", "download_candidate": bool(ARCHIVE.search(url) or "/mods/" in path)}
    if host == "deadlystream.com" or host.endswith(".deadlystream.com"):
        file_page = "/files/file/" in path
        payload = bool(ARCHIVE.search(url))
        gateway = bool(re.search(r"[?&]do=download(?:&|$)", url, re.I))
        kind = "payload_candidate" if payload else "download_gateway_candidate" if gateway else "mod_page" if file_page else "forum_page"
        return {"provider": "deadlystream", "kind": kind, "download_candidate": file_page or payload or gateway}
    if host in ("mega.nz", "mega.co.nz"):
        return {"provider": "mega", "kind": "folder_reference" if "/folder/" in path or parts.fragment.startswith("F!") else "file_reference", "download_candidate": True}
    if ARCHIVE.search(url):
        return {"provider": "direct", "kind": "payload_candidate", "download_candidate": True}
    if "forum" in host or "/forum" in path or host in ("reddit.com", "www.reddit.com"):
        return {"provider": "forum", "kind": "forum_page", "download_candidate": False}
    return {"provider": "info", "kind": "informational_page", "download_candidate": False}


def line_links(line: str) -> list[dict[str, Any]]:
    found: dict[tuple[int, str], dict[str, Any]] = {}
    for match in URL.finditer(line):
        url = match.group(0).rstrip(".,;")
        # Strip Markdown/prose closers while retaining balanced parentheses and fragments.
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        url = url.rstrip(".,;")
        prefix = line[:match.start()]
        label = re.search(r"\[([^\]\n]*)\]\($", prefix)
        found[(match.start(), url)] = {"url": url, "column": match.start() + 1, "label": label.group(1) if label else None}
    for match in MARKDOWN_LINK.finditer(line):
        url = match.group(2)
        if not url.lower().startswith(("http://", "https://")):
            found[(match.start(2), url)] = {"url": url, "column": match.start(2) + 1, "label": match.group(1)}
    return [found[key] for key in sorted(found)]


def build_inventory(source: str | Path, *, game: str | None = None, source_url: str | None = None,
                    source_revision: str | None = None) -> dict[str, Any]:
    """Inventory exact source bytes, all ordered sections, and every published link."""
    source = Path(source)
    raw = source.read_bytes()
    text = raw.decode("utf-8")  # Fail explicitly; never rewrite bytes with replacement characters.
    lines = text.splitlines(keepends=True)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hierarchy: list[dict[str, Any]] = []
    in_comment = False
    fence: tuple[str, int] | None = None
    for number, line in enumerate(lines, 1):
        stripped = line.rstrip("\r\n")
        match = None
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", stripped)
        if fence_match and not in_comment:
            delimiter = fence_match.group(1)
            if fence is None:
                fence = (delimiter[0], len(delimiter))
            elif delimiter[0] == fence[0] and len(delimiter) >= fence[1] and stripped.strip() == delimiter:
                fence = None
        elif fence is None and not in_comment:
            match = HEADING.match(stripped)
        if fence is None:
            if "<!--" in stripped and stripped.rfind("<!--") > stripped.rfind("-->"):
                in_comment = True
                match = None
            elif in_comment and "-->" in stripped:
                in_comment = False
        if current is None or match:
            if current is not None:
                current["end_line"] = number - 1
                current["raw_text"] = "".join(current["raw_lines"])
            level = len(match.group(1)) if match else 0
            heading = match.group(2) if match else "Preamble"
            while hierarchy and hierarchy[-1]["level"] >= level:
                hierarchy.pop()
            parents = [parent["heading"] for parent in hierarchy]
            title_context = " / ".join([*parents, heading])
            optional = bool(re.search(r"optional|widescreen|extended widescreen", title_context, re.I))
            aspyr = bool(re.search(r"aspyr|critical:.*version", title_context, re.I))
            current = {"id": f"section-{len(sections):04d}-{digest(title_context)[:12]}",
                       "order": len(sections), "heading": heading, "level": level,
                       "parent_id": hierarchy[-1]["id"] if hierarchy else None,
                       "heading_path": [*parents, heading], "start_line": number,
                       "optional_variant": optional, "aspyr_variant": aspyr,
                       "raw_lines": [], "steps": [], "resource_ids": []}
            sections.append(current)
            if level:
                hierarchy.append(current)
        current["raw_lines"].append(line)
        if stripped.strip():
            current["steps"].append({"id": f"{current['id']}:line-{number}", "line": number,
                                     "raw_line": line, "kind": "heading" if match else "source_line", "interpretation": "source_text_only"})
    if current:
        current["end_line"] = len(lines)
        current["raw_text"] = "".join(current["raw_lines"])
    resources: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    for section in sections:
        section["named_mod"] = any(re.match(r"\s*\*\*Name\s*:", line, re.I) for line in section["raw_lines"])
        for offset, line in enumerate(section["raw_lines"]):
            number = section["start_line"] + offset
            for link in line_links(line):
                url = link["url"]
                owner = {"game": game, "guide_sha256": digest(raw), "section_id": section["id"], "line": number, "column": link["column"],
                         "step_id": f"{section['id']}:line-{number}", "label": link["label"], "raw_line": line,
                         "optional_variant": section["optional_variant"], "aspyr_variant": section["aspyr_variant"]}
                if url not in by_url:
                    classification = classify_url(url)
                    resource = {"id": "resource-" + digest(url), "url": url, "order": len(resources),
                                **classification, "owners": [], "status": "unresolved",
                                "payload_url": None, "filename": None, "sha256": None, "size": None}
                    resources.append(resource)
                    by_url[url] = resource
                resource = by_url[url]
                if resource["kind"] in ("forum_page", "informational_page") and (
                    DOWNLOAD_HINT.search(line) or re.match(r"\s*\*\*Name\s*:", line, re.I)
                    or (link["label"] and re.search(r"patch", link["label"], re.I))
                ):
                    resource["download_candidate"] = True
                    resource["candidate_reason"] = "download_related_source_text_requires_manual_resolution"
                owner["required_for_complete"] = resource["download_candidate"]
                resource["required_for_complete"] = resource["download_candidate"]
                resource["owners"].append(owner)
                if resource["id"] not in section["resource_ids"]:
                    section["resource_ids"].append(resource["id"])
    candidates = [resource for resource in resources if resource["download_candidate"]]
    return {"schema_version": 1, "game": game, "source": {"path": str(source), "url": source_url,
            "revision": source_revision, "sha256": digest(raw), "byte_length": len(raw), "line_count": len(lines)},
            "statement": "Source inventory and exact manual checklist only. Page URLs are not downloadable files. No resources were acquired or installation actions inferred.",
            "sections": sections, "resources": resources,
            "coverage": {"candidate_count": len(candidates), "resolved_count": 0,
                         "unresolved_ids": [resource["id"] for resource in candidates],
                         "percent": 100.0 if not candidates else 0.0, "complete": not candidates,
                         "includes_optional_variants": True, "generation": None,
                         "scope": "published_resource_mapping_and_payload_pins",
                         "offline_payload_verification_required_before_install": True}}


def combine_inventories(inventories: list[dict[str, Any]]) -> dict[str, Any]:
    """Union both raw guides into one required resource set and generation binding."""
    if not inventories:
        raise ValueError("At least one source guide is required")
    combined = {"schema_version": 1, "game": "combined", "sources": [], "sections": [], "resources": [],
                "statement": inventories[0]["statement"]}
    by_url: dict[str, dict[str, Any]] = {}
    for original in inventories:
        inv = json.loads(json.dumps(original))
        combined["sources"].append({**inv["source"], "game": inv["game"]})
        prefix = "guide-" + inv["source"]["sha256"][:12] + ":"
        for section in inv["sections"]:
            section["id"] = prefix + section["id"]
            section["parent_id"] = prefix + section["parent_id"] if section["parent_id"] else None
            section["guide_sha256"] = inv["source"]["sha256"]
            for step in section["steps"]:
                step["id"] = prefix + step["id"]
            combined["sections"].append(section)
        for resource in inv["resources"]:
            resource.update(status="unresolved", payload_url=None, filename=None, sha256=None, size=None)
            for owner in resource["owners"]:
                owner["section_id"] = prefix + owner["section_id"]
                owner["step_id"] = prefix + owner["step_id"]
            if resource["url"] not in by_url:
                resource["order"] = len(combined["resources"])
                by_url[resource["url"]] = resource
                combined["resources"].append(resource)
            else:
                existing = by_url[resource["url"]]
                existing["owners"].extend(resource["owners"])
                existing["download_candidate"] |= resource["download_candidate"]
                existing["required_for_complete"] |= resource["required_for_complete"]
    candidates = [resource for resource in combined["resources"] if resource["download_candidate"]]
    combined["coverage"] = {**inventories[0]["coverage"], "candidate_count": len(candidates), "resolved_count": 0,
                             "unresolved_ids": [resource["id"] for resource in candidates],
                             "percent": 0.0 if candidates else 100.0, "complete": not candidates, "generation": None}
    return combined


# Both spellings are accepted by consumers; neither consumes generated TOML.
combined_inventory = combine_inventories


def portable_archive_name(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[^/\\\x00-\x1f<>:\"|?*]+\.(?:zip|rar|7z|exe)", value, re.I):
        return False
    if value != unicodedata.normalize("NFC", value) or value.endswith((" ", ".")):
        return False
    return not re.match(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", value, re.I)


def reconcile_catalog(inventory: dict[str, Any], catalog: dict[str, Any], *,
                      catalog_bytes: bytes | None = None,
                      expected_generation_digest: str | None = None) -> dict[str, Any]:
    """Bind published resource identities to explicitly resolved, pinned payloads."""
    result = json.loads(json.dumps(inventory))
    entries = catalog.get("sources", [])
    if not isinstance(entries, list):
        raise ValueError("catalog.sources must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] in by_id:
            raise ValueError("catalog source identities must be unique strings")
        by_id[entry["id"]] = entry
    generation = catalog.get("generation")
    verified_generation = bool(catalog_bytes is not None and isinstance(expected_generation_digest, str)
                               and SHA256.fullmatch(expected_generation_digest)
                               and digest(catalog_bytes) == expected_generation_digest
                               and json.loads(catalog_bytes) == catalog
                               and isinstance(generation, str) and generation)
    if catalog_bytes is not None and not verified_generation:
        raise ValueError("cache generation bytes, contents, and immutable digest must match")
    resolved = 0
    unresolved = []
    for resource in result["resources"]:
        if not resource["download_candidate"]:
            continue
        entry = by_id.get(resource["id"])
        valid = bool(entry and verified_generation and isinstance(entry.get("sha256"), str) and SHA256.fullmatch(entry["sha256"])
                     and type(entry.get("size")) is int and entry["size"] > 0
                     and isinstance(entry.get("url"), str) and urlsplit(entry["url"]).scheme == "https"
                     and portable_archive_name(entry.get("filename"))
                     and (not entry.get("sourceUrl") or entry["sourceUrl"] == resource["url"]))
        if valid and classify_url(entry["url"])["kind"] in ("mod_page", "forum_page", "folder_reference", "file_reference"):
            valid = False  # A pinned digest never turns a provider page into an archive URL.
        if valid:
            resource.update(status="resolved", payload_url=entry["url"], filename=entry["filename"],
                            sha256=entry["sha256"], size=entry["size"], revision=entry.get("revision"))
            resolved += 1
        else:
            unresolved.append(resource["id"])
    total = result["coverage"]["candidate_count"]
    result["coverage"].update(resolved_count=resolved, unresolved_ids=unresolved,
                              percent=100.0 if not total else 100.0 * resolved / total,
                              complete=verified_generation and not unresolved, generation=generation,
                              generation_digest=expected_generation_digest if verified_generation else None)
    return result


def manual_checklist(inventory: dict[str, Any]) -> str:
    """Emit exact guide sections, with no synthetic install actions or substitutions."""
    return "".join(section["raw_text"] for section in inventory["sections"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="+")
    parser.add_argument("--game", choices=("k1", "k2"))
    parser.add_argument("--source-url")
    parser.add_argument("--source-revision")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--generation-digest", help="SHA256 of the exact immutable catalog manifest bytes")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checklist", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    inventories = [build_inventory(source, game=args.game or (source.parent.name if source.parent.name in ("k1", "k2") else None),
                                   source_url=args.source_url if len(args.source) == 1 else None,
                                   source_revision=args.source_revision) for source in args.source]
    inventory = inventories[0] if len(inventories) == 1 else combine_inventories(inventories)
    if args.catalog:
        catalog_bytes = args.catalog.read_bytes()
        inventory = reconcile_catalog(inventory, json.loads(catalog_bytes), catalog_bytes=catalog_bytes,
                                      expected_generation_digest=args.generation_digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.checklist:
        args.checklist.write_bytes(manual_checklist(inventory).encode("utf-8"))
    print(json.dumps({"source_sha256": [source["sha256"] for source in inventory.get("sources", [inventory.get("source")])],
                      "coverage": {key: value for key, value in inventory["coverage"].items() if key != "unresolved_ids"}}))
    return 2 if args.require_complete and not inventory["coverage"]["complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
