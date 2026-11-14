#!/usr/bin/env python3
"""Repository-owned full-run request, provenance and coverage contract.

No network, credentials, game mutation or Rust test compilation happens here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

OPERATIONS = ("check", "full", "games", "archives", "install", "seed", "p2p")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def exact_sha(value):
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ValueError("an exact lowercase 40-character commit SHA is required")
    return value


def repository(value):
    if not isinstance(value, str) or REPO.fullmatch(value) is None:
        raise ValueError("invalid repository identity")
    return value


def plan(config, execution_repository, own_sha, counterpart_sha="", operation="check",
         game="both", mode="both", write_cache=False, event="workflow_dispatch"):
    if config.get("schema_version") != 1 or config.get("role") not in ("engine", "guides"):
        raise ValueError("unsupported repository-owned CI configuration")
    owner = repository(config["repository"])
    if repository(execution_repository).lower() != owner.lower():
        raise ValueError("this CI variant must execute in its owning repository")
    own_sha = exact_sha(own_sha)
    counterpart_sha = exact_sha(counterpart_sha or config["counterpart_sha"])
    engine_repo = repository(config["engine_repository"])
    guide_repo = repository(config["guide_repository"])
    if operation not in OPERATIONS or game not in ("both", "k1", "k2") or mode not in ("both", "cli", "gui"):
        raise ValueError("unsupported operation, game or mode")
    if type(write_cache) is not bool:
        raise ValueError("write_cache must be boolean")
    if event != "workflow_dispatch" and operation != "check":
        raise ValueError("licensed/full-run operations require explicit manual dispatch")
    # The dispatch form shares this checkbox across operations. Non-producer
    # operations are always read-only, regardless of the form's default value.
    write_cache = write_cache and operation in ("full", "games", "archives")
    if config["role"] == "engine":
        if engine_repo.lower() != owner.lower():
            raise ValueError("engine variant must test its own commit")
        engine_sha, guide_sha = own_sha, counterpart_sha
    else:
        if guide_repo.lower() != owner.lower():
            raise ValueError("guide variant must test its own commit")
        engine_sha, guide_sha = counterpart_sha, own_sha
    result = {
        "schema_version": 1, "owner_repository": owner, "owner_sha": own_sha,
        "engine_repository": engine_repo, "engine_sha": engine_sha,
        "guide_repository": guide_repo, "guide_sha": guide_sha,
        "operation": operation, "games": ["k1", "k2"] if game == "both" else [game],
        "modes": ["cli", "gui"] if mode == "both" else [mode],
        "cache_writer": write_cache,
        "claim": "workflow-contract-only" if operation == "check" else "single-lane-installation",
        "cross_lane_parity": "unverified",
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result["plan_id"] = hashlib.sha256(canonical).hexdigest()
    return result


def require_complete(report, mode, expected_guide_sha256=None):
    if not isinstance(report, dict) or report.get("status") != "installed" or report.get("implementation") != "rust":
        raise ValueError("required Rust lane did not complete installation")
    if report.get("mode") != mode:
        raise ValueError("lane mode mismatch")
    if report.get("partial") is not False or report.get("omitted_components") != []:
        raise ValueError("partial or missing coverage cannot satisfy a full install")
    count = report.get("component_count")
    if type(count) is not int or count <= 0:
        raise ValueError("missing/nonpositive component coverage")
    errors = report.get("lane_errors")
    if not isinstance(errors, dict) or type(errors.get("count")) is not int or errors["count"] != 0:
        raise ValueError("missing or failing lane error reconciliation")
    if expected_guide_sha256 is not None and report.get("guide_sha256") != expected_guide_sha256:
        raise ValueError("report guide digest does not match the admitted guide")
    if mode == "gui":
        gui = report.get("gui")
        scope = report.get("gui_not_covered")
        if (not isinstance(gui, dict) or gui.get("coverage") != "full"
                or not isinstance(scope, dict) or scope.get("excluded_widescreen") != []
                or scope.get("widescreen_phase") != "none_in_input"):
            raise ValueError("GUI subset or unverified widescreen phase is incomplete")
    return {"status": "complete", "component_count": count,
            "cross_lane_parity": "unverified"}


def aggregate(operation, results):
    required = {"check": ("plan", "toolkit"), "full": ("plan", "toolkit", "games", "archives", "install", "p2p"),
                "games": ("plan", "toolkit", "games"), "archives": ("plan", "toolkit", "archives"),
                "install": ("plan", "toolkit", "install"), "seed": ("plan", "toolkit", "seed"),
                "p2p": ("plan", "toolkit", "p2p")}
    if operation not in required or not isinstance(results, dict):
        raise ValueError("unknown requested coverage")
    missing = [name for name in required[operation]
               if not isinstance(results.get(name), dict) or results[name].get("result") != "success"]
    if missing:
        raise ValueError("required jobs missing, skipped, cancelled or failed: " + ", ".join(missing))
    return {"operation": operation, "status": "passed", "required_jobs": list(required[operation]),
            "cross_lane_parity": "unverified"}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def emit_outputs(value):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            for key, item in value.items():
                text = json.dumps(item, separators=(",", ":")) if not isinstance(item, str) else item
                if "\n" in text or "\r" in text:
                    raise ValueError("unsafe workflow output")
                stream.write(key + "=" + text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--config", default=".github/fullrun.json")
    p.add_argument("--output", required=True)
    c = sub.add_parser("complete")
    c.add_argument("--report", required=True)
    c.add_argument("--guide", required=True)
    c.add_argument("--mode", choices=("cli", "gui"), required=True)
    c.add_argument("--output", required=True)
    a = sub.add_parser("aggregate")
    a.add_argument("--operation", required=True)
    a.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            raw = os.environ.get("REQUEST_WRITE_CACHE", "false").lower()
            if raw not in ("true", "false"):
                raise ValueError("write_cache must be boolean")
            value = plan(json.loads(Path(args.config).read_text()), os.environ["GITHUB_REPOSITORY"],
                         os.environ["GITHUB_SHA"], os.environ.get("REQUEST_COUNTERPART_SHA", ""),
                         os.environ.get("REQUEST_OPERATION", "check"), os.environ.get("REQUEST_GAME", "both"),
                         os.environ.get("REQUEST_MODE", "both"), raw == "true",
                         os.environ["GITHUB_EVENT_NAME"])
            if value["operation"] in ("full", "games", "install") and not os.environ.get("CACHE_ENCRYPTION_KEY"):
                raise ValueError("CACHE_ENCRYPTION_KEY is required for game materialization")
            if value["operation"] in ("full", "install", "archives", "seed") and not os.environ.get("DIAGNOSTICS_ENCRYPTION_KEY"):
                raise ValueError("DIAGNOSTICS_ENCRYPTION_KEY is required for retained private lane evidence")
            emit_outputs(value)
        elif args.command == "complete":
            digest = hashlib.sha256(Path(args.guide).read_bytes()).hexdigest()
            value = require_complete(json.loads(Path(args.report).read_text()), args.mode, digest)
        else:
            value = aggregate(args.operation, json.loads(os.environ.get("NEEDS_JSON", "{}")))
        write_json(args.output, value)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError) as error:
        if args.command == "aggregate":
            write_json(args.output, {"operation": args.operation, "status": "failed", "reason": str(error),
                                     "cross_lane_parity": "unverified"})
        print("::error::full-run contract: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
