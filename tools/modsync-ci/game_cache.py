#!/usr/bin/env python3
"""Pack a SteamCMD game install into an encrypted cache, or unpack and verify one.

The cache never contains *.bik movies. Layout inside the seal:
  baseline-<game>/       game files
  baseline-<game>.json   {relative path: {size, sha256}} inventory
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SEAL = Path(__file__).with_name("seal.py")
EXECUTABLE = {"k1": "swkotor.exe", "k2": "swkotor2.exe"}


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"Game tree contains a symlink: {path.relative_to(directory)}")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
            result[path.relative_to(directory).as_posix()] = {
                "size": path.stat().st_size, "sha256": digest.hexdigest()}
    return result


def require_game(game, directory):
    for name in ("chitin.key", "dialog.tlk", EXECUTABLE[game]):
        if not (directory / name).is_file():
            raise SystemExit(f"{game} game is incomplete: {name} is missing")
    if not any((directory / "modules").glob("*")):
        raise SystemExit(f"{game} game is incomplete: modules/ is empty")


def seal(command, source, destination):
    subprocess.run([sys.executable, str(SEAL), command, "--source", str(source),
                    "--destination", str(destination)], check=True)


def pack(game, source, output):
    shutil.rmtree(source / "steamapps", ignore_errors=True)
    for path in list(source.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".bik":
            path.unlink()
    require_game(game, source)
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        staging = Path(staging)
        tree = staging / f"baseline-{game}"
        source.rename(tree)
        (staging / f"baseline-{game}.json").write_text(json.dumps(inventory(tree), sort_keys=True))
        output.unlink(missing_ok=True)
        seal("seal", staging, output)
    print(f"Packed {game} game cache without movies.")


def unpack(game, sealed, destination):
    if not sealed.is_file():
        raise SystemExit(f"No {game} game cache was restored; the game job must acquire it first.")
    if destination.exists():
        raise SystemExit(f"Destination already exists: {destination}")
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        staging = Path(staging) / "unsealed"
        seal("unseal", sealed, staging)
        tree = staging / f"baseline-{game}"
        receipt = json.loads((staging / f"baseline-{game}.json").read_text())
        shutil.rmtree(tree / "steamapps", ignore_errors=True)
        receipt = {path: meta for path, meta in receipt.items()
                   if not path.startswith("steamapps/") and not path.lower().endswith(".bik")}
        if inventory(tree) != receipt:
            raise SystemExit(f"{game} game cache failed inventory verification.")
        require_game(game, tree)
        tree.rename(destination)
    print(f"Restored verified {game} game to {destination}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pack", "unpack"))
    parser.add_argument("--game", choices=tuple(EXECUTABLE), required=True)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--seal", type=Path, required=True)
    args = parser.parse_args()
    args.seal.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "pack":
        pack(args.game, args.game_dir.resolve(), args.seal.resolve())
    else:
        args.game_dir.parent.mkdir(parents=True, exist_ok=True)
        unpack(args.game, args.seal.resolve(), args.game_dir.resolve())


if __name__ == "__main__":
    main()
