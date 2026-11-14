#!/usr/bin/env python3
"""Recover a verified baseline for this repository's producer or install lane.

Uses the inherited game_cache/game_backup/artstore implementation locally. Reads
local cache seals, current-repository backups, then an explicitly named legacy
read repository. Nothing deletes remote caches/artifacts or publishes content.

GITHUB_TOKEN (or GH_TOKEN) is only for GITHUB_REPOSITORY. ARTSTORE_READ_TOKEN
(or ARTSTORE_TOKEN) is only for --legacy-repository. The legacy namespace never
changes the destination of new backups. CACHE_ENCRYPTION_KEY is passed only to
the local game-cache unpack process, never to an application process.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import artstore
import artstore_client
import game_backup

HERE = Path(__file__).resolve().parent
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
FAILURES = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            artstore_client.ArtifactError)


def _warning(label, error):
    # Provider exceptions can contain credentials/URLs. Only a bounded category
    # is useful here; the authenticated provider handles its own detailed logs.
    print("::warning::baseline %s unavailable (%s)" % (label, type(error).__name__), flush=True)


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _local_path(path, root):
    path = Path(path)
    resolved = path.resolve()
    if path.is_symlink() or resolved == root or root not in resolved.parents:
        raise ValueError("baseline paths must be isolated directories inside RUNNER_TEMP")
    return resolved


def _verification_environment(environ, decrypt):
    # No GitHub/storage/seed/Steam credentials or host Python import overrides
    # reach this subprocess. It runs only the repository-owned game-cache tool.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "RUNNER_TEMP")
    env = {name: environ[name] for name in allowed if name in environ}
    if decrypt and environ.get("CACHE_ENCRYPTION_KEY"):
        env["CACHE_ENCRYPTION_KEY"] = environ["CACHE_ENCRYPTION_KEY"]
    return env


def verify_seal(game, seal, game_dir, environ):
    command = [sys.executable, str(HERE / "game_cache.py")]
    subprocess.run(command + ["unpack", "--game", game, "--seal", str(seal),
                             "--game-dir", str(game_dir)], check=True,
                   env=_verification_environment(environ, decrypt=True))
    subprocess.run(command + ["inventory", "--game", game, "--game-dir", str(game_dir),
                             "--require-modules", "--label", "baseline-byte-verification"],
                   check=True, env=_verification_environment(environ, decrypt=False))


def _client(repository, token, environ):
    if not token or not repository:
        return None
    if REPOSITORY.fullmatch(repository) is None:
        raise ValueError("invalid artifact repository")
    return artstore_client.ArtifactsClient(
        repository, token, api=environ.get("GITHUB_API_URL") or artstore_client.DEFAULT_API)


def _artifacts(client, game, now):
    if client is None:
        return []
    name = game_backup.artifact_name(game)
    return artstore._newest_first([
        item for item in client.list_artifacts(name=name)
        if item.get("name") == name and not artstore._is_dead(item, now)
    ])


def recover(game, seal_dir, game_dir, *, legacy_repository="", local_only=False, audit_backup=True,
            environ=None, now=None):
    """Return available/origin/backup_required; materialize only verified bytes.

    backup_required is false only for a fully verified, fresh backup in the
    current repository. Freshness in a legacy namespace never suppresses local
    publication. Current fresh backups are checked even on a valid cache hit.
    """
    environ = os.environ if environ is None else environ
    if game not in game_backup.GAMES:
        raise ValueError("unsupported game")
    if not environ.get("RUNNER_TEMP"):
        raise ValueError("RUNNER_TEMP is required")
    root = Path(environ["RUNNER_TEMP"]).resolve(strict=True)
    seal_dir, game_dir = _local_path(seal_dir, root), _local_path(game_dir, root)
    if seal_dir == game_dir or seal_dir in game_dir.parents or game_dir in seal_dir.parents:
        raise ValueError("seal and game directories must be separate")
    if game_dir.exists():
        raise ValueError("game destination must be fresh")
    if legacy_repository and REPOSITORY.fullmatch(legacy_repository) is None:
        raise ValueError("invalid legacy repository")
    now = artstore._now(now)
    result = {"available": False, "origin": "none", "backup_required": True}
    selected = None
    verified_digests = {}
    with tempfile.TemporaryDirectory(prefix="fullrun-baseline-", dir=root) as work_text:
        work = Path(work_text)

        def verify(candidate, label):
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError("candidate is not a regular seal")
            digest = _digest(candidate)
            if digest in verified_digests:
                return candidate, verified_digests[digest]
            # Failed candidates can leave their own partial verification tree.
            # A different candidate never reuses that destination.
            destination = Path(tempfile.mkdtemp(prefix="verify-", dir=work)) / "game"
            verify_seal(game, candidate, destination, environ)
            verified_digests[digest] = destination
            print("baseline verified: " + label, flush=True)
            return candidate, destination

        if seal_dir.is_dir():
            candidates = sorted(seal_dir.glob("*.seal"), key=lambda path: (path.name != "baseline.seal", path.name))
            for index, candidate in enumerate(candidates):
                try:
                    if candidate.is_symlink():
                        raise ValueError("cached seal is a symlink")
                    snapshot = work / ("cache-%d.seal" % index)
                    shutil.copyfile(candidate, snapshot)
                    selected = verify(snapshot, "cache")
                    result["origin"] = "cache"
                    break
                except FAILURES as error:
                    _warning("cache candidate", error)

        current_repo = environ.get("GITHUB_REPOSITORY", "").strip()
        current_token = environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN")
        namespaces = []
        if not local_only and (selected is None or audit_backup):
            namespaces.append(("current", current_repo, current_token))
            if legacy_repository and legacy_repository.lower() != current_repo.lower():
                namespaces.append(("legacy", legacy_repository,
                                   environ.get("ARTSTORE_READ_TOKEN") or environ.get("ARTSTORE_TOKEN")))
        for namespace, repository, token in namespaces:
            if namespace == "legacy" and selected is not None:
                break
            try:
                client = _client(repository, token, environ)
                items = _artifacts(client, game, now)
            except FAILURES as error:
                _warning(namespace + " artifact lookup", error)
                continue
            for item in items:
                fresh = game_backup.backup_state(item, now) == "fresh"
                if selected is not None and not fresh:
                    continue
                try:
                    identifier = int(item["id"])
                    if identifier <= 0:
                        raise ValueError("invalid artifact id")
                    download = Path(tempfile.mkdtemp(prefix=namespace + "-", dir=work))
                    game_backup.fetch(game, identifier, download, client)
                    candidate = verify(download / game_backup.RESTORED_SEAL,
                                       "%s artifact %d" % (namespace, identifier))
                    if selected is None:
                        selected = candidate
                        result["origin"] = "artifact:%s:%s:%d" % (namespace, repository, identifier)
                    # A valid backup of another baseline does not preserve the
                    # baseline selected from cache. Publish the selected seal
                    # unless a current fresh backup contains those exact bytes.
                    if namespace == "current" and fresh and _digest(candidate[0]) == _digest(selected[0]):
                        result["backup_required"] = False
                        break
                    if namespace == "legacy":
                        break
                except FAILURES as error:
                    _warning(namespace + " artifact candidate", error)
            if namespace == "current" and not result["backup_required"]:
                break

        if selected is not None:
            seal, verified_game = selected
            seal_dir.mkdir(parents=True, exist_ok=True)
            # The canonical seal is copied from the exact verified snapshot;
            # original cache candidates are never deleted.
            staging = seal_dir / ".baseline.seal.verified"
            shutil.copyfile(seal, staging)
            os.replace(staging, seal_dir / game_backup.RESTORED_SEAL)
            game_dir.parent.mkdir(parents=True, exist_ok=True)
            verified_game.rename(game_dir)
            result["available"] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=game_backup.GAMES, required=True)
    parser.add_argument("--seal-dir", type=Path, required=True)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--legacy-repository", default="")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--consumer", action="store_true",
                        help="after verified cache recovery, skip producer-only backup freshness auditing")
    parser.add_argument("--local-only", action="store_true",
                        help="verify only staged cache seals, without artifact reads")
    args = parser.parse_args(argv)
    result = {"available": False, "origin": "none", "backup_required": True}
    failed = False
    try:
        result = recover(args.game, args.seal_dir, args.game_dir,
                         legacy_repository=args.legacy_repository, local_only=args.local_only,
                         audit_backup=not args.consumer)
    except FAILURES as error:
        _warning("request", error)
        failed = True
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write("%s=%s\n" % (key, str(value).lower() if isinstance(value, bool) else value))
    if failed or (args.required and not result["available"]):
        print("::error::No verified %s baseline is available." % args.game, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
