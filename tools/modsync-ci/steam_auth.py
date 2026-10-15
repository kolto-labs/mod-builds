#!/usr/bin/env python3
"""Bounded transport and restoration for SteamCMD authentication state."""

from __future__ import annotations

import base64
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zlib


SECRET_LIMIT = 48 * 1024
DECOMPRESSED_LIMIT = 16 * 1024 * 1024
BUNDLE_LIMIT = 24 * 1024 * 1024
FILE_LIMIT = DECOMPRESSED_LIMIT
FILE_COUNT_LIMIT = 32
FORMAT = b"MODSYNC-STEAM-AUTH-1\n"
CONFIG_PATH = "steamcmd/config/config.vdf"
SSFN_DIRECTORIES = (
    "",
    "steamcmd",
    ".steam",
    ".steam/root",
    ".steam/steam",
    ".local/share/Steam",
)
REGISTRY_PATHS = frozenset((
    ".steam/registry.vdf",
    ".steam/root/registry.vdf",
    ".steam/steam/registry.vdf",
    ".local/share/Steam/registry.vdf",
    "steamcmd/registry.vdf",
    "steamcmd/config/registry.vdf",
))
SSFN_NAME = re.compile(r"ssfn[A-Za-z0-9._-]{1,120}\Z")


class AuthStateError(ValueError):
    pass


