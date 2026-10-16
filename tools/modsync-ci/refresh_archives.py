#!/usr/bin/env python3
"""Refresh the private ModSync archive cache one due link at a time.

Restores only the manifest, selects due links (never-attempted first, then oldest
attempt), restores and verifies each selected object, refreshes it with cache.py
under a 3 GiB disk reserve, uploads the object and manifest, then evicts it.
Only sanitized totals are printed: no URLs, remote paths, rclone output or secrets.

Exit codes: 0 run completed (unresolved links are totals, not errors) or cache not
configured; 1 infrastructure failure (backend unreachable,
corrupt manifest, persistence failed); 2 usage error. A partly configured backend
is reported as a warning and exits 0 so a configuration gap cannot fail the pipeline.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

spec = importlib.util.spec_from_file_location("cache", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)
discover_all = cache.discover

SECRETS = ("RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID", "RCLONE_CONFIG_MODCACHE_SECRET_ACCESS_KEY",
           "RCLONE_CONFIG_MODCACHE_ENDPOINT")
RESERVE = 3 * 1024 ** 3
FRESH_SECONDS = 14 * 86400
MISSING = (3, 4)
FAILURE_MARKERS = (
    ("permission", ("access denied", "accessdenied", "permission denied", "forbidden", "unauthorized", "status code: 403")),
    ("flag", ("unknown flag", "unknown shorthand flag", "flag needs an argument", "invalid argument")),
    ("immutable", ("immutable",)),
    ("network", ("timeout", "timed out", "connection", "no such host", "network", "tls handshake")),
)
ADAPTER_REASONS = {
    "landing page has no unique archive endpoint; attachment selection/provider adapter required",
    "landing URL has no explicit archive endpoint",
    "archive endpoint returned HTML; provider adapter required",
    "download endpoint did not resolve to an attachment; provider adapter required",
    *(name + " requires an authenticated/provider adapter" for name in ("nexus", "mega", "google-drive", "gamefront")),
}
SELECTION_PREFIXES = ("reviewed archive selection for file", "landing page lists multiple files and no reviewed selection",
                      "fetched files do not match the reviewed multi-file selection")
NETWORK_REASONS = {"network request failed", "total download timeout", "TimeoutError", "URLError", "HTTPException"}
INTEGRITY_REASONS = {
    "not a supported archive (HTML and unknown payloads rejected)", "truncated archive", "invalid manifest SHA256",
    "restore SHA256 mismatch", "cache object conflict", "expected complete HTTP 200 archive",
}
PROVIDERS = {"deadlystream", "nexus", "mega", "google-drive", "gamefront", "dropbox", "http"}


class Stop(BaseException):
    """Raised from SIGALRM/SIGTERM so the current item unwinds and persistence still runs."""


def rclone(*args, timeout=600):
    """Run rclone with private stderr; failures carry a coarse category only."""
    with tempfile.TemporaryFile() as diagnostics:
        try:
            subprocess.run(["rclone", *map(str, args), "--contimeout", "20s", "--timeout", "60s"],
                           check=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=diagnostics)
        except subprocess.CalledProcessError as error:
            diagnostics.seek(0)
            message = diagnostics.read(65536).decode("utf-8", errors="replace").lower()
            error.failure_category = next((name for name, markers in FAILURE_MARKERS
                                           if any(marker in message for marker in markers)), "unclassified")
            print("rclone", args[0], "failed:", error.failure_category, f"(exit {error.returncode})", file=sys.stderr)
            raise


def fetch(remote_path, local_path, *extra):
    """Copy one remote file; return False when it does not exist."""
    try:
        rclone("copyto", remote_path, local_path, "--retries", "1", *extra)
        return Path(local_path).is_file()
    except subprocess.CalledProcessError as error:
        if error.returncode in MISSING:
            return False
        raise


def matches(path, sha256, size):
    return path.is_file() and path.stat().st_size == size and cache.digest(path) == sha256


def upload_object(path, remote_path):
    sha256, size = path.name, path.stat().st_size
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or cache.digest(path) != sha256:
        raise ValueError("object source failed SHA256 identity verification")
    existing = path.with_name(".download-existing")
    try:
        if fetch(remote_path, existing) and not matches(existing, sha256, size):
            raise ValueError("existing remote object failed size/SHA256 verification")
    finally:
        existing.unlink(missing_ok=True)
    if not matches(path, sha256, size):
        raise ValueError("object source changed before upload")
    rclone("copyto", path, remote_path, "--retries", "1", "--checksum")
    if not matches(path, sha256, size):
        raise ValueError("object source changed during upload")


def is_due(entry, now, item=None, pins=None):
    if item is not None and (cache.uncached_reason(item) or cache.is_pinned(item, entry, pins)):
        return False
    upstream_checked = entry.get("status") == "fresh" or (
        entry.get("status") == "verified" and entry.get("evidence") == "download-without-upstream-metadata")
    current = item is None or cache.objects_match_selection(item, entry)
    return not (upstream_checked and current and 0 <= now - entry.get("checked_at", 0) < FRESH_SECONDS)


def refresh_item(item, root, remote, force):
    """Restore, refresh, persist and evict one item; returns cache.py's sanitized report."""
    manifest_path = root / "manifest.json"
    policy = cache.uncached_reason(item)
    if policy:
        yield {"entries": [cache.uncached_entry(item, policy)], "ok": True}
        return
    manifest = json.loads(manifest_path.read_text())
    manifest.setdefault("refresh_attempts", {})[item["id"]] = time.time()
    cache.atomic_json(manifest_path, manifest)
    old = manifest["entries"].get(item["id"], {})
    yield "restore"
    for stored in cache.entry_objects(old):
        path, size = cache.object_path(root, stored), stored.get("size")
        if not isinstance(size, int) or size < 0 or size + RESERVE >= shutil.disk_usage(root).free:
            raise ValueError("insufficient disk to restore selected object")
        temporary = root / "objects" / ".download-restore"
        try:
            if fetch(f"{remote}/objects/{path.name}", temporary):
                if not matches(temporary, stored["sha256"], size):
                    raise ValueError("restored object failed size/SHA256 verification")
                cache.validate(temporary, 300, loose=cache.entry_loose(item, stored))
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    available = min(shutil.disk_usage(root).free - RESERVE, cache.MAX_LINK_BYTES)
    if available <= 0:
        raise ValueError("insufficient download disk")
    yield "refresh"
    report_path = root.parent / "item-report.json"
    report_path.unlink(missing_ok=True)
    cache.discover = lambda guides: [item]
    argv = ["refresh", "--cache-dir", str(root), "--max-age-days", "14", "--max-bytes", str(available),
            "--download-timeout", "600", "--integrity-timeout", "300", "--timeout", "20", "--retries", "1",
            "--report", str(report_path)] + (["--force"] if force else [])
    with contextlib.redirect_stdout(io.StringIO()):
        cache.main(argv)
    result = json.loads(report_path.read_text())
    yield "persist-object"
    uploaded = set()
    for entry in json.loads(manifest_path.read_text())["entries"].values():
        for stored in cache.entry_objects(entry):
            if stored.get("sha256") not in uploaded and cache.local_valid(root, stored):
                path = cache.object_path(root, stored)
                upload_object(path, f"{remote}/objects/{path.name}")
                uploaded.add(stored["sha256"])
    yield "persist-manifest"
    rclone("copyto", manifest_path, f"{remote}/manifest.json", "--retries", "1")
    yield "evict"
    for path in (root / "objects").iterdir():
        if path.is_file():
            path.unlink()
    yield result


