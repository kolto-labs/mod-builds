#!/usr/bin/env python3
"""Restore a game from an encrypted seed archive when neither the Actions cache nor the backup artifact has it.

The seed is a tar stream, compressed with zstd and encrypted with `openssl enc -aes-256-cbc -pbkdf2 -iter 200000`.
It is hosted at a private URL and configured with repository secrets (GAME is the game id, uppercased):
  GAME_SEED_URL_<GAME>      https URL of the seed (required)
  GAME_SEED_KEY_<GAME>      passphrase for openssl (required)
  GAME_SEED_SHA256_<GAME>   SHA-256 of the downloaded file (optional; a mismatch fails the job)

Command (a missing, unusable or failed seed is a ::warning:: and exit 0 so the caller falls through to SteamCMD;
only a SHA-256 mismatch exits 1):
  restore  download the seed, verify it, then stream openssl | zstd | tar into --game-dir; output restored=true
           only when chitin.key exists there afterwards
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

GAMES = ("k1", "k2")
CHECK_FILE = "chitin.key"
CURL_ATTEMPTS = 3
CURL_RANGE_ERROR = 33
HASH_BLOCK = 1024 * 1024
KDF_ITERATIONS = "200000"
MIN_PATH_PARTS = 3
MESSAGE_LIMIT = 300
SHA256_LENGTH = 64
HEX_DIGITS = frozenset("0123456789abcdef")

Seed = namedtuple("Seed", "url key_env sha256")


class SeedIntegrityError(Exception):
    pass


def env_names(game):
    suffix = game.upper()
    return {
        "url": "GAME_SEED_URL_" + suffix,
        "key": "GAME_SEED_KEY_" + suffix,
        "sha256": "GAME_SEED_SHA256_" + suffix,
    }


def valid_sha256(value):
    return len(value) == SHA256_LENGTH and set(value) <= HEX_DIGITS


def read_config(game, environ):
    names = env_names(game)
    url = environ.get(names["url"], "").strip()
    key = environ.get(names["key"], "")
    expected = environ.get(names["sha256"], "").strip().lower()
    missing = [names[field] for field, value in (("url", url), ("key", key)) if not value]
    if missing:
        return None, "not set: " + ", ".join(missing)
    if not url.lower().startswith("https://") or any(ord(char) <= 32 for char in url):
        return None, "%s is not a single https:// URL" % names["url"]
    if expected and not valid_sha256(expected):
        return None, "%s is not 64 hexadecimal characters" % names["sha256"]
    return Seed(url=url, key_env=names["key"], sha256=expected), ""


def curl_config(url):
    escaped = url.replace("\\", "\\\\").replace('"', '\\"')
    return 'url = "%s"\n' % escaped


def curl_command(output):
    return ["curl", "-fL", "-sS", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "3", "--retry-delay", "5", "--retry-all-errors", "--connect-timeout", "30",
            "-C", "-", "-o", str(output), "--config", "-"]


def decrypt_commands(archive, key_env, game_dir):
    return [
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", KDF_ITERATIONS,
         "-pass", "env:" + key_env, "-in", str(archive)],
        ["zstd", "-d", "-q", "-c"],
        ["tar", "-x", "-C", str(game_dir)],
    ]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_BLOCK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path, expected):
    actual = sha256_file(path)
    if actual != expected.lower():
        raise SeedIntegrityError("the downloaded seed does not match the configured SHA-256")
    return actual


def _clean(message):
    return " ".join(str(message).split())[:MESSAGE_LIMIT]


def _say(message):
    print("game seed: " + _clean(message), flush=True)


def _warn(message):
    print("::warning::game seed: " + _clean(message), flush=True)


def _output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, value))


def _clear_dir(path):
    resolved = Path(path).resolve()
    if len(resolved.parts) < MIN_PATH_PARTS:
        raise ValueError("refusing to clear %s" % resolved)
    shutil.rmtree(resolved, ignore_errors=True)


def download(seed, archive, runner=subprocess.run, attempts=CURL_ATTEMPTS):
    config = curl_config(seed.url)
    for attempt in range(1, attempts + 1):
        result = runner(curl_command(archive), input=config, text=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return True
        _say("download attempt %d of %d failed (curl exit %d)" % (attempt, attempts, result.returncode))
        if result.returncode == CURL_RANGE_ERROR:
            Path(archive).unlink(missing_ok=True)
    return False


def ensure_zstd(runner=subprocess.run, which=shutil.which):
    if which("zstd"):
        return True
    try:
        runner(["sudo", "apt-get", "install", "-y", "-qq", "zstd"], stdout=subprocess.DEVNULL, check=False)
    except OSError:
        return False
    return which("zstd") is not None


def run_pipeline(commands, environ):
    errors = [tempfile.TemporaryFile() for _ in commands]
    processes = []
    try:
        previous = None
        for index, command in enumerate(commands):
            last = index == len(commands) - 1
            process = subprocess.Popen(
                command, env=environ, stderr=errors[index],
                stdin=previous.stdout if previous else subprocess.DEVNULL,
                stdout=None if last else subprocess.PIPE)
            if previous:
                previous.stdout.close()
            processes.append(process)
            previous = process
        codes = [process.wait() for process in processes]
        details = []
        for command, error, code in zip(commands, errors, codes):
            error.seek(0)
            tail = error.read().decode("utf-8", "replace").strip().splitlines()[-2:] if code else []
            details.append("%s exit %d%s" % (command[0], code, ": " + " / ".join(tail) if tail else ""))
        return codes, details
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        for error in errors:
            error.close()


def restore(game, game_dir, environ, download_fn=download, pipeline_fn=run_pipeline, zstd_fn=ensure_zstd,
            work_dir=None):
    seed, reason = read_config(game, environ)
    if seed is None:
        _warn("no seed for %s (%s); continuing without it" % (game, reason))
        return False
    print("::add-mask::" + seed.url, flush=True)
    game_path = Path(game_dir)
    work = tempfile.mkdtemp(prefix="game-seed-", dir=work_dir)
    archive = Path(work) / "seed.bin"
    try:
        if not download_fn(seed, archive):
            _warn("could not download the %s seed; continuing without it" % game)
            return False
        if seed.sha256:
            verify_sha256(archive, seed.sha256)
            _say("seed SHA-256 verified")
        else:
            _say("no GAME_SEED_SHA256 configured; the download was not checked against a digest")
        if not zstd_fn():
            _warn("zstd is not available; continuing without the %s seed" % game)
            return False
        game_path.mkdir(parents=True, exist_ok=True)
        codes, details = pipeline_fn(decrypt_commands(archive, seed.key_env, game_path), environ)
        if any(codes):
            _warn("could not unpack the %s seed (%s); continuing without it" % (game, "; ".join(details)))
            _clear_dir(game_path)
            return False
        if not (game_path / CHECK_FILE).is_file():
            _warn("the %s seed has no %s; continuing without it" % (game, CHECK_FILE))
            _clear_dir(game_path)
            return False
        _say("restored %s from the seed" % game)
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def command_restore(args, environ=None):
    try:
        restored = restore(args.game, args.game_dir, os.environ if environ is None else environ)
    except SeedIntegrityError as error:
        print("::error::game seed: %s; fix or remove GAME_SEED_SHA256_%s" % (error, args.game.upper()), flush=True)
        return 1
    except (OSError, ValueError) as error:
        _warn("seed restore failed (%s); continuing without it" % type(error).__name__)
        return 0
    if restored:
        _output("restored", "true")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--game", required=True, choices=GAMES)
    restore_parser.add_argument("--game-dir", required=True)
    args = parser.parse_args(argv)
    return command_restore(args)


if __name__ == "__main__":
    sys.exit(main())