def _allowed_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AuthStateError("Invalid authentication-state path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise AuthStateError("Invalid authentication-state path")
    normalized = path.as_posix()
    if normalized != value:
        raise AuthStateError("Invalid authentication-state path")
    if normalized == CONFIG_PATH or normalized in REGISTRY_PATHS:
        return normalized
    parent = path.parent.as_posix() if path.parent != PurePosixPath(".") else ""
    if parent in SSFN_DIRECTORIES and SSFN_NAME.fullmatch(path.name):
        return normalized
    raise AuthStateError("Authentication-state path is not allowlisted")


def _validated_files(files: dict[str, bytes]) -> dict[str, bytes]:
    if not isinstance(files, dict) or not 0 < len(files) <= FILE_COUNT_LIMIT:
        raise AuthStateError("Invalid authentication-state file count")
    result = {}
    total = 0
    for raw_path, content in files.items():
        path = _allowed_path(raw_path)
        if path in result:
            raise AuthStateError("Duplicate authentication-state path")
        if not isinstance(content, bytes) or not content or len(content) > FILE_LIMIT:
            raise AuthStateError("Invalid authentication-state file size")
        total += len(content)
        if total > DECOMPRESSED_LIMIT:
            raise AuthStateError("Authentication state exceeds size limit")
        result[path] = content
    if CONFIG_PATH not in result:
        raise AuthStateError("SteamCMD configuration is missing")
    return result


def encode_secret(files: dict[str, bytes]) -> str:
    validated = _validated_files(files)
    entries = [
        {"path": path, "data": base64.b64encode(content).decode("ascii")}
        for path, content in sorted(validated.items())
    ]
    document = json.dumps(
        {"format": "modsync-steam-auth", "version": 1, "files": entries},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    payload = FORMAT + document
    if len(payload) > BUNDLE_LIMIT:
        raise AuthStateError("Authentication bundle exceeds decompressed size limit")
    compressed = base64.b64encode(gzip.compress(payload, mtime=0)).decode("ascii")
    if len(compressed.encode("ascii")) > SECRET_LIMIT:
        raise AuthStateError("Authentication state exceeds GitHub secret limit")
    return compressed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthStateError("Duplicate authentication-state field")
        result[key] = value
    return result


def _decompress_bounded(compressed: bytes) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = decoder.decompress(compressed, BUNDLE_LIMIT + 1)
        if decoder.unconsumed_tail or len(data) > BUNDLE_LIMIT:
            raise AuthStateError("Authentication state exceeds decompressed size limit")
        data += decoder.flush(BUNDLE_LIMIT + 1 - len(data))
    except zlib.error as error:
        raise AuthStateError("Invalid compressed authentication state") from error
    if (len(data) > BUNDLE_LIMIT or not decoder.eof
            or decoder.unused_data):
        raise AuthStateError("Invalid compressed authentication state")
    return data


def decode_secret(secret: str) -> dict[str, bytes]:
    if not isinstance(secret, str) or not secret or len(secret) > SECRET_LIMIT:
        raise AuthStateError("Invalid or oversized authentication secret")
    try:
        encoded = secret.encode("ascii")
        compressed = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError) as error:
        raise AuthStateError("Invalid authentication secret encoding") from error
    payload = _decompress_bounded(compressed)
    if not payload.startswith(FORMAT):
        return _validated_files({CONFIG_PATH: payload})
    try:
        document = json.loads(payload[len(FORMAT):], object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuthStateError("Invalid authentication-state bundle") from error
    if (not isinstance(document, dict)
            or set(document) != {"format", "version", "files"}
            or document["format"] != "modsync-steam-auth"
            or document["version"] != 1
            or not isinstance(document["files"], list)
            or not 0 < len(document["files"]) <= FILE_COUNT_LIMIT):
        raise AuthStateError("Unsupported authentication-state bundle")
    files = {}
    for entry in document["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "data"}:
            raise AuthStateError("Invalid authentication-state entry")
        path = _allowed_path(entry["path"])
        if path in files:
            raise AuthStateError("Duplicate authentication-state path")
        try:
            content = base64.b64decode(entry["data"].encode("ascii"), validate=True)
        except (AttributeError, UnicodeError, ValueError) as error:
            raise AuthStateError("Invalid authentication-state file encoding") from error
        files[path] = content
    return _validated_files(files)


def _safe_directory(root: Path, relative: PurePosixPath, *, create: bool) -> Path | None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                return None
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            if stat.S_ISLNK(metadata.st_mode) and not create:
                return None
            raise AuthStateError("Authentication-state directory is not a real directory")
    return current


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_size <= 0 or metadata.st_size > FILE_LIMIT):
                raise AuthStateError("Authentication-state entry is not a bounded regular file")
            content = source.read(FILE_LIMIT + 1)
            if len(content) != metadata.st_size:
                raise AuthStateError("Authentication-state file changed while reading")
            return content
    except OSError as error:
        raise AuthStateError("Could not safely read authentication-state file") from error


def capture(home: Path) -> dict[str, bytes]:
    home = Path(home)
    if home.is_symlink() or not home.is_dir():
        raise AuthStateError("Steam home must be a real directory")
    config_parent = _safe_directory(
        home, PurePosixPath(CONFIG_PATH).parent, create=False)
    if config_parent is None:
        raise AuthStateError("SteamCMD configuration directory is missing or linked")
    config = _read_regular(config_parent / PurePosixPath(CONFIG_PATH).name)
    files = {CONFIG_PATH: config}
    total_size = len(config)

    def add(relative: str, path: Path) -> None:
        nonlocal total_size
        if relative in files:
            raise AuthStateError("Duplicate authentication-state path")
        if len(files) >= FILE_COUNT_LIMIT:
            raise AuthStateError("Too many authentication-state files")
        try:
            metadata = path.lstat()
        except OSError as error:
            raise AuthStateError("Could not safely inspect authentication-state file") from error
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size <= 0 or metadata.st_size > FILE_LIMIT
                or total_size + metadata.st_size > DECOMPRESSED_LIMIT):
            raise AuthStateError("Authentication-state entry exceeds safe file limits")
        content = _read_regular(path)
        files[relative] = content
        total_size += len(content)

    candidate_directories = set(SSFN_DIRECTORIES)
    candidate_directories.update(str(PurePosixPath(path).parent) for path in REGISTRY_PATHS)
    for relative_directory in sorted(candidate_directories):
        directory = _safe_directory(home, PurePosixPath(relative_directory), create=False) if relative_directory else home
        if directory is None:
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if SSFN_NAME.fullmatch(name):
                    relative = f"{relative_directory}/{name}" if relative_directory else name
                    path = _allowed_path(relative)
                    add(path, directory / name)
                elif name == "registry.vdf":
                    relative = f"{relative_directory}/{name}" if relative_directory else name
                    if relative in REGISTRY_PATHS:
                        add(relative, directory / name)
    return _validated_files(files)


def _write_regular(home: Path, relative: str, content: bytes) -> None:
    path = PurePosixPath(_allowed_path(relative))
    if relative == CONFIG_PATH:
        return
    parent = _safe_directory(home, path.parent, create=True)
    target = parent / path.name
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
        raise AuthStateError("Refusing to replace a linked authentication-state file")
    descriptor, temporary = tempfile.mkstemp(prefix=".steam-auth-", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def restore_auxiliary(files: dict[str, bytes], home: Path) -> None:
    home = Path(home)
    if home.is_symlink() or not home.is_dir():
        raise AuthStateError("Steam home must be a real directory")
    for relative, content in _validated_files(files).items():
        _write_regular(home, relative, content)
