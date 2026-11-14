#!/usr/bin/env python3
"""Seed the private ModSync archive cache from an operator-controlled staging remote.

Some archives cannot be fetched headlessly (CAPTCHA, login or client-side-decrypted hosts).
An operator hand-downloads them once into a flat rclone folder (``<staging path>/<archive_name>``)
and records a reviewed pin per guide link in seed_pins.json. For each pin this script copies one
file at a time to scratch space, verifies the pinned size and SHA256 plus archive integrity,
uploads ``objects/<sha256>``, records a "pinned" / "operator-seed" manifest entry, and evicts
the local copy. Nothing is trusted that does not match its pin: any mismatch fails the run.

Modes: seed (default), ``--dry-run`` (full download and verification, no cache writes) and
``--verify`` (read-only: pull the manifest with the read credentials and prove every pinned id
restores through cache.restore_object).

Environment:
  SEED_STAGING_RCLONE_CONF_B64   base64 rclone.conf defining the STAGING remote (seed, dry-run)
  RCLONE_CONFIG_MODCACHE_*       cache remote credentials; write pair to seed, read pair to verify
  MOD_CACHE_BUCKET               cache bucket (seed, verify)

Only sanitized totals are printed: no URLs, archive names, remote paths, rclone output or secrets.
Exit codes: 0 success; 1 any pin or infrastructure failure; 2 usage or configuration error.
"""
import argparse
import base64
import binascii
import configparser
import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refresh = load_module("refresh_archives", "refresh_archives.py")
cache = refresh.cache

STAGING_CONF_ENV = "SEED_STAGING_RCLONE_CONF_B64"
STAGING_REMOTE = "STAGING"
DEFAULT_STAGING_PATH = "modsync-seed-staging"
DEFAULT_PINS = HERE / "seed_pins.json"
MAX_CONF_BYTES = 64 * 1024
TRANSFER_TIMEOUT = 3 * 3600
INTEGRITY_TIMEOUT = 900
SHA256 = re.compile(r"[0-9a-f]{64}")
SENSITIVE_KEY = re.compile(r"(secret|password|pass|token|key|credential|private|auth)", re.I)


class SeedError(Exception):
    """A pin or its staged file failed verification; the message is safe to print."""


class Stop(BaseException):
    """Raised from SIGTERM/SIGINT so scratch space and the staging config are still removed."""


def sanitized(error):
    if isinstance(error, SeedError):
        return str(error)
    category = getattr(error, "failure_category", None)
    return type(error).__name__ + (f" ({category})" if category else "")


