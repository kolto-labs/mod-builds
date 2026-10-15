#!/usr/bin/env python3
"""Acquire an owned Windows game into an independent workspace copy."""
import argparse
import importlib.util
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    acquire = commands.add_parser("acquire")
    acquire.add_argument("--game", choices=("k1", "k2"), required=True)
    acquire.add_argument("--destination", type=Path, required=True)
    acquire.add_argument("--state-dir", type=Path, required=True)
    acquire.add_argument("--report", type=Path, required=True)
    acquire.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "private-ci/src/steam_acquire.py"
    specification = importlib.util.spec_from_file_location("steam_acquire", source)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    try:
        module.acquire_game(args.game, args.destination, args.state_dir, args.report,
                            dict(os.environ), args.timeout)
    except Exception:
        print("Steam acquisition failed. Check the report and private state; Steam Guard may require authorization.")
        return 1
    print("Steam acquisition verified; independent workspace copy ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