def run_item(item, root, remote, force):
    stage = "attempt-manifest"
    try:
        for step in refresh_item(item, root, remote, force):
            if isinstance(step, dict):
                return step["entries"], step["ok"]
            stage = step
    except Exception as error:
        failure = {"id": item["id"], "status": "unresolved", "stage": stage,
                   "reason": "item operation failed; private diagnostics withheld",
                   "exception_category": type(error).__name__,
                   "source_frames": [f"{Path(frame.filename).name[:80]}:{frame.lineno}:{frame.name[:80]}"
                                     for frame in traceback.extract_tb(error.__traceback__)[-6:]]}
        if isinstance(error, subprocess.CalledProcessError):
            failure["returncode"] = error.returncode
            failure["failure_category"] = getattr(error, "failure_category", "unclassified")
        return [failure], False


def classify(reason):
    if not isinstance(reason, str):
        return "other", None
    if reason in ADAPTER_REASONS:
        return "adapter-required", None
    if reason == "provider challenge/CAPTCHA; blocked-pending-user-action":
        return "captcha-user-action", None
    if reason.startswith("nexus requires NEXUS_API_KEY"):
        return "credentials-required", None
    match = re.fullmatch(r"HTTP ([45][0-9]{2})", reason)
    if match:
        return "http-status", int(match[1])
    if reason in NETWORK_REASONS:
        return "network", None
    if reason in INTEGRITY_REASONS:
        return "integrity", None
    if reason.startswith(SELECTION_PREFIXES):
        return "selection-review", None
    return "other", None


