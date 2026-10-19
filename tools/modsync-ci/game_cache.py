#!/usr/bin/env python3
"""Pack a SteamCMD game install into an encrypted cache, unpack and verify one, or print an inventory.

`inventory` is read-only and is never part of a cache key.

The cache never contains *.bik movies. Layout inside the seal:
  baseline-<game>/       game files
  baseline-<game>.json   {relative path: {size, sha256}} inventory
"""
import argparse
import hashlib
import json
import os
import re
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


def child(directory, name):
    """Direct child whose name matches case-insensitively (Steam ships K2 as Modules/, K1 as modules/)."""
    if directory is None or not directory.is_dir():
        return None
    exact = directory / name
    if exact.exists():
        return exact
    for entry in sorted(directory.iterdir()):
        if entry.name.casefold() == name.casefold():
            return entry
    return None


def tally(directory, accept, recursive=False):
    count = size = 0
    if directory is None or not directory.is_dir():
        return count, size
    stack = [directory]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and accept(entry.name.casefold()):
                    count += 1
                    size += entry.stat(follow_symlinks=False).st_size
    return count, size


def survey(directory):
    """Compact facts about an unpacked or freshly downloaded game directory (never part of a cache key)."""
    modules = child(directory, "modules")
    override = child(directory, "override")
    data = child(directory, "data")
    facts = {
        "top_level": sorted(entry.name + ("/" if entry.is_dir() else "") for entry in directory.iterdir()),
        "modules_dir": modules.name if modules else None,
        "override_dir": override.name if override else None,
        "data_dir": data.name if data else None,
        "rim": tally(modules, lambda name: name.endswith(".rim") and not name.endswith("_s.rim")),
        "rim_s": tally(modules, lambda name: name.endswith("_s.rim")),
        "mod": tally(modules, lambda name: name.endswith(".mod")),
        "dlg_erf": tally(modules, lambda name: name.endswith("_dlg.erf")),
        "modules_all": tally(modules, lambda name: True),
        "override_files": tally(override, lambda name: True, recursive=True),
        "bif": tally(data, lambda name: name.endswith(".bif")),
    }
    for name in ("chitin.key", "dialog.tlk"):
        found = child(directory, name)
        facts[name] = found.stat().st_size if found and found.is_file() else None
    steamapps = directory / "steamapps"
    manifests = sorted(steamapps.glob("appmanifest_*.acf")) if steamapps.is_dir() else []
    facts["steam"] = []
    for manifest in manifests:
        text = manifest.read_text(errors="replace")
        fields = {key: re.search(r'"%s"\s+"([^"]*)"' % key, text) for key in ("StateFlags", "buildid", "SizeOnDisk")}
        facts["steam"].append(manifest.name + " " + " ".join(
            f"{key}={match.group(1)}" for key, match in fields.items() if match))
    return facts


def format_bytes(size):
    return f"{size:,}B"


def render_survey(game, directory, facts, label=""):
    def counted(key):
        count, size = facts[key]
        return f"{count} files {format_bytes(size)}"

    modules = facts["modules_dir"]
    casing = ""
    if modules and modules != "modules":
        casing = " (not lowercase: a case-sensitive lookup of modules/ misses it)"
    sizes = ", ".join(f"{name}={format_bytes(facts[name]) if facts[name] is not None else 'MISSING'}"
                      for name in ("chitin.key", "dialog.tlk"))
    return [
        f"game inventory {game}{' ' + label if label else ''}: {directory}",
        f"  top-level: {' '.join(facts['top_level'])}",
        f"  modules dir: {modules or 'MISSING'}{casing}",
        f"  modules *.rim (no _s): {counted('rim')}",
        f"  modules *_s.rim: {counted('rim_s')}",
        f"  modules *.mod: {counted('mod')}",
        f"  modules *_dlg.erf: {counted('dlg_erf')}",
        f"  modules all files: {counted('modules_all')}",
        f"  override dir: {facts['override_dir'] or 'MISSING'}, files (recursive): {counted('override_files')}",
        f"  data dir: {facts['data_dir'] or 'MISSING'}, *.bif: {counted('bif')}",
        f"  {sizes}",
        f"  steam manifest: {'; '.join(facts['steam']) or 'none (steamapps/ removed or never present)'}",
    ]


def report_survey(game, directory, label=""):
    facts = survey(directory)
    lines = render_survey(game, directory, facts, label)
    print("\n".join(lines), flush=True)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(f"#### Game inventory: {game}{' ' + label if label else ''}\n\n```\n"
                         + "\n".join(lines[1:]) + "\n```\n\n")
    return facts


def require_modules(game, facts):
    if facts["rim"][0] + facts["rim_s"][0] == 0:
        raise SystemExit(
            f"{game} game is incomplete: modules/ holds no .rim files (modules dir: "
            f"{facts['modules_dir'] or 'missing'}). A Steam KOTOR install always ships modules/*.rim, so the "
            "SteamCMD download is partial or the wrong depot; not caching it.")


def seal(command, source, destination):
    subprocess.run([sys.executable, str(SEAL), command, "--source", str(source),
                    "--destination", str(destination)], check=True)


def pack(game, source, output):
    require_modules(game, survey(source))
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
    parser.add_argument("command", choices=("pack", "unpack", "inventory"))
    parser.add_argument("--game", choices=tuple(EXECUTABLE), required=True)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--seal", type=Path)
    parser.add_argument("--label", default="", help="inventory: lane name shown in the log and step summary")
    parser.add_argument("--require-modules", action="store_true",
                        help="inventory: exit nonzero when modules/ holds no .rim files")
    parser.add_argument("--quiet", action="store_true", help="inventory: print nothing, only set the exit status")
    args = parser.parse_args()
    if args.command == "inventory":
        if not args.game_dir.is_dir():
            raise SystemExit(f"{args.game} game directory is missing: {args.game_dir}")
        game_dir = args.game_dir.resolve()
        facts = survey(game_dir) if args.quiet else report_survey(args.game, game_dir, args.label)
        if args.require_modules:
            require_modules(args.game, facts)
        return
    if args.seal is None:
        parser.error(f"--seal is required for {args.command}")
    args.seal.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "pack":
        pack(args.game, args.game_dir.resolve(), args.seal.resolve())
    else:
        args.game_dir.parent.mkdir(parents=True, exist_ok=True)
        unpack(args.game, args.seal.resolve(), args.game_dir.resolve())


if __name__ == "__main__":
    main()
