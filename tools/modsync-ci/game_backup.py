#!/usr/bin/env python3
"""Back the sealed game baseline up as an encrypted Actions artifact and refill the Actions cache from it.

The baseline.seal that `game_cache.py pack` writes is already encrypted with CACHE_ENCRYPTION_KEY. The artifact adds
the artstore part envelope on top of it (same key, same code as the artifact store): the seal is cut into parts of
at most PART_SIZE bytes, every part is sealed and authenticated on its own, and manifest.json records the whole-file
SHA-256, the size and the part list. The repository is public, so nothing but sealed parts and that manifest is ever
uploaded.

Commands (all of them exit 0; a backup or lookup problem is a ::warning:: and the matching output stays unset):
  status  look up the newest non-expired game-<game>-sealed artifact; outputs state=missing|stale|fresh|unknown
          and artifact_id
  stage   cut the cached seal into sealed parts plus manifest.json in a directory; output staged=true
  fetch   download an artifact's parts, decrypt, verify and join them into <seal-dir>/baseline.seal; output
          restored=true
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import artstore  # noqa: E402
import artstore_client as client_mod  # noqa: E402
import artstore_core as core  # noqa: E402

GAMES = ("k1", "k2")
PART_SIZE = 2000 * 1024 * 1024
REFRESH_MARGIN_DAYS = 14
MANIFEST_MEMBER = "manifest.json"
MANIFEST_SCHEMA = 1
MANIFEST_LIMIT = 1024 * 1024
RESTORED_SEAL = "baseline.seal"
FAILURES = (client_mod.ArtifactError, OSError, ValueError)


def artifact_name(game):
    return "game-%s-sealed" % game


def select_artifact(artifacts, name, now_epoch):
    live = [artifact for artifact in artifacts
            if artifact.get("name") == name and not artstore._is_dead(artifact, now_epoch)]
    return artstore._newest_first(live)[0] if live else None


def backup_state(artifact, now_epoch, margin_days=REFRESH_MARGIN_DAYS):
    if artifact is None:
        return "missing"
    expires = artstore._epoch(artifact.get("expires_at"))
    if expires is not None and expires - now_epoch <= margin_days * core.DAY_SECONDS:
        return "stale"
    return "fresh"


def find_seal(seal_dir):
    seals = sorted(Path(seal_dir).glob("*.seal"))
    if len(seals) != 1:
        raise ValueError("expected exactly one .seal file in %s, found %d" % (seal_dir, len(seals)))
    return seals[0]


def parse_manifest(text, game):
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA or data.get("game") != game:
        raise ValueError("manifest is not a %s game backup manifest" % game)
    parts = data.get("parts")
    if (not isinstance(data.get("sha256"), str) or not artstore._SHA_RE.match(data["sha256"])
            or not _positive(data.get("size")) or not isinstance(parts, list) or not parts):
        raise ValueError("manifest is incomplete")
    for index, part in enumerate(parts):
        if (not isinstance(part, dict) or part.get("n") != index
                or not isinstance(part.get("stored_sha256"), str)
                or not artstore._SHA_RE.match(part["stored_sha256"])
                or not _positive(part.get("stored_size")) or not _positive(part.get("size"))):
            raise ValueError("manifest part %d is invalid" % index)
    return data


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _say(message):
    print("game backup: " + artstore._redact(" ".join(str(message).split()))[:300], flush=True)


def _warn(message):
    print("::warning::game backup: " + artstore._redact(" ".join(str(message).split()))[:300], flush=True)


def _output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, value))


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(core.CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lookup(client, game, now_epoch):
    name = artifact_name(game)
    artifact = select_artifact(client.list_artifacts(name=name), name, now_epoch)
    return artifact, backup_state(artifact, now_epoch)


def stage(game, seal_dir, out_dir, key_env=core.KEY_ENV, part_size=PART_SIZE):
    seal = find_seal(seal_dir)
    out = Path(out_dir)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    try:
        result = core.split_and_seal(seal, out, part_size, key_env)
        manifest = {"schema": MANIFEST_SCHEMA, "game": game, **result}
        (out / MANIFEST_MEMBER).write_text(json.dumps(manifest, sort_keys=True))
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return manifest


def fetch(game, artifact_id, seal_dir, client, key_env=core.KEY_ENV):
    seal_dir = Path(seal_dir)
    seal_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".game-backup-", dir=seal_dir))
    try:
        manifest_path = work / MANIFEST_MEMBER
        client.read_member(artifact_id, MANIFEST_MEMBER, manifest_path)
        if manifest_path.stat().st_size > MANIFEST_LIMIT:
            raise ValueError("manifest is too large")
        manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"), game)
        paths = []
        for part in manifest["parts"]:
            target = work / ("%d%s" % (part["n"], core.PART_SUFFIX))
            client.read_member(artifact_id, part["stored_sha256"] + core.PART_SUFFIX, target,
                               expect_size=part["stored_size"])
            if _sha256_file(target) != part["stored_sha256"]:
                raise ValueError("stored part %d digest mismatch" % part["n"])
            paths.append(target)
        core.join_parts(paths, seal_dir / RESTORED_SEAL, manifest["sha256"], manifest["size"],
                        sealed=True, key_env=key_env)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def command_status(args, client=None, now=None):
    game = args.game
    state, artifact_id = "unknown", ""
    try:
        client = client or client_mod.from_environment()
        if client is None:
            raise ValueError("no GitHub token or repository is available for the artifact lookup")
        now_epoch = artstore._now(now)
        artifact, state = lookup(client, game, now_epoch)
        if artifact is not None:
            artifact_id = str(int(artifact["id"]))
            expires = artstore._epoch(artifact.get("expires_at"))
            left = "" if expires is None else ", %.1f days left" % ((expires - now_epoch) / core.DAY_SECONDS)
            _say("%s: %s (artifact %s%s)" % (artifact_name(game), state, artifact_id, left))
        else:
            _say("%s: no live artifact" % artifact_name(game))
    except FAILURES as exc:
        state, artifact_id = "unknown", ""
        _warn("could not look up %s: %s" % (artifact_name(game), artstore._describe(exc)))
    _output("state", state)
    _output("artifact_id", artifact_id)
    return 0


def command_stage(args):
    try:
        manifest = stage(args.game, args.seal_dir, args.out_dir)
    except FAILURES as exc:
        _warn("could not stage the %s backup: %s" % (args.game, artstore._describe(exc)))
        return 0
    _say("staged %s: %d parts, %s" % (artifact_name(args.game), len(manifest["parts"]),
                                        artstore._human(manifest["size"])))
    _output("staged", "true")
    return 0


def command_fetch(args, client=None):
    try:
        client = client or client_mod.from_environment()
        if client is None:
            raise ValueError("no GitHub token or repository is available for the artifact download")
        manifest = fetch(args.game, args.artifact_id, args.seal_dir, client)
    except FAILURES as exc:
        _warn("could not restore %s from artifact %s: %s" % (args.game, args.artifact_id,
                                                              artstore._describe(exc)))
        return 0
    _say("restored %s from artifact %s: %d parts, %s" % (args.game, args.artifact_id,
                                                          len(manifest["parts"]), artstore._human(manifest["size"])))
    _output("restored", "true")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stage", "fetch"):
        command = commands.add_parser(name)
        command.add_argument("--game", choices=GAMES, required=True)
        if name == "stage":
            command.add_argument("--seal-dir", type=Path, required=True)
            command.add_argument("--out-dir", type=Path, required=True)
        if name == "fetch":
            command.add_argument("--artifact-id", type=int, required=True)
            command.add_argument("--seal-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    handler = {"status": command_status, "stage": command_stage, "fetch": command_fetch}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