def load_pins(path):
    """Pins from seed_pins.json via cache.load_pins; any pin cache.py would drop is a hard error."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        pins = cache.load_pins(path)
    except (OSError, ValueError) as error:
        raise ValueError("seed pins file is unreadable or invalid") from error
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError("seed pins file has no entries")
    rejected = sorted(set(entries) - set(pins))
    if rejected:
        raise ValueError(f"{len(rejected)} seed pin(s) rejected by cache.py validation")
    names = {}
    for pin in pins.values():
        hashes = set()
        for each in pin["objects"]:
            if each["size"] <= 0 or each["sha256"] in hashes:
                raise ValueError("invalid or duplicate seed pin object")
            hashes.add(each["sha256"])
            if cache.safe_archive_name(each["archive_name"], each["extension"]) != each["archive_name"]:
                raise ValueError("invalid seed pin archive name")
            owner = names.setdefault(each["archive_name"].casefold(), (each["sha256"], each["size"]))
            if owner != (each["sha256"], each["size"]):
                raise ValueError("seed pins stage different content under one archive name")
    return pins


def staging_config(environ=None):
    """Decode the staging rclone.conf from the environment; returns text or None when unset."""
    encoded = (environ if environ is not None else os.environ).get(STAGING_CONF_ENV, "").strip()
    if not encoded:
        return None
    try:
        text = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{STAGING_CONF_ENV} is not valid base64 UTF-8 text") from error
    if not text.strip() or len(text.encode()) > MAX_CONF_BYTES:
        raise ValueError(f"{STAGING_CONF_ENV} is empty or too large")
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise ValueError(f"{STAGING_CONF_ENV} is not a valid rclone config") from error
    if STAGING_REMOTE not in parser.sections():
        raise ValueError(f"{STAGING_CONF_ENV} does not define a [{STAGING_REMOTE}] remote")
    return text


def mask_config_secrets(text, environ=None):
    """Ask GitHub Actions to mask every credential-looking value in the decoded config."""
    if (environ if environ is not None else os.environ).get("GITHUB_ACTIONS") != "true":
        return
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    for section in parser.sections():
        for key, value in parser.items(section):
            value = value.strip()
            if value and "\n" not in value and SENSITIVE_KEY.search(key):
                print(f"::add-mask::{value}")


def write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


@contextlib.contextmanager
def long_transfers():
    """refresh_archives.rclone defaults to a 10 minute ceiling; staged archives can be multi-GB."""
    original = refresh.rclone

    def patient(*args, timeout=600):
        return original(*args, timeout=max(timeout, TRANSFER_TIMEOUT))

    refresh.rclone = patient
    try:
        yield
    finally:
        refresh.rclone = original


def bind_selection(item, objects):
    """Attach listed_name/row to pin objects when the reviewed guide selection needs several files."""
    selectors = cache.multi_selectors(item)
    if not selectors:
        return len(objects) == 1
    for stored in objects:
        matches = [selector for selector in selectors
                   if cache.safe_archive_name(selector["file"], stored["extension"]) == stored["archive_name"]]
        if len(matches) != 1:
            raise SeedError("pin object cannot be bound to the reviewed multi-file selection")
        stored["listed_name"], stored["row"] = matches[0]["file"], matches[0]["r"]
    return False


def build_entry(item, objects, now):
    """Manifest entry from verified imported objects (single-object form unless a selection needs several)."""
    if bind_selection(item, objects):
        return {**objects[0], "checked_at": now}
    return {"objects": objects, "size": sum(each["size"] for each in objects), "checked_at": now,
            "status": "pinned", "evidence": "operator-seed", "metadata_unknown": True,
            "imported_at": now}


def same_objects(entry, pin):
    wanted = {(each["sha256"], each["size"], each["extension"], each["archive_name"]) for each in pin["objects"]}
    present = {(each.get("sha256"), each.get("size"), each.get("extension"), each.get("archive_name"))
               for each in cache.entry_objects(entry)}
    return wanted == present and len(cache.entry_objects(entry)) == len(pin["objects"])


def remote_size(remote_path):
    """Size in bytes of one remote file, or None when absent."""
    try:
        done = subprocess.run(["rclone", "lsjson", "--files-only", "--no-modtime", "--no-mimetype",
                               remote_path, "--contimeout", "20s", "--timeout", "60s"],
                              check=True, timeout=300, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as error:
        if error.returncode in refresh.MISSING:
            return None
        raise
    listing = json.loads(done.stdout or b"[]")
    return listing[0].get("Size") if len(listing) == 1 else None


def stage_object(pin_object, item, staging, config, root, scratch):
    """Copy one staged file, verify it against its pin and import it to root/objects/<sha256>."""
    size = pin_object["size"]
    if 2 * size + refresh.RESERVE >= shutil.disk_usage(root).free:
        raise SeedError("insufficient disk for staged object")
    scratch.mkdir(parents=True, exist_ok=True)
    source = scratch / pin_object["archive_name"]
    source.unlink(missing_ok=True)
    try:
        try:
            refresh.rclone("copyto", f"{staging}/{pin_object['archive_name']}", source,
                           "--config", config, "--retries", "2", timeout=TRANSFER_TIMEOUT)
        except subprocess.CalledProcessError as error:
            if error.returncode in refresh.MISSING:
                raise SeedError("staged file missing") from error
            raise
        if not source.is_file():
            raise SeedError("staged file missing")
        if source.stat().st_size != size:
            raise SeedError("staged file size does not match its pin")
        args = types.SimpleNamespace(max_bytes=size, integrity_timeout=INTEGRITY_TIMEOUT)
        loose = pin_object["extension"] == cache.LOOSE
        if loose and not cache.reviewed_loose(item):
            raise SeedError("loose pin is not a reviewed loose selection")
        outcome = cache.import_object(item, pin_object["archive_name"], source, loose, root, args,
                                      None, False, pin_object["sha256"])
        if outcome[0] == "pin-mismatch":
            raise SeedError("staged file sha256 does not match its pin")
        if outcome[0] != "imported":
            raise SeedError("staged file could not be imported")
        stored = outcome[1]
        if stored["extension"] != pin_object["extension"] or stored["status"] != "pinned" \
                or stored["evidence"] != "operator-seed":
            raise SeedError("staged archive type does not match its pin")
        return cache.object_path(root, stored), stored
    finally:
        source.unlink(missing_ok=True)


def evict(root):
    objects = root / "objects"
    if objects.is_dir():
        for path in objects.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink()


def seed_pin(identity, pin, item, root, staging, config, remote, pins, manifest, manifest_path,
             dry_run, force, now):
    """Returns the outcome label for one pin; raises on any failure."""
    if not dry_run and not force and cache.is_pinned(item, manifest["entries"].get(identity), pins) and all(
            remote_size(f"{remote}/objects/{each['sha256']}") == each["size"] for each in pin["objects"]):
        return "already-seeded"
    stored_objects = []
    for pin_object in pin["objects"]:
        path, stored = stage_object(pin_object, item, staging, config, root, root.parent / "stage")
        try:
            if not dry_run:
                refresh.upload_object(path, f"{remote}/objects/{path.name}")
        finally:
            path.unlink(missing_ok=True)
        stored_objects.append(stored)
    entry = build_entry(item, stored_objects, now)
    if not cache.is_pinned(item, entry, pins) or not cache.objects_match_selection(item, entry):
        raise SeedError("imported entry is not recognized as pinned by cache.py")
    if dry_run:
        return "verified"
    manifest["entries"][identity] = entry
    cache.atomic_json(manifest_path, manifest)
    refresh.rclone("copyto", manifest_path, f"{remote}/manifest.json", "--retries", "1", timeout=300)
    return "seeded"


def load_manifest(remote, manifest_path, extra=()):
    if not refresh.fetch(f"{remote}/manifest.json", manifest_path, *extra, "--max-transfer", "16M",
                         "--cutoff-mode", "hard"):
        return {"schema": 1, "entries": {}}, False
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict):
        raise ValueError("unsupported manifest schema")
    return manifest, True


def verify_pin(identity, pin, item, root, remote, pins, entry, destination):
    """Read-only proof that a pinned id is in the manifest and restores like an install would."""
    if not cache.is_pinned(item, entry, pins):
        raise SeedError("manifest entry missing or not recognized as pinned")
    if not same_objects(entry, pin):
        raise SeedError("manifest entry does not match its pin")
    args = types.SimpleNamespace(destination=destination, integrity_timeout=INTEGRITY_TIMEOUT)
    restored, taken, created = {}, set(), []
    try:
        for stored in cache.entry_objects(entry):
            pin_object = next(each for each in pin["objects"] if each["sha256"] == stored["sha256"])
            if pin_object["size"] + refresh.RESERVE >= shutil.disk_usage(root).free:
                raise SeedError("insufficient disk for verification")
            path = cache.object_path(root, stored)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if not refresh.fetch(f"{remote}/objects/{path.name}", path, "--retries", "2"):
                    raise SeedError("pinned object missing from the cache")
                if not refresh.matches(path, pin_object["sha256"], pin_object["size"]):
                    raise SeedError("cached object does not match its pin")
                try:
                    cache.restore_object(item, stored, root, args, restored, taken, created)
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    raise SeedError("cached object does not restore (" + type(error).__name__ + ")") from error
            finally:
                path.unlink(missing_ok=True)
    finally:
        for path in created:
            path.unlink(missing_ok=True)
    return "restores"


def publish(summary, results):
    print(json.dumps(summary, sort_keys=True))
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        lines = ["### ModSync archive cache seed", "", "```json", json.dumps(summary, indent=2, sort_keys=True), "```", ""]
        lines += [f"- `{identity[:12]}` {outcome}" for identity, outcome in results]
        with open(target, "a") as stream:
            stream.write("\n".join(lines) + "\n")


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="download and fully verify every pin from staging; write nothing to the cache")
    mode.add_argument("--verify", action="store_true",
                      help="read-only: check every pinned id in the cache manifest and restores")
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS, help="seed pins file")
    parser.add_argument("--guide", action="append", help="repeatable full.md path (default both games)")
    parser.add_argument("--only", action="append", default=[], help="repeatable pin id (64 lowercase hex)")
    parser.add_argument("--staging-path", default=DEFAULT_STAGING_PATH, help="flat folder inside the STAGING remote")
    parser.add_argument("--force", action="store_true", help="reseed pins already present in the cache")
    parser.add_argument("--work-dir", type=Path, help="parent for the private scratch dir (default $RUNNER_TEMP or /tmp)")
    args = parser.parse_args(argv)
    for each in args.only:
        if not SHA256.fullmatch(each):
            parser.error("--only must be exactly 64 lowercase hexadecimal characters")
    if args.force and (args.dry_run or args.verify):
        parser.error("--force only applies to seeding")
    return args


def main(argv=None):
    args = parse(argv)
    mode = "dry-run" if args.dry_run else "verify" if args.verify else "seed"
    try:
        pins = load_pins(args.pins)
        config_text = None if args.verify else staging_config()
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    unknown = [each for each in args.only if each not in pins]
    if unknown:
        print("::error::--only names a pin id that is not in the seed pins file.", file=sys.stderr)
        return 2
    if not args.verify and config_text is None:
        print(f"::error::{STAGING_CONF_ENV} is not set; the staging remote is required to {mode}.", file=sys.stderr)
        return 2
    bucket = os.environ.get("MOD_CACHE_BUCKET", "")
    if not args.dry_run:
        needed = ("RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID", "RCLONE_CONFIG_MODCACHE_SECRET_ACCESS_KEY",
                  "RCLONE_CONFIG_MODCACHE_ENDPOINT")
        if not bucket or not all(os.environ.get(name) for name in needed):
            print("::error::The archive cache remote is not fully configured.", file=sys.stderr)
            return 2
    selected = {identity: pin for identity, pin in pins.items() if not args.only or identity in args.only}

    guides = args.guide or ["content/k1/full.md", "content/k2/full.md"]
    items = {item["id"]: item for item in cache.discover(guides)}
    drifted = [identity for identity, pin in selected.items()
               if identity not in items or items[identity]["url"] != pin["url"]]
    if drifted:
        print(f"::error::{len(drifted)} pinned id(s) are not linked from the guides; pins and guides have drifted.",
              file=sys.stderr)
        return 1

    if config_text is not None:
        mask_config_secrets(config_text)
    os.umask(0o077)
    parent = args.work_dir or Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="modsync-seed-", dir=parent))
    root = work / "mod-cache"
    (root / "objects").mkdir(parents=True)
    manifest_path = root / "manifest.json"
    remote = f"MODCACHE:{bucket}/modsync-archives"
    staging = f"{STAGING_REMOTE}:{args.staging_path.strip('/')}"
    config = None

    def interrupt(signum, frame):
        raise Stop("terminated")

    previous = {signum: signal.signal(signum, interrupt) for signum in (signal.SIGTERM, signal.SIGINT)}
    results, failures, started = [], [], time.time()
    try:
        if config_text is not None:
            config = write_private(work / "staging.rclone.conf", config_text)
            del config_text
        manifest = {"schema": 1, "entries": {}}
        if not args.dry_run:
            try:
                manifest, existed = load_manifest(remote, manifest_path, ("--retries", "2"))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"::error::Archive cache backend unreachable or manifest corrupt ({type(error).__name__}).",
                      file=sys.stderr)
                return 1
            if args.verify and not existed:
                print("::error::The cache has no manifest to verify.", file=sys.stderr)
                return 1
            cache.atomic_json(manifest_path, manifest)
        destination = work / "restored"
        with long_transfers():
            for identity, pin in selected.items():
                outcome = "failed"
                try:
                    if args.verify:
                        outcome = verify_pin(identity, pin, items[identity], root, remote, pins,
                                             manifest["entries"].get(identity), destination)
                    else:
                        outcome = seed_pin(identity, pin, items[identity], root, staging, config, remote, pins,
                                           manifest, manifest_path, args.dry_run, args.force, time.time())
                except Stop:
                    raise
                except Exception as error:
                    outcome = "FAILED: " + sanitized(error)
                    failures.append(identity)
                finally:
                    evict(root)
                    shutil.rmtree(destination, ignore_errors=True)
                results.append((identity, outcome))
                print(f"{mode} {identity[:12]} {outcome}", flush=True)
    except Stop:
        failures.append("interrupted")
        results.append(("interrupted", "FAILED: terminated"))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        shutil.rmtree(work, ignore_errors=True)
    completed = len(results) == len(selected) and "interrupted" not in failures
    summary = {"mode": mode, "pins": len(selected), "processed": len(results),
               "ok": sum(1 for _, outcome in results if not outcome.startswith("FAILED")),
               "failed": len(failures), "already_seeded": sum(1 for _, outcome in results if outcome == "already-seeded"),
               "complete": int(completed and not failures), "seconds": round(time.time() - started)}
    publish(summary, results)
    if failures:
        print(f"::error::{len(failures)} pin(s) failed {mode}; no mismatched file was published.", file=sys.stderr)
        return 1
    return 0 if completed else 1


if __name__ == "__main__":
    sys.exit(main())
