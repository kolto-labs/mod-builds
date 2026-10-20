#!/usr/bin/env python3
"""GitHub CLI extension for an associated project's encrypted CI companion."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

PLATFORMS = ("linux", "windows", "macos")
KINDS = ("cli", "gui")
RUNNERS = ("hosted", "self-hosted")
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


class CliError(Exception):
    """A safe, user-facing error that never includes secret input."""


def repository(value: str) -> str:
    if not REPOSITORY.fullmatch(value):
        raise CliError("Repository must be OWNER/REPOSITORY")
    return value


def commit(value: str) -> str:
    if not COMMIT.fullmatch(value):
        raise CliError("References must be immutable 40-character commit SHAs")
    return value.lower()


def gh(arguments: list[str], secret_input: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["gh", *arguments], input=secret_input, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as error:
        raise CliError("GitHub CLI could not start; install gh and authenticate first") from error
    if result.returncode:
        # gh may echo request bodies or diagnostics; never relay a secret operation's output.
        raise CliError(f"GitHub CLI operation failed (exit {result.returncode}); check gh auth status and repository access")
    return result.stdout


def workflow_name(platform: str, kind: str) -> str:
    return f"private-ci-{kind}-{platform}.yml"


def nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise CliError(f"{label} must be a non-empty string")
    return value


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        config = json.loads(raw)
    except (OSError, ValueError) as error:
        raise CliError("Could not read configuration JSON") from error
    if not isinstance(config, dict):
        raise CliError("Configuration JSON must be an object")
    return config


def private_config(args: argparse.Namespace, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = load_config(args.config) if raw is None else raw
    source = repository(nonempty(args.private_repo or raw.get("privateRepository"), "privateRepository"))
    source_ref = commit(nonempty(args.private_ref or raw.get("ref"), "Private source ref"))
    configured = raw.get("commands", {})
    if not isinstance(configured, dict):
        raise CliError("commands must be an object keyed by platform")
    commands: dict[str, dict[str, str]] = {}
    for platform in PLATFORMS:
        values = configured.get(platform, {})
        if not isinstance(values, dict):
            raise CliError(f"commands.{platform} must be an object")
        commands[platform] = {}
        for kind in KINDS:
            fallback = getattr(args, f"command_{kind}")
            commands[platform][kind] = nonempty(values.get(kind, fallback), f"commands.{platform}.{kind}")
    artifact = nonempty(args.artifact_path or raw.get("artifactPath", "artifacts/evidence"), "artifactPath")
    if artifact.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", artifact) or ".." in artifact.replace("\\", "/").split("/"):
        raise CliError("artifactPath must stay relative to the private checkout")
    if artifact != "artifacts/evidence":
        raise CliError("artifactPath must use the executor receipt allowlist directory artifacts/evidence")
    timeout = raw.get("timeoutMinutes", 45)
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise CliError("timeoutMinutes must be an integer between 1 and 180")
    return {"privateRepository": source, "ref": source_ref, "commands": commands,
            "artifactPath": artifact, "timeoutMinutes": timeout}


def wrapper(library: str, reference: str, runner: str, platform: str, kind: str, workflow_path: str, tool_path: str | None) -> str:
    # JSON-quoted scalars are also valid YAML; secret configuration is never serialized here.
    tool_input = f"      tool-path: {json.dumps(tool_path)}\n" if tool_path else ""
    return f'''name: Associated project CI ({platform}, {kind})
on:
  workflow_dispatch:
    inputs:
      runner-mode:
        description: Execution environment
        type: choice
        options: [hosted, self-hosted]
        default: {runner}
permissions:
  contents: read
jobs:
  associated-project:
    uses: {json.dumps(f"{library}/{workflow_path}@{reference}")}
    with:
      platform: {platform}
      kind: {kind}
      runner-mode: ${{{{ inputs.runner-mode }}}}
      tool-repository: {json.dumps(library)}
      tool-ref: {json.dumps(reference)}
{tool_input}    secrets:
      PRIVATE_CI_CONFIG: ${{{{ secrets.PRIVATE_CI_CONFIG }}}}
      PRIVATE_CI_TOKEN: ${{{{ secrets.PRIVATE_CI_TOKEN }}}}
      PRIVATE_CI_RECIPIENT: ${{{{ secrets.PRIVATE_CI_RECIPIENT }}}}
'''


def read_secret_file(path: str) -> str:
    try:
        value = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CliError("Could not read the token input file") from error
    return nonempty(value.strip(), "Token input")


def initialize(args: argparse.Namespace) -> int:
    library = repository(args.library)
    reference = commit(args.ref)
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", args.workflow_path):
        raise CliError("--workflow-path must name a .github/workflows YAML file")
    if args.tool_path and (not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", args.tool_path) or any(part in (".", "..") for part in args.tool_path.split("/"))):
        raise CliError("--tool-path must be a relative directory without traversal")
    target = Path(args.directory).resolve()
    config = private_config(args)
    if args.configure_secrets and (not args.repo or not args.token_file or not args.recipient):
        raise CliError("--configure-secrets requires --repo, --token-file and --recipient")
    if args.configure_secrets:
        repository(args.repo)
        if args.config == "-" and args.token_file == "-":
            raise CliError("Configuration and token cannot both read from stdin")
    private_output = Path(args.config_output).resolve() if args.config_output else Path(tempfile.mkdtemp(prefix="gh-private-ci-")) / "config.json"
    if private_output == target or target in private_output.parents:
        raise CliError("Private configuration output must be outside the companion repository")
    if private_output.exists() and not args.force:
        raise CliError("Private configuration output already exists; choose a new path or use --force")
    workflows = target / ".github" / "workflows"
    files = {workflows / workflow_name(platform, kind): wrapper(library, reference, args.runner, platform, kind, args.workflow_path, args.tool_path)
             for platform in PLATFORMS for kind in KINDS}
    if not args.force and any(path.exists() for path in files):
        raise CliError("A caller workflow already exists; use --force to replace all six callers")
    # Resolve secret inputs before creating files, so input failures leave caller files untouched.
    token = read_secret_file(args.token_file) if args.configure_secrets else None
    payload = json.dumps(config, indent=2) + "\n"
    private_output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(private_output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    workflows.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    if args.configure_secrets:
        gh(["secret", "set", "PRIVATE_CI_CONFIG", "--repo", args.repo], payload)
        gh(["secret", "set", "PRIVATE_CI_TOKEN", "--repo", args.repo], token)
        gh(["secret", "set", "PRIVATE_CI_RECIPIENT", "--repo", args.repo], args.recipient)
    print(f"Created six companion caller workflows in {workflows}")
    print(f"Private configuration saved outside the repository: {private_output}")
    if args.configure_secrets:
        print("Configured companion secrets through GitHub CLI stdin; no secret values were printed")
    else:
        print("No GitHub secrets changed. Configure PRIVATE_CI_CONFIG, PRIVATE_CI_TOKEN and PRIVATE_CI_RECIPIENT before dispatch.")
    print("The companion must be associated with this private project. Public run metadata remains visible; logs and artifacts must be encrypted.")
    return 0


def dispatch(args: argparse.Namespace) -> int:
    repo = repository(args.repo)
    command = ["workflow", "run", workflow_name(args.platform, args.kind), "--repo", repo,
               "--field", f"runner-mode={args.runner}"]
    if args.branch:
        command += ["--ref", args.branch]
    if args.implementation:
        command += ["--field", f"implementation={args.implementation}"]
    gh(command)
    print(f"Dispatched {workflow_name(args.platform, args.kind)} in {repo}")
    return 0


def refresh(args: argparse.Namespace) -> int:
    repo = repository(args.repo)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.config_secret) or args.config_secret.upper().startswith("GITHUB_"):
        raise CliError("--config-secret must be a valid non-reserved GitHub secret name")
    if args.config == "-":
        raise CliError("refresh requires an existing external configuration file")
    path = Path(args.config).resolve()
    if not path.is_file():
        raise CliError("refresh requires an existing external configuration file")
    if any((parent / ".git").exists() for parent in path.parents):
        raise CliError("Private configuration must remain outside Git working trees")
    # Validate the old source pin too; refresh cannot silently repair an unpinned config.
    existing = load_config(str(path))
    private_config(argparse.Namespace(config=str(path), private_repo=None, private_ref=None,
                                      command_cli=None, command_gui=None, artifact_path=None), existing)
    revision = args.private_ref
    if args.source_directory:
        try:
            result = subprocess.run(["git", "-C", args.source_directory, "rev-parse", "--verify", "HEAD"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except (OSError, ValueError) as error:
            raise CliError("Git could not read the local source revision") from error
        if result.returncode:
            raise CliError("Git could not read the local source revision")
        revision = result.stdout.strip()
    existing["ref"] = commit(nonempty(revision, "Private source ref"))
    payload = json.dumps(existing, indent=2) + "\n"
    staged: Path | None = None
    retain = False
    try:
        # Stage and fsync in the same external directory before touching the remote secret.
        descriptor, name = tempfile.mkstemp(prefix=".private-ci-refresh-", suffix=".json", dir=path.parent)
        staged = Path(name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        gh(["secret", "set", args.config_secret, "--repo", repo], payload)
        try:
            os.replace(staged, path)
        except OSError as error:
            retain = True
            raise CliError(f"Secret updated but local replacement failed; protected prepared config retained at {staged}") from error
    except OSError as error:
        raise CliError("Could not prepare the protected configuration update") from error
    finally:
        if staged is not None and not retain:
            staged.unlink(missing_ok=True)
    print(f"Updated private configuration secret for {repo}; private configuration replaced atomically")
    return 0


def status(args: argparse.Namespace) -> int:
    repo = repository(args.repo)
    fields = "databaseId,status,conclusion,url,headSha,createdAt,workflowName"
    if args.run_id:
        command = ["run", "view", str(args.run_id), "--repo", repo, "--json", fields]
    else:
        command = ["run", "list", "--repo", repo, "--limit", str(args.limit), "--json", fields]
        if bool(args.platform) != bool(args.kind):
            raise CliError("Filter status with both --platform and --kind")
        if args.platform:
            command += ["--workflow", workflow_name(args.platform, args.kind)]
    result = gh(command)
    try:
        parsed = json.loads(result)
    except ValueError as error:
        raise CliError("GitHub CLI returned invalid status JSON") from error
    print(json.dumps(parsed, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="gh private-ci", description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="Write six callers and keep private configuration outside the companion")
    init.add_argument("--library", required=True, help="Public reusable workflow library OWNER/REPOSITORY")
    init.add_argument("--ref", required=True, help="Immutable public library 40-character commit SHA")
    init.add_argument("--workflow-path", default=".github/workflows/modsync-private-job.yml", help="Reusable workflow path within the existing public library")
    init.add_argument("--tool-path", help="Optional vendored executor path passed to the reusable workflow")
    init.add_argument("--private-repo", help="Associated private source OWNER/REPOSITORY")
    init.add_argument("--private-ref", help="Immutable private source commit SHA")
    init.add_argument("--runner", choices=RUNNERS, default="hosted")
    init.add_argument("--command-cli")
    init.add_argument("--command-gui")
    init.add_argument("--config", help="Private JSON commands/configuration input; '-' reads stdin")
    init.add_argument("--config-output", help="Private output file outside the companion; default secure temporary file")
    init.add_argument("--artifact-path", help="Dedicated receipt directory; must be artifacts/evidence")
    init.add_argument("--directory", default=".")
    init.add_argument("--force", action="store_true")
    init.add_argument("--configure-secrets", action="store_true", help="Deliberately set the three companion secrets using gh stdin")
    init.add_argument("--repo", help="Companion OWNER/REPOSITORY for explicit secret configuration")
    init.add_argument("--token-file", help="Least-privilege private source read token file, or '-' for stdin; token never enters argv")
    init.add_argument("--recipient", help="Public age encryption recipient, stored through gh stdin")
    init.set_defaults(handler=initialize)
    run = commands.add_parser("run", help="Dispatch one exact companion caller workflow")
    run.add_argument("--repo", required=True)
    run.add_argument("--platform", required=True, choices=PLATFORMS)
    run.add_argument("--kind", required=True, choices=KINDS)
    run.add_argument("--runner", choices=RUNNERS, default="hosted")
    run.add_argument("--branch")
    run.add_argument("--implementation", choices=("rust", "typescript", "both"), help="Optional selector for associated two-implementation callers; omitted for generic callers")
    run.set_defaults(handler=dispatch)
    update = commands.add_parser("refresh", help="Refresh the private source pin and secret without changing public workflows")
    update.add_argument("--repo", required=True, help="Associated public companion OWNER/REPOSITORY")
    update.add_argument("--config", required=True, help="Existing private JSON file outside Git working trees")
    update.add_argument("--config-secret", default="PRIVATE_CI_CONFIG", help="Configuration secret name, including optional _RS or _TS suffix")
    source = update.add_mutually_exclusive_group(required=True)
    source.add_argument("--private-ref", help="New immutable private source commit SHA")
    source.add_argument("--source-directory", help="Local private source checkout whose HEAD is captured without printing it")
    update.set_defaults(handler=refresh)
    state = commands.add_parser("status", help="Read companion run status through gh")
    state.add_argument("--repo", required=True)
    state.add_argument("--platform", choices=PLATFORMS)
    state.add_argument("--kind", choices=KINDS)
    state.add_argument("--run-id", type=int)
    state.add_argument("--limit", type=int, default=10)
    state.set_defaults(handler=status)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return args.handler(args)
    except CliError as error:
        print(f"private-ci: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
