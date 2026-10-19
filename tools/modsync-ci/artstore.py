#!/usr/bin/env python3
"""Artifact-store tier: stage sealed blobs for upload, restore them by SHA-256, refresh and prune.

Packs are GitHub Actions artifacts holding ``<stored_sha256>.part`` members; index fragments are
tiny artifacts holding ``index.json``.  Pack entries only learn their artifact id and expiry from
the artifact listing, so ``load_index`` reconciles every fragment against that listing before
merging.  Every library entry point degrades to "store unavailable" when no token is configured.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import artstore_client as client_mod  # noqa: E402
import artstore_core as core  # noqa: E402

__all__ = [
    "fetch",
    "load_index",
    "stage",
    "refresh_plan",
    "prune",
    "main",
]

KEY_ENV = core.KEY_ENV
PART_SIZE_DEFAULT = core.PART_SIZE_DEFAULT
PACK_BUDGET_DEFAULT = 10 * 1024 ** 3
MIN_DAYS_LEFT = 30
REFRESH_WITHIN_DAYS = 21
INDEX_FETCH_LIMIT = 3
INDEX_KEEP = 5
PRUNE_GRACE_HOURS = 24
DEFAULT_WORKERS = 4
SEAL_OVERHEAD = 128
MAX_INDEX_BYTES = 256 * 1024 * 1024
INDEX_MEMBER = "index.json"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISS = 3
EXIT_UNAVAILABLE = 4

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SIGNED_RE = re.compile(r"(?i)\b(sig|signature|token|access_token|key)=[^&\s\"']+")
_SECRET_ENVS = {"ARTSTORE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", KEY_ENV}


def _redact(text):
    for name in _SECRET_ENVS:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, "***")
    return _SIGNED_RE.sub(r"\1=***", text)


def _log(level, message):
    text = _redact(" ".join(str(message).split()))[:300]
    print("::%s::artstore: %s" % (level, text), file=sys.stderr, flush=True)


def _describe(exc):
    return "%s: %s" % (type(exc).__name__, exc)


def _now(value=None):
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _epoch(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _make_client():
    return client_mod.from_environment()


def _key_configured(key_env):
    return len(os.environ.get(key_env, "").encode()) >= 32


def _is_dead(artifact, now_epoch):
    if artifact.get("expired"):
        return True
    expires = _epoch(artifact.get("expires_at"))
    return expires is not None and expires <= now_epoch


def _newest_first(artifacts):
    return sorted(artifacts, key=lambda item: (item.get("created_at") or "", item.get("id") or 0),
                  reverse=True)


def _packs_by_name(listing, now_epoch):
    chosen = {}
    for artifact in _newest_first(listing):
        name = str(artifact.get("name", ""))
        if not name.startswith(core.PACK_PREFIX):
            continue
        current = chosen.get(name)
        if current is None or (_is_dead(current, now_epoch) and not _is_dead(artifact, now_epoch)):
            chosen[name] = artifact
    return chosen


def _reconcile(index, packs_by_name, now_epoch):
    for name in list(index["packs"]):
        artifact = packs_by_name.get(name)
        if artifact is None:
            core.set_pack_artifact(index, name, None, 0)
            continue
        expires = _epoch(artifact.get("expires_at"))
        if _is_dead(artifact, now_epoch):
            core.set_pack_artifact(index, name, None,
                                   min(expires if expires is not None else 0, now_epoch))
        else:
            core.set_pack_artifact(index, name, artifact.get("id"), expires)
    return index


def _read_fragment(client, artifact, scratch):
    destination = os.path.join(scratch, "index-%d.json" % int(artifact["id"]))
    client.read_member(artifact["id"], INDEX_MEMBER, destination)
    if os.path.getsize(destination) > MAX_INDEX_BYTES:
        raise ValueError("Index fragment too large")
    return core.load_index(destination)


def _index_from_listing(client, listing, keep, now_epoch):
    fragments = _newest_first([item for item in listing
                               if str(item.get("name", "")).startswith(core.INDEX_PREFIX)
                               and not _is_dead(item, now_epoch)])
    if not fragments:
        return core.new_index()
    packs = _packs_by_name(listing, now_epoch)
    scratch = tempfile.mkdtemp(prefix=".artstore-index-")
    loaded = []
    try:
        for artifact in fragments:
            try:
                loaded.append(_reconcile(_read_fragment(client, artifact, scratch), packs,
                                         now_epoch))
            except (client_mod.ArtifactError, ValueError, OSError) as exc:
                _log("warning", "index artifact %s unreadable (%s)" % (artifact.get("id"),
                                                                       _describe(exc)))
                continue
            if len(loaded) >= keep:
                break
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not loaded:
        raise client_mod.ArtifactError("no readable index artifact in the store")
    return core.merge_indexes(loaded, now_epoch)


def load_index(client=None, *, keep=INDEX_FETCH_LIMIT, now=None):
    """Merged, listing-reconciled index; empty when the store is unavailable or has no index.

    Raises ArtifactError when the store is reachable but the listing/index cannot be read.
    """
    now_epoch = _now(now)
    client = client if client is not None else _make_client()
    if client is None:
        return core.new_index()
    listing = client.list_artifacts(include_expired=True)
    return _index_from_listing(client, listing, max(1, int(keep)), now_epoch)


def _pull_part(client, part, entry, scratch, key_env):
    number = part["n"]
    sealed = scratch / ("%d.part" % number)
    client.read_member(entry["artifact_id"], part["stored_sha256"] + core.PART_SUFFIX, sealed,
                       expect_size=part["stored_size"])
    digest = hashlib.sha256()
    with sealed.open("rb") as handle:
        for chunk in iter(lambda: handle.read(core.CHUNK), b""):
            digest.update(chunk)
    if digest.hexdigest() != part["stored_sha256"]:
        raise ValueError("Stored part digest mismatch")
    plain = scratch / ("%d.plain" % number)
    core.open_part(sealed, plain, key_env)
    sealed.unlink()
    return plain


def _fetch(digest, dest, client, index, workdir, key_env, workers):
    if not _SHA_RE.match(digest):
        _log("warning", "fetch refused: not a SHA-256 hex digest")
        return False
    _SECRET_ENVS.add(key_env)
    if not _key_configured(key_env):
        _log("warning", "fetch %s skipped: %s is not configured" % (digest[:12], key_env))
        return False
    client = client if client is not None else _make_client()
    if client is None:
        _log("notice", "store unavailable (no token); %s not fetched" % digest[:12])
        return False
    if index is None:
        index = load_index(client)
    record = core.lookup(index, digest)
    if record is None:
        _log("notice", "%s is not in the artifact store" % digest[:12])
        return False
    parts = sorted(record["parts"], key=lambda part: part["n"])
    now = time.time()
    entries = []
    for part in parts:
        entry = index["packs"].get(part["pack"])
        if entry is None or entry.get("artifact_id") is None:
            _log("warning", "fetch %s: pack %s expired or missing" % (digest[:12], part["pack"]))
            return False
        expires = entry.get("expires_at")
        if expires is not None and expires <= now:
            _log("warning", "fetch %s: pack %s expired" % (digest[:12], part["pack"]))
            return False
        entries.append(entry)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base = Path(workdir) if workdir else dest.parent
    base.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".artstore-fetch-", dir=base))
    try:
        plain = []
        if parts:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(parts))))
            with pool:
                futures = [pool.submit(_pull_part, client, part, entry, scratch, key_env)
                           for part, entry in zip(parts, entries)]
                try:
                    plain = [future.result() for future in futures]
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
        core.join_parts(plain, dest, digest, record["size"], key_env=key_env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return True


def fetch(sha256, dest, *, client=None, index=None, workdir=None, key_env=KEY_ENV,
          workers=DEFAULT_WORKERS):
    """Restore the plaintext blob with this SHA-256 to ``dest``; False on any miss, never raises."""
    digest = str(sha256).strip().lower()
    try:
        return _fetch(digest, dest, client, index, workdir, key_env, workers)
    except Exception as exc:
        _log("warning", "fetch %s failed: %s" % (digest[:12], _describe(exc)))
        return False


def _run_tag():
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if run_id:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "").strip() or "1"
        tag = "%s-%s" % (run_id, attempt)
    else:
        tag = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    suffix = os.environ.get("ARTSTORE_NAME_SUFFIX", "").strip()
    if suffix:
        tag += "-" + suffix
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tag)


def _is_live(index, sha256, now_epoch, min_days_left):
    record = index["blobs"].get(sha256)
    if record is None:
        return False
    threshold = now_epoch + min_days_left * core.DAY_SECONDS
    for part in record["parts"]:
        entry = index["packs"].get(part["pack"])
        if entry is None:
            return False
        expires = entry.get("expires_at")
        if expires is not None and expires <= threshold:
            return False
    return True


def _pairs(paths_and_sha256):
    items = paths_and_sha256.items() if hasattr(paths_and_sha256, "items") else paths_and_sha256
    for path, sha256 in items:
        yield path, sha256


def stage(paths_and_sha256, packdir, part_size=PART_SIZE_DEFAULT, index=None, *,
          pack_budget=PACK_BUDGET_DEFAULT, key_env=KEY_ENV, min_days_left=MIN_DAYS_LEFT,
          now=None):
    """Seal blobs into ``packdir/<pack_name>/`` dirs plus a cumulative ``packdir/index/index.json``."""
    now_epoch = _now(now)
    _SECRET_ENVS.add(key_env)
    if not _key_configured(key_env):
        raise ValueError("%s must contain at least 32 bytes" % key_env)
    packdir = Path(packdir)
    packdir.mkdir(parents=True, exist_ok=True)
    base = copy.deepcopy(index) if index is not None else core.new_index()
    core.drop_expired(base, now_epoch)
    fragment_path = packdir / "index" / INDEX_MEMBER
    if fragment_path.is_file():
        base = core.merge_indexes([core.load_index(fragment_path), base])
    tag = _run_tag()
    counter = 0
    skipped = []
    seen = set()
    packs = []
    current = None

    def skip(path, sha256, reason):
        skipped.append({"path": str(path), "sha256": sha256, "reason": reason})

    for path, claimed in _pairs(paths_and_sha256):
        sha256 = str(claimed).strip().lower()
        source = Path(path)
        if not _SHA_RE.match(sha256):
            skip(path, sha256, "bad-sha256")
            continue
        if sha256 in seen:
            skip(path, sha256, "duplicate")
            continue
        seen.add(sha256)
        if source.suffix.lower() == ".bik":
            skip(path, sha256, "bik")
            continue
        if not source.is_file():
            skip(path, sha256, "missing")
            continue
        if _is_live(base, sha256, now_epoch, min_days_left):
            skip(path, sha256, "present")
            continue
        estimate = source.stat().st_size + SEAL_OVERHEAD * (source.stat().st_size // part_size + 1)
        if current is None or (current["blobs"] and current["bytes"] + estimate > pack_budget):
            while ((packdir / ("%s%s-%d" % (core.PACK_PREFIX, tag, counter))).exists()
                   or ("%s%s-%d" % (core.PACK_PREFIX, tag, counter)) in base["packs"]):
                counter += 1
            name = "%s%s-%d" % (core.PACK_PREFIX, tag, counter)
            counter += 1
            current = {"name": name, "dir": packdir / name, "bytes": 0, "blobs": [], "n": counter - 1}
            current["dir"].mkdir(parents=True)
            packs.append(current)
        before = set(os.listdir(current["dir"]))
        try:
            record = core.split_and_seal(source, current["dir"], part_size, key_env)
        except (ValueError, OSError) as exc:
            for leftover in set(os.listdir(current["dir"])) - before:
                (current["dir"] / leftover).unlink(missing_ok=True)
            skip(path, sha256, "seal-failed: %s" % _redact(str(exc))[:80])
            continue
        if record["sha256"] != sha256:
            for part in record["parts"]:
                (current["dir"] / (part["stored_sha256"] + core.PART_SUFFIX)).unlink(missing_ok=True)
            skip(path, sha256, "sha256-mismatch")
            continue
        current["blobs"].append(record)
        current["bytes"] += sum(part["stored_size"] for part in record["parts"])
    staged = []
    kept = []
    for pack in packs:
        if not pack["blobs"]:
            shutil.rmtree(pack["dir"], ignore_errors=True)
            continue
        members = {part["stored_sha256"]: part["stored_sha256"] + core.PART_SUFFIX
                   for blob in pack["blobs"] for part in blob["parts"]}
        core.add_pack(base, pack["name"], pack["blobs"], members, created_at=int(now_epoch))
        kept.append(pack)
        staged.extend(blob["sha256"] for blob in pack["blobs"])
    result = {"blobs": [copy.deepcopy(base["blobs"][sha256]) for sha256 in staged],
              "pack_name": kept[0]["name"] if kept else None,
              "packs": [{"name": pack["name"], "dir": str(pack["dir"]), "size": pack["bytes"],
                         "blobs": len(pack["blobs"])} for pack in kept],
              "index_name": None, "index_path": None, "skipped": skipped}
    if kept:
        core.dump_index(base, fragment_path)
        result["index_name"] = "%s%s-%d" % (core.INDEX_PREFIX, tag, kept[0]["n"])
        result["index_path"] = str(fragment_path)
    return result


def refresh_plan(index, now, within_days=REFRESH_WITHIN_DAYS):
    """SHA-256s whose retention ends within ``within_days`` (or whose pack is unknown)."""
    return core.blobs_needing_refresh(index, _now(now), within_days)


def _prune_plan(listing, index, keep_days_over, keep_indexes, grace_hours, now_epoch):
    referenced = {part["pack"] for record in index["blobs"].values() for part in record["parts"]}
    plan = []
    notes = []
    kept = 0

    def add(artifact, kind, reason):
        plan.append({"id": artifact.get("id"), "name": artifact.get("name"), "kind": kind,
                     "reason": reason, "size": artifact.get("size_in_bytes")})

    packs = [item for item in listing if str(item.get("name", "")).startswith(core.PACK_PREFIX)]
    guarded = not referenced and any(not _is_dead(item, now_epoch) for item in packs)
    if guarded:
        notes.append("index references no packs; live packs left untouched")
    for artifact in _newest_first(packs):
        if _is_dead(artifact, now_epoch):
            add(artifact, "pack", "expired")
            continue
        if artifact.get("name") in referenced or guarded:
            kept += 1
            continue
        created = _epoch(artifact.get("created_at"))
        if created is not None and now_epoch - created < grace_hours * 3600:
            kept += 1
            continue
        expires = _epoch(artifact.get("expires_at"))
        if (keep_days_over is not None and expires is not None
                and (expires - now_epoch) / core.DAY_SECONDS > keep_days_over):
            kept += 1
            continue
        add(artifact, "pack", "superseded")
    indexes = _newest_first([item for item in listing
                             if str(item.get("name", "")).startswith(core.INDEX_PREFIX)])
    alive = 0
    for artifact in indexes:
        if _is_dead(artifact, now_epoch):
            add(artifact, "index", "expired")
        elif alive < keep_indexes:
            alive += 1
            kept += 1
        else:
            add(artifact, "index", "superseded")
    return plan, kept, notes


def prune(client, index, keep_days_over=None, *, dry_run=True, keep_indexes=INDEX_KEEP,
          grace_hours=PRUNE_GRACE_HOURS, now=None):
    """Delete expired or superseded pack/index artifacts (needs ``actions: write``).

    A pack is superseded when no blob in ``index`` references it; packs younger than
    ``grace_hours`` (upload may precede its index) and packs with more than ``keep_days_over``
    days of retention left are kept.  Only the newest ``keep_indexes`` index artifacts survive.
    """
    now_epoch = _now(now)
    listing = client.list_artifacts(include_expired=True)
    plan, kept, notes = _prune_plan(listing, index, keep_days_over, keep_indexes, grace_hours,
                                    now_epoch)
    result = {"dry_run": bool(dry_run), "delete": plan, "deleted": [], "failed": [],
              "kept": kept, "notes": notes}
    if dry_run:
        return result
    for item in plan:
        try:
            client.delete_artifact(item["id"])
        except client_mod.ArtifactError as exc:
            result["failed"].append({"id": item["id"], "error": _redact(_describe(exc))[:200]})
        else:
            result["deleted"].append(item["id"])
    return result


def _human(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024


def _days_left(index, record, now_epoch):
    remaining = []
    for name in {part["pack"] for part in record["parts"]}:
        entry = index["packs"].get(name)
        if entry is None:
            return "expired"
        expires = entry.get("expires_at")
        if expires is not None:
            remaining.append((expires - now_epoch) / core.DAY_SECONDS)
    if not remaining:
        return "?"
    low = min(remaining)
    return "expired" if low <= 0 else "%.1f" % low


def _table(index, now_epoch):
    rows = [("SHA256", "SIZE", "PARTS", "PACKS", "DAYS_LEFT")]
    for sha256 in sorted(index["blobs"]):
        record = index["blobs"][sha256]
        rows.append((sha256, _human(record["size"]), str(len(record["parts"])),
                     str(len({part["pack"] for part in record["parts"]})),
                     _days_left(index, record, now_epoch)))
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    lines = ["  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip()
             for row in rows]
    total = sum(record["size"] for record in index["blobs"].values())
    lines.append("%d blobs, %s, %d packs" % (len(index["blobs"]), _human(total),
                                              len(index["packs"])))
    return "\n".join(lines)


def _read_manifest(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON list of {path, sha256}")
    pairs = []
    for entry in data:
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            raise ValueError("manifest entries need path and sha256")
        pairs.append((entry["path"], entry["sha256"]))
    return pairs


def _require_client():
    client = _make_client()
    if client is None:
        _log("notice", "store unavailable (no token or repository configured)")
    return client


def _command_fetch(args):
    client = _make_client()
    ok = fetch(args.sha256, args.dest, client=client, workdir=args.workdir, key_env=args.key_env,
               workers=args.workers)
    print("hit" if ok else "miss")
    return EXIT_OK if ok else EXIT_MISS


def _command_stage(args):
    client = _require_client()
    if client is None:
        return EXIT_UNAVAILABLE
    pairs = _read_manifest(args.manifest_json)
    index = load_index(client)
    result = stage(pairs, args.packdir, args.part_size, index, pack_budget=args.pack_budget,
                   key_env=args.key_env)
    summary = {key: result[key] for key in ("pack_name", "packs", "index_name", "index_path",
                                            "skipped")}
    summary["staged"] = [blob["sha256"] for blob in result["blobs"]]
    print(json.dumps(summary, indent=1, sort_keys=True))
    return EXIT_OK


def _command_list(args):
    client = _require_client()
    if client is None:
        return EXIT_UNAVAILABLE
    print(_table(load_index(client), time.time()))
    return EXIT_OK


def _command_refresh_plan(args):
    client = _require_client()
    if client is None:
        return EXIT_UNAVAILABLE
    for sha256 in refresh_plan(load_index(client), time.time(), args.within_days):
        print(sha256)
    return EXIT_OK


def _command_prune(args):
    client = _require_client()
    if client is None:
        return EXIT_UNAVAILABLE
    result = prune(client, load_index(client), args.keep_days_over, dry_run=not args.apply,
                   keep_indexes=args.keep_indexes, grace_hours=args.grace_hours)
    verb = "DRY-RUN delete" if result["dry_run"] else "delete"
    for item in result["delete"]:
        print("%s %s %s id=%s reason=%s" % (verb, item["kind"], item["name"], item["id"],
                                            item["reason"]))
    for note in result["notes"]:
        print("note: %s" % note)
    print("%d to delete, %d kept, %d deleted, %d failed" % (
        len(result["delete"]), result["kept"], len(result["deleted"]), len(result["failed"])))
    for failure in result["failed"]:
        _log("warning", "delete %s failed: %s" % (failure["id"], failure["error"]))
    return EXIT_ERROR if result["failed"] else EXIT_OK


def _parser():
    parser = argparse.ArgumentParser(prog="artstore.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("fetch", help="restore a blob (exit 0 hit, 3 miss)")
    sub.add_argument("--sha256", required=True)
    sub.add_argument("--dest", required=True)
    sub.add_argument("--workdir")
    sub.add_argument("--key-env", default=KEY_ENV)
    sub.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    sub.set_defaults(handler=_command_fetch)
    sub = commands.add_parser("stage", help="seal blobs from a manifest into packdir")
    sub.add_argument("--manifest-json", required=True)
    sub.add_argument("--packdir", required=True)
    sub.add_argument("--part-size", type=int, default=PART_SIZE_DEFAULT)
    sub.add_argument("--pack-budget", type=int, default=PACK_BUDGET_DEFAULT)
    sub.add_argument("--key-env", default=KEY_ENV)
    sub.set_defaults(handler=_command_stage)
    sub = commands.add_parser("list", help="table of stored blobs")
    sub.set_defaults(handler=_command_list)
    sub = commands.add_parser("refresh-plan", help="SHA-256s nearing expiry")
    sub.add_argument("--within-days", type=int, default=REFRESH_WITHIN_DAYS)
    sub.set_defaults(handler=_command_refresh_plan)
    sub = commands.add_parser("prune", help="delete expired/superseded artifacts")
    sub.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    sub.add_argument("--keep-days-over", type=float, default=None)
    sub.add_argument("--keep-indexes", type=int, default=INDEX_KEEP)
    sub.add_argument("--grace-hours", type=float, default=PRUNE_GRACE_HOURS)
    sub.set_defaults(handler=_command_prune)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (client_mod.ArtifactError, ValueError, OSError) as exc:
        _log("error", "%s failed: %s" % (args.command, _describe(exc)))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
