#!/usr/bin/env python3
"""Thin repository-local adapters for existing archive and install tools."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import cache
import gui_diag
import fullrun_contract as contract

HERE = Path(__file__).resolve().parent


def checked(command, log, env=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False, env=env)
    if result.returncode:
        raise ValueError("stage failed; inspect its retained encrypted evidence")
    return result


def installer_environment():
    return {name: value for name, value in cache.scrubbed_env().items()
            if not gui_diag.SECRET_ENV_NAME.search(name)
            and not name.startswith(("SSH_", "GIT_CONFIG_"))}


def selected_seed_ids(root, games):
    import seed_archives
    pins = seed_archives.load_pins(seed_archives.DEFAULT_PINS)
    paths = guides(root, games)[1::2]
    selected = sorted(item["id"] for item in cache.discover(paths) if item["id"] in pins)
    if not selected:
        raise ValueError("no reviewed seed pins match the requested guide scope")
    return selected


def guides(root, games):
    result = []
    for game in games:
        if game not in ("k1", "k2"):
            raise ValueError("invalid game")
        path = Path(root) / "content" / game / "full.md"
        if path.is_symlink() or not path.is_file():
            raise ValueError("pinned guide file is unavailable")
        result.extend(["--guide", str(path)])
    return result


def remote_options():
    if os.environ.get("RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID") and os.environ.get("MOD_CACHE_BUCKET"):
        bucket = os.environ["MOD_CACHE_BUCKET"]
        if not bucket or any(c in bucket for c in "/\\:\r\n"):
            raise ValueError("invalid cache bucket name")
        return ["--remote", "MODCACHE:" + bucket + "/modsync-archives"]
    return []


def cache_key(path):
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in ("checked_at", "imported_at")}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    document = json.loads(Path(path).read_text())
    canonical = json.dumps(stable(document), sort_keys=True, separators=(",", ":")).encode()
    return "mod-archives-" + hashlib.sha256(canonical).hexdigest()[:16]


def identity(engine, guide_root, engine_sha, guide_sha, output):
    actual_engine = subprocess.check_output(["git", "-C", str(engine), "rev-parse", "HEAD"], text=True).strip()
    actual_guide = subprocess.check_output(["git", "-C", str(guide_root), "rev-parse", "HEAD"], text=True).strip()
    if actual_engine != contract.exact_sha(engine_sha) or actual_guide != contract.exact_sha(guide_sha):
        raise ValueError("checked-out engine or guide does not match the admitted immutable revision")
    contract.write_json(output, {"schema_version": 1, "engine_sha": actual_engine,
                                "guide_sha": actual_guide, "execution_repository": os.environ.get("GITHUB_REPOSITORY"),
                                "execution_sha": os.environ.get("GITHUB_SHA"),
                                "artifact_repository": os.environ.get("ARTSTORE_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY"),
                                "plan_id": os.environ.get("PLAN_ID"), "run_id": os.environ.get("GITHUB_RUN_ID"),
                                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"), "cross_lane_parity": "unverified"})


def install(args):
    temp = Path(args.output)
    temp.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    options = ["--allow-uncached-by-policy", "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching"]
    cutscene = ("Optional Widescreen & Extended Widescreen Support Mods::Remastered Cutscenes" if args.game == "k1"
                else "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled")
    options += ["--allow-uncached-by-policy", cutscene]
    checked([sys.executable, str(HERE / "cache.py"), "restore", *guides(args.guides, [args.game]),
             "--cache-dir", args.cache, "--destination", str(work / "archives"),
             "--archive-map", str(temp / "archive-map.json"), "--report", str(temp / "restore.json"),
             "--deselect-unavailable", *options, *remote_options()], temp / "restore-private.log")
    checked([sys.executable, str(HERE / "ports.py"), "--implementation", "rust", "--mode", args.mode,
             "--source", args.engine, "--guide", str(Path(args.guides) / "content" / args.game / "full.md"),
             "--game-dir", args.game_dir, "--archives", str(work / "archives"),
             "--archive-map", str(temp / "archive-map.json"), "--restore-report", str(temp / "restore.json"),
             "--work-dir", str(work / "work"), "--report", str(temp / "report.json")], temp / "install-private.log",
            env=installer_environment())
    digest = hashlib.sha256((Path(args.guides) / "content" / args.game / "full.md").read_bytes()).hexdigest()
    result = contract.require_complete(json.loads((temp / "report.json").read_text()), args.mode, digest)
    contract.write_json(temp / "completion.json", result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "seed", "install", "identity"):
        child = sub.add_parser(name)
        child.add_argument("--guides", required=True)
        child.add_argument("--output", required=True)
        if name in ("refresh", "seed"):
            child.add_argument("--games", default='["k1","k2"]')
        if name == "refresh":
            child.add_argument("--cache", required=True)
        if name == "seed":
            child.add_argument("--dry-run", action="store_true")
        if name in ("identity", "install"):
            child.add_argument("--engine", required=True)
        if name == "identity":
            child.add_argument("--engine-sha", required=True)
            child.add_argument("--guide-sha", required=True)
        if name == "install":
            child.add_argument("--cache", required=True)
            child.add_argument("--work-dir", required=True)
            child.add_argument("--game-dir", required=True)
            child.add_argument("--game", choices=("k1", "k2"), required=True)
            child.add_argument("--mode", choices=("cli", "gui"), required=True)
    args = parser.parse_args(argv)
    old = os.umask(0o077)
    try:
        if args.command == "identity":
            identity(args.engine, args.guides, args.engine_sha, args.guide_sha, args.output)
        elif args.command == "install":
            install(args)
        else:
            requested_games = json.loads(args.games)
            if not isinstance(requested_games, list) or not requested_games:
                raise ValueError("games must be a nonempty list")
            if args.command == "refresh":
                checked([sys.executable, str(HERE / "cache.py"), "refresh", *guides(args.guides, requested_games),
                         "--cache-dir", args.cache, "--cache-limit-bytes", "2684354560", *remote_options()],
                        Path(args.output) / "refresh-private.log")
                key = cache_key(Path(args.cache) / "manifest.json")
                contract.emit_outputs({"archive_cache_key": key})
                contract.write_json(Path(args.output) / "cache-receipt.json", {"archive_cache_key": key, "games": requested_games})
            else:
                command = [sys.executable, str(HERE / "seed_archives.py"), *guides(args.guides, requested_games)]
                for identity in selected_seed_ids(args.guides, requested_games):
                    command += ["--only", identity]
                if args.dry_run:
                    command.append("--dry-run")
                checked(command, Path(args.output) / "seed-private.log")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("::error::full-run stage failed; inspect retained stage evidence.", file=sys.stderr)
        return 1
    finally:
        os.umask(old)


if __name__ == "__main__":
    sys.exit(main())