def totals(entries, manifest, started, stop, ok, persisted, max_items):
    counts = Counter(entry.get("status") for entry in entries)
    checked = [entry for entry in manifest.get("entries", {}).values() if entry.get("checked_at", 0) >= started]
    verified = [stored for entry in checked for stored in cache.entry_objects(entry)
                if stored.get("evidence") in ("download-200", "download-without-upstream-metadata")]
    unresolved = Counter()
    other_reasons = Counter()
    for entry in entries:
        if entry.get("status") == "unresolved":
            provider = entry.get("provider") if entry.get("provider") in PROVIDERS else "unknown"
            unresolved[(provider, *classify(entry.get("reason")))] += 1
            if classify(entry.get("reason"))[0] in ("other", "selection-review"):
                other_reasons[provider + ": " + str(entry.get("reason"))[:160]] += 1
    stages = {}
    for entry in entries:
        if entry.get("stage"):
            stage = stages.setdefault(entry["stage"], {"count": 0, "returncodes": Counter(),
                                                       "failure_categories": Counter(),
                                                       "exception_categories": Counter(), "source_frames": []})
            stage["count"] += 1
            stage["exception_categories"][entry["exception_category"]] += 1
            stage["failure_categories"][entry.get("failure_category", "none")] += 1
            if "returncode" in entry:
                stage["returncodes"][str(entry["returncode"])] += 1
            if len(stage["source_frames"]) < 10:
                stage["source_frames"].append(entry["source_frames"])
    return {
        "max_items": max_items, "state": stop, "reported_entries": len(entries),
        "fresh": counts["fresh"], "verified": counts["verified"], "pinned": counts["pinned"],
        "stale_unknown": counts["stale-unknown"], "uncached_by_policy": counts[cache.UNCACHED],
        "unresolved": counts["unresolved"], "manifest_checked": len(checked),
        "verified_downloads": len(verified), "verified_bytes": sum(entry.get("size", 0) for entry in verified),
        "refresh_complete": int(ok and persisted and stop == "finished"), "persistence_succeeded": int(persisted),
        "other_reasons": dict(other_reasons.most_common(20)),
        "unresolved_classes": [
            {"provider": provider, "reason_class": reason_class, "count": count,
             **({"http_status": status} if status is not None else {})}
            for (provider, reason_class, status), count in sorted(unresolved.items(), key=str)],
        "failure_stages": stages,
    }


