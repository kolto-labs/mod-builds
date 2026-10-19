#!/usr/bin/env python3
"""Content-addressed storage on top of GitHub Actions artifacts.

Artifacts are immutable per run, so this module treats each upload as a
segment and keeps a small signed-by-hash index describing which segments hold
which encrypted chunks. Unchanged chunks remain referenced by later indexes;
garbage collection removes segments no longer referenced. Payloads must be
age-encrypted before staging. The GitHub token is read from the environment
and never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

MAX_INDEX = 32 * 1024 * 1024
MAX_CHUNK = 256 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,180}$")


class ArtifactStoreError(Exception):
    pass


def digest(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def safe_rel(value: str) -> str:
    path = Path(value)
    if not isinstance(value, str) or not value or path.is_absolute() or "\\" in value:
        raise ArtifactStoreError("Unsafe artifact member path")
    if any(part in ("", ".", "..") for part in path.parts) or not NAME_RE.fullmatch(value):
        raise ArtifactStoreError("Unsafe artifact member path")
    return value


def api(repo: str, token: str, path: str, method="GET", body=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo):
        raise ArtifactStoreError("Invalid repository")
    request = urllib.request.Request(
        "https://api.github.com/repos/" + repo + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "modsync-artifact-store"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        raise ArtifactStoreError("GitHub artifact API request failed") from None
    except urllib.error.URLError:
        raise ArtifactStoreError("GitHub artifact API is unavailable") from None


def list_artifacts(repo, token, prefix):
    page, result = 1, []
    while True:
        status, _, payload = api(repo, token, f"/actions/artifacts?per_page=100&page={page}")
        if status != 200:
            raise ArtifactStoreError("Could not list artifacts")
        data = json.loads(payload)
        result.extend(item for item in data.get("artifacts", []) if item.get("name", "").startswith(prefix))
        if len(data.get("artifacts", [])) < 100:
            return result
        page += 1


def download_artifact(repo, token, artifact_id: int, destination: Path):
    if not isinstance(artifact_id, int) or artifact_id <= 0:
        raise ArtifactStoreError("Invalid artifact id")
    status, _, payload = api(repo, token, f"/actions/artifacts/{artifact_id}/zip")
    if status != 200:
        raise ArtifactStoreError("Could not download artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.write_bytes(payload)
    temporary.replace(destination)


def extract_zip(archive: Path, root: Path):
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        for entry in entries:
            name = safe_rel(entry.filename)
            target = root / name
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as source_stream, target.open("wb") as output:
                    shutil.copyfileobj(source_stream, output)


def load_index(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INDEX:
        raise ArtifactStoreError("Invalid artifact index")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("chunks"), dict):
        raise ArtifactStoreError("Unsupported artifact index")
    return data


def stage(encrypted: Path, output: Path, chunk_size: int):
    if encrypted.is_symlink() or not encrypted.is_file() or chunk_size <= 0 or chunk_size > MAX_CHUNK:
        raise ArtifactStoreError("Invalid encrypted payload")
    output.mkdir(parents=True, exist_ok=True)
    chunks = {}
    with encrypted.open("rb") as source:
        while block := source.read(chunk_size):
            sha = hashlib.sha256(block).hexdigest()
            target = output / (sha + ".part")
            if target.exists() and digest(target)[0] != sha:
                raise ArtifactStoreError("Chunk collision")
            target.write_bytes(block)
            chunks[sha] = {"size": len(block), "file": target.name}
    if not chunks:
        raise ArtifactStoreError("Empty encrypted payload")
    return chunks


def stage_directory(encrypted_directory: Path, output: Path, chunk_size: int):
    """Stage stable per-object ciphertexts.

    Callers retain each age-encrypted source object by its source digest and
    replace only objects whose source changed. Re-encrypting one large tarball
    every refresh would randomize all age nonces and defeat deduplication.
    """
    if encrypted_directory.is_symlink() or not encrypted_directory.is_dir():
        raise ArtifactStoreError("Encrypted object directory is missing or unsafe")
    result = {}
    for source in sorted(encrypted_directory.iterdir(), key=lambda item: item.name):
        if source.is_symlink() or not source.is_file() or not NAME_RE.fullmatch(source.name):
            raise ArtifactStoreError("Encrypted object directory contains an unsafe member")
        result.update(stage(source, output, chunk_size))
    if not result:
        raise ArtifactStoreError("Encrypted object directory is empty")
    return result


def make_index(chunks, segment_id=None, previous=None):
    merged = dict(previous.get("chunks", {})) if previous else {}
    for sha, entry in chunks.items():
        merged[sha] = {"size": entry["size"], "artifactId": segment_id or entry.get("artifactId")}
    if any(not isinstance(item.get("artifactId"), int) or item["artifactId"] <= 0 for item in merged.values()):
        raise ArtifactStoreError("Every chunk needs an uploaded artifact id")
    body = {"schema": 1, "chunks": dict(sorted(merged.items()))}
    body["sha256"] = hashlib.sha256(json_bytes(body)).hexdigest()
    return body


def fetch_index(repo, token, artifact_id, output):
    with tempfile.TemporaryDirectory(prefix="artifact-index-") as temp:
        archive = Path(temp) / "index.zip"
        download_artifact(repo, token, artifact_id, archive)
        extract_zip(archive, Path(temp) / "unpacked")
        candidates = list((Path(temp) / "unpacked").glob("*.json"))
        if len(candidates) != 1:
            raise ArtifactStoreError("Index artifact must contain one JSON file")
        data = load_index(candidates[0])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(json_bytes(data))
    return data


def fetch_chunks(repo, token, index, destination):
    destination.mkdir(parents=True, exist_ok=True)
    groups = {}
    for sha, item in index["chunks"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not isinstance(item.get("artifactId"), int):
            raise ArtifactStoreError("Invalid chunk reference")
        groups.setdefault(item["artifactId"], []).append((sha, item["size"]))
    for artifact_id, expected in groups.items():
        with tempfile.TemporaryDirectory(prefix="artifact-segment-") as temp:
            archive = Path(temp) / "segment.zip"
            download_artifact(repo, token, artifact_id, archive)
            unpacked = Path(temp) / "unpacked"
            extract_zip(archive, unpacked)
            for sha, size in expected:
                candidate = unpacked / (sha + ".part")
                if not candidate.is_file() or candidate.stat().st_size != size or digest(candidate)[0] != sha:
                    raise ArtifactStoreError("Artifact chunk failed integrity verification")
                shutil.copyfile(candidate, destination / candidate.name)


def gc(repo, token, prefix, keep_ids):
    keep = {int(item) for item in keep_ids}
    removed = []
    for item in list_artifacts(repo, token, prefix):
        artifact_id = int(item["id"])
        if artifact_id not in keep and not item.get("expired"):
            api(repo, token, f"/actions/artifacts/{artifact_id}", method="DELETE")
            removed.append(artifact_id)
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "fetch-index", "fetch-chunks", "gc"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--repo")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--prefix", default="modsync-cache-")
    parser.add_argument("--keep-id", action="append", default=[])
    parser.add_argument("--chunk-size", type=int, default=128 * 1024 * 1024)
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env)
    if args.action != "stage" and (not args.repo or not token):
        raise ArtifactStoreError("A repository and authenticated token are required")
    if args.action == "stage":
        if (bool(args.input) == bool(args.input_dir)) or not args.output:
            raise ArtifactStoreError("Stage requires input and output")
        result = stage(args.input, args.output, args.chunk_size) if args.input else stage_directory(args.input_dir, args.output, args.chunk_size)
        print(json.dumps({"chunks": result}, sort_keys=True))
    elif args.action == "fetch-index":
        fetch_index(args.repo, token, args.artifact_id, args.output)
        print("Artifact index downloaded.")
    elif args.action == "fetch-chunks":
        fetch_chunks(args.repo, token, load_index(args.index), args.output)
        print("Artifact chunks downloaded and verified.")
    else:
        removed = gc(args.repo, token, args.prefix, args.keep_id)
        print(f"Removed {len(removed)} unreferenced artifact(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ArtifactStoreError:
        print("Artifact storage operation blocked.", file=os.sys.stderr)
        raise SystemExit(1)
