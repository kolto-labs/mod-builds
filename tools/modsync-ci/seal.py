#!/usr/bin/env python3
"""Stream a regular-file tree into an authenticated encrypted cache envelope."""

import argparse
from contextlib import ExitStack
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile


MAGIC = b"MODSYNC-SEAL-1\n"
CHUNK = 1024 * 1024


def keys(salt):
    secret = os.environ.get("CACHE_ENCRYPTION_KEY", "")
    if len(secret.encode()) < 32:
        raise ValueError("CACHE_ENCRYPTION_KEY must contain at least 32 bytes")
    master = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 200000)
    return (hmac.digest(master, b"encryption", "sha256").hex(),
            hmac.digest(master, b"authentication", "sha256"))


def openssl(password, incoming, outgoing, decrypt=False):
    environment = dict(os.environ)
    environment.pop("CACHE_ENCRYPTION_KEY", None)
    environment["MODSYNC_SEAL_PASSWORD"] = password
    command = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
               "-md", "sha256", "-pass", "env:MODSYNC_SEAL_PASSWORD"]
    if decrypt:
        command.append("-d")
    return subprocess.Popen(command, stdin=incoming, stdout=outgoing,
                            stderr=subprocess.DEVNULL, env=environment)


def seal(source, output):
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be a directory")
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output must be outside source")
    output.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(32)
    password, authentication = keys(salt)
    header = MAGIC + salt
    descriptor, temporary = tempfile.mkstemp(dir=output.parent)
    try:
        with os.fdopen(descriptor, "w+b") as envelope:
            envelope.write(header)
            envelope.flush()
            process = openssl(password, subprocess.PIPE, envelope)
            try:
                with ExitStack() as handles:
                    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

                    def open_directory(name, parent=None):
                        descriptor = os.open(name, directory_flags, dir_fd=parent)
                        handles.callback(os.close, descriptor)
                        return descriptor

                    def fingerprint(metadata):
                        return (metadata.st_dev, metadata.st_ino, metadata.st_mode,
                                metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns,
                                metadata.st_ctime_ns, metadata.st_uid, metadata.st_gid)

                    def unchanged(expected, actual):
                        if fingerprint(expected) != fingerprint(actual):
                            raise ValueError("Source changed while sealing")

                    anchors = []
                    parent = open_directory(source.absolute().anchor)
                    for name in source.absolute().parts[1:]:
                        expected = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        descriptor = open_directory(name, parent)
                        unchanged(expected, os.fstat(descriptor))
                        anchors.append((parent, name, descriptor, expected))
                        parent = descriptor

                    directories = []

                    def walk(descriptor, prefix, archive):
                        expected = os.fstat(descriptor)
                        names = sorted(os.listdir(descriptor))
                        entries = []
                        directories.append((descriptor, expected, names, entries))
                        for name in names:
                            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                            entries.append((name, metadata))
                            member = tarfile.TarInfo(prefix + name)
                            member.mode = stat.S_IMODE(metadata.st_mode)
                            member.uid = metadata.st_uid
                            member.gid = metadata.st_gid
                            member.mtime = metadata.st_mtime
                            if stat.S_ISDIR(metadata.st_mode):
                                child = open_directory(name, descriptor)
                                unchanged(metadata, os.fstat(child))
                                member.type = tarfile.DIRTYPE
                                archive.addfile(member)
                                walk(child, prefix + name + "/", archive)
                            elif stat.S_ISREG(metadata.st_mode):
                                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                incoming_fd = os.open(name, flags, dir_fd=descriptor)
                                with os.fdopen(incoming_fd, "rb") as incoming:
                                    opened = os.fstat(incoming.fileno())
                                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                                        raise ValueError("Only single-link regular files may be sealed")
                                    unchanged(metadata, opened)
                                    member.size = opened.st_size
                                    archive.addfile(member, incoming)
                                    unchanged(opened, os.fstat(incoming.fileno()))
                            else:
                                raise ValueError("Only regular files and directories may be sealed")
                            unchanged(metadata, os.stat(name, dir_fd=descriptor,
                                                        follow_symlinks=False))

                    with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                        walk(parent, "", archive)
                        for descriptor, expected, names, entries in directories:
                            unchanged(expected, os.fstat(descriptor))
                            if sorted(os.listdir(descriptor)) != names:
                                raise ValueError("Source changed while sealing")
                            for name, metadata in entries:
                                unchanged(metadata, os.stat(name, dir_fd=descriptor,
                                                            follow_symlinks=False))
                        for parent, name, descriptor, expected in anchors:
                            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                            if (current.st_dev, current.st_ino, current.st_mode) != (
                                    expected.st_dev, expected.st_ino, expected.st_mode):
                                raise ValueError("Source directory changed while sealing")
                process.stdin.close()
                if process.wait() != 0:
                    raise ValueError("Encryption failed")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            mac = hmac.new(authentication, header, "sha256")
            envelope.seek(len(header))
            for chunk in iter(lambda: envelope.read(CHUNK), b""):
                mac.update(chunk)
            envelope.write(mac.digest())
            envelope.flush()
            os.fsync(envelope.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def unseal(source, destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination must not exist")
    with source.open("rb") as envelope, tempfile.TemporaryFile() as ciphertext:
        header = envelope.read(len(MAGIC) + 32)
        if len(header) != len(MAGIC) + 32 or not header.startswith(MAGIC):
            raise ValueError("Invalid envelope")
        password, authentication = keys(header[len(MAGIC):])
        remaining = os.fstat(envelope.fileno()).st_size - len(header) - 32
        if remaining < 32:
            raise ValueError("Truncated envelope")
        mac = hmac.new(authentication, header, "sha256")
        while remaining:
            chunk = envelope.read(min(CHUNK, remaining))
            if not chunk:
                raise ValueError("Truncated envelope")
            remaining -= len(chunk)
            mac.update(chunk)
            ciphertext.write(chunk)
        if not hmac.compare_digest(mac.digest(), envelope.read(32)) or envelope.read(1):
            raise ValueError("Envelope authentication failed")
        ciphertext.seek(0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            staged = Path(temporary) / "tree"
            staged.mkdir(mode=0o700)
            process = openssl(password, ciphertext, subprocess.PIPE, decrypt=True)
            try:
                seen = set()
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        name = PurePosixPath(member.name)
                        if (name.is_absolute() or ".." in name.parts or not name.parts
                                or "\\" in member.name or member.name in seen
                                or not (member.isdir() or member.isreg())):
                            raise ValueError("Unsafe archive member")
                        seen.add(member.name)
                        target = staged.joinpath(*name.parts)
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        if member.isdir():
                            target.mkdir(exist_ok=True, mode=0o700)
                        else:
                            with archive.extractfile(member) as incoming, target.open("xb") as outgoing:
                                shutil.copyfileobj(incoming, outgoing, CHUNK)
                            target.chmod(0o700 if member.mode & 0o111 else 0o600)
                while process.stdout.read(CHUNK):
                    pass
                if process.wait() != 0:
                    raise ValueError("Decryption failed")
                staged.rename(destination)
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seal", "unseal"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        (seal if args.command == "seal" else unseal)(args.source, args.destination)
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        parser.exit(1, "Cache sealing operation failed; no private diagnostics emitted.\n")


if __name__ == "__main__":
    main()