def publish(summary):
    line = json.dumps(summary, sort_keys=True)
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write("### ModSync archive cache refresh\n\n```json\n"
                         + json.dumps(summary, indent=2, sort_keys=True) + "\n```\n\n"
                         "Selected due objects are restored individually and size/SHA256/archive verified before "
                         "conditional refresh. Each object and the manifest are persisted before local eviction; "
                         "fresh links are retained for 14 days. No public archive artifact is created.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-items", type=int, required=True, help="due links to attempt (1-500)")
    parser.add_argument("--candidate-id", help="retry exactly this discovered item (64 lowercase hex), forced")
    parser.add_argument("--guide", action="append", help="repeatable full.md path (default both games)")
    parser.add_argument("--work-dir", type=Path, help="parent for the private scratch dir (default $RUNNER_TEMP or /tmp)")
    parser.add_argument("--time-limit", type=int, default=2100, help="seconds before stopping with partial progress")
    args = parser.parse_args(argv)
    if not 1 <= args.max_items <= 500:
        parser.error("--max-items must be 1..500")
    if args.candidate_id and not re.fullmatch(r"[0-9a-f]{64}", args.candidate_id):
        parser.error("--candidate-id must be exactly 64 lowercase hexadecimal characters")

    configured = [bool(os.environ.get(name)) for name in SECRETS]
    if not any(configured):
        print("::notice::Archive cache backend not configured; skipping refresh.")
        return 0
    bucket = os.environ.get("MOD_CACHE_BUCKET", "")
    if not all(configured) or not bucket:
        print("::warning::Archive cache backend is only partly configured; skipping refresh.")
        return 0
    remote = f"MODCACHE:{bucket}/modsync-archives"

    guides = args.guide or ["content/k1/full.md", "content/k2/full.md"]
    items = discover_all(guides)
    if args.candidate_id:
        items = [item for item in items if item["id"] == args.candidate_id]
        if len(items) != 1:
            parser.error("--candidate-id does not match exactly one discovered item")
    try:
        policy = {item["id"]: cache.uncached_reason(item) for item in items}
    except (ValueError, OSError) as error:
        print(f"::error::Archive selections are invalid ({type(error).__name__}).", file=sys.stderr)
        return 2
    policy_entries = [cache.uncached_entry(item, policy[item["id"]]) for item in items if policy[item["id"]]]
    items = [item for item in items if not policy[item["id"]]]

    os.umask(0o077)
    parent = args.work_dir or Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="modsync-cache-", dir=parent))
    root = work / "mod-cache"
    (root / "objects").mkdir(parents=True)
    manifest_path = root / "manifest.json"
    started = time.time()

    def interrupt(signum, frame):
        raise Stop("time limit" if signum == signal.SIGALRM else "terminated")

    try:
        try:
            fetch(f"{remote}/manifest.json", manifest_path, "--max-transfer", "16M", "--cutoff-mode", "hard")
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema": 1, "entries": {}}
            if (manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict)
                    or not isinstance(manifest.setdefault("refresh_attempts", {}), dict)):
                raise ValueError
            for entry in policy_entries:
                manifest["entries"].pop(entry["id"], None)
                manifest["refresh_attempts"].pop(entry["id"], None)
            cache.atomic_json(manifest_path, manifest)
        except (subprocess.SubprocessError, ValueError, OSError) as error:
            print(f"::error::Archive cache backend unreachable or manifest corrupt ({type(error).__name__}).",
                  file=sys.stderr)
            return 1

        if not args.candidate_id:
            attempts, entries_by_id = manifest["refresh_attempts"], manifest["entries"]
            try:
                pins = cache.load_pins()
            except (ValueError, OSError):
                pins = {}

            def priority(item):
                attempted = attempts.get(item["id"], entries_by_id.get(item["id"], {}).get("checked_at", 0))
                return bool(attempted), attempted, item["id"]

            items = sorted((item for item in items if is_due(entries_by_id.get(item["id"], {}), started, item, pins)),
                           key=priority)[:args.max_items]

        entries, ok, stop = list(policy_entries), True, "finished"
        signal.signal(signal.SIGALRM, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        signal.alarm(args.time_limit)
        try:
            for item in items:
                if shutil.disk_usage(root).free < RESERVE:
                    stop = "disk limit; partial progress"
                    break
                item_entries, item_ok = run_item(item, root, remote, bool(args.candidate_id))
                entries += item_entries
                ok = ok and item_ok and all(entry.get("status") in ("fresh", "verified", "pinned", cache.UNCACHED)
                                            for entry in item_entries)
        except Stop as reason:
            stop, ok = f"{reason}; partial progress", False
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            cache.discover = discover_all

        persisted = True
        try:
            rclone("copy", root / "objects", f"{remote}/objects", "--exclude", ".download-*",
                   "--checksum", "--immutable", "--retries", "2", timeout=480)
            if manifest_path.exists():
                rclone("copyto", manifest_path, f"{remote}/manifest.json", "--retries", "2", timeout=120)
        except subprocess.SubprocessError:
            persisted = False
        try:
            final_manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            final_manifest = {}
        publish(totals(entries, final_manifest, started, stop, ok, persisted, args.max_items))
        return 0 if persisted else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
