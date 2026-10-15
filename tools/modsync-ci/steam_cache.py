import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time


FRESHNESS_SECONDS = 14 * 86400
EXECUTABLES = {'k1': 'swkotor.exe', 'k2': 'swkotor2.exe'}


class CacheValidationError(ValueError):
    pass


def _regular_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise CacheValidationError('cache member is not an independent regular file')
    return path.read_bytes()


def _inventory_contract():
    specification = importlib.util.spec_from_file_location(
        'steam_acquire_cache_contract',
        Path(__file__).resolve().parents[1] / 'private-ci/src/steam_acquire.py')
    if specification is None or specification.loader is None:
        raise CacheValidationError('acquisition inventory contract is unavailable')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.inventory


def require_game(game, directory, receipt=None):
    directory = Path(directory)
    if game not in EXECUTABLES or directory.is_symlink() or not directory.is_dir():
        raise CacheValidationError('game directory is missing')
    for name in (EXECUTABLES[game], 'chitin.key', 'dialog.tlk'):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise CacheValidationError(f'game is incomplete: {name} is missing')
    modules = directory / 'modules'
    if modules.is_symlink() or not modules.is_dir() or not any(modules.iterdir()):
        raise CacheValidationError('game is incomplete: modules/ is missing or empty')
    if receipt is not None:
        inventory = json.loads(_regular_file(Path(receipt)))
        if not isinstance(inventory, dict) or not inventory:
            raise CacheValidationError('game receipt is missing or empty')


def validate_baseline(root, game, now=None, copy_to=None, require_fresh=False):
    root = Path(root)
    if game not in ('k1', 'k2') or root.is_symlink() or not root.is_dir():
        raise CacheValidationError('invalid authenticated cache root')
    baseline = root / f'baseline-{game}'
    if baseline.is_symlink() or not baseline.is_dir():
        raise CacheValidationError('filtered baseline is missing')
    scope_bytes = _regular_file(root / 'cache-scope.json')
    source_bytes = _regular_file(root / f'source-baseline-{game}.json')
    receipt_bytes = _regular_file(root / f'baseline-{game}.json')
    scope = json.loads(scope_bytes)
    source_receipt = json.loads(source_bytes)
    receipt = json.loads(receipt_bytes)
    required = {'schema', 'game', 'excluded_pattern', 'excluded_count',
                'excluded_bytes', 'source_receipt_sha256'}
    if not isinstance(scope, dict) or not required <= scope.keys():
        raise CacheValidationError('filtered cache scope is incomplete')
    if (type(scope['schema']) is not int or scope['schema'] != 1
            or scope['game'] != game or scope['excluded_pattern'] != '*.bik'
            or any(type(scope[key]) is not int or scope[key] < 0
                   for key in ('excluded_count', 'excluded_bytes'))
            or not isinstance(scope['source_receipt_sha256'], str)
            or re.fullmatch(r'[0-9a-fA-F]{64}', scope['source_receipt_sha256']) is None):
        raise CacheValidationError('filtered cache scope is invalid')
    if hashlib.sha256(source_bytes).hexdigest() != scope['source_receipt_sha256'].lower():
        raise CacheValidationError('source receipt authentication failed')
    if not isinstance(source_receipt, dict) or not source_receipt or not isinstance(receipt, dict) or not receipt:
        raise CacheValidationError('cache inventories are empty or invalid')
    if any(not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
           or '..' in PurePosixPath(path).parts or not isinstance(metadata, dict)
           for path, metadata in source_receipt.items()):
        raise CacheValidationError('source receipt paths or metadata are invalid')
    excluded = {path: metadata for path, metadata in source_receipt.items()
                if path.lower().endswith('.bik')}
    expected = {path: metadata for path, metadata in source_receipt.items()
                if not path.lower().endswith('.bik')}
    excluded_sizes = [metadata.get('size') for metadata in excluded.values()]
    if (receipt != expected or len(excluded) != scope['excluded_count']
            or any(type(size) is not int or size < 0 for size in excluded_sizes)
            or sum(excluded_sizes) != scope['excluded_bytes']):
        raise CacheValidationError('filtered inventory does not match its source scope')
    if any(path.name.lower().endswith('.bik') for path in baseline.rglob('*')):
        raise CacheValidationError('filtered baseline contains an excluded BIK path')
    inventory = _inventory_contract()
    if inventory(baseline) != receipt:
        raise CacheValidationError('filtered baseline inventory verification failed')
    require_game(game, baseline)
    stamp = _regular_file(root / 'refreshed-at').decode('ascii').strip()
    try:
        refreshed_at = float(stamp)
    except ValueError as error:
        raise CacheValidationError('cache freshness timestamp is invalid') from error
    if not math.isfinite(refreshed_at):
        raise CacheValidationError('cache freshness timestamp is invalid')
    current_time = time.time() if now is None else now
    age = current_time - refreshed_at
    if age < 0:
        raise CacheValidationError('cache freshness timestamp is in the future')
    fresh = age < FRESHNESS_SECONDS
    if require_fresh and not fresh:
        raise CacheValidationError('authenticated baseline is stale')
    if copy_to is not None:
        destination = Path(copy_to)
        if destination.exists() or destination.is_symlink():
            raise CacheValidationError('copy destination already exists')
        shutil.copytree(baseline, destination, ignore=shutil.ignore_patterns('steamapps'))
        if inventory(destination) != receipt:
            raise CacheValidationError('independent game copy verification failed')
    return {'cache_valid': True, 'fresh': fresh, 'age_seconds': int(age)}


def main():
    parser = argparse.ArgumentParser(description='Validate an authenticated filtered Steam game cache.')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate')
    validate.add_argument('--root', type=Path, required=True)
    validate.add_argument('--game', choices=('k1', 'k2'), required=True)
    validate.add_argument('--allow-missing', action='store_true')
    validate.add_argument('--require-fresh', action='store_true')
    validate.add_argument('--copy-to', type=Path)
    validate.add_argument('--github-output', type=Path)
    verify = commands.add_parser('verify-game')
    verify.add_argument('--root', type=Path, required=True)
    verify.add_argument('--game', choices=('k1', 'k2'), required=True)
    verify.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'verify-game':
        try:
            require_game(args.game, args.root, args.receipt)
        except CacheValidationError as error:
            parser.exit(1, f'Acquired game verification failed: {error}.\n')
        except (OSError, ValueError):
            parser.exit(1, 'Acquired game verification failed: game receipt is unreadable.\n')
        print(f'Acquired {args.game} game verified.')
        return
    try:
        if args.allow_missing and not args.root.exists() and not args.root.is_symlink():
            result = {'cache_valid': False, 'fresh': False, 'age_seconds': None}
        else:
            result = validate_baseline(args.root, args.game, copy_to=args.copy_to,
                                       require_fresh=args.require_fresh)
        if args.require_fresh and not result['fresh']:
            raise CacheValidationError('authenticated baseline is stale')
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, UnicodeDecodeError,
            ImportError, AttributeError, TypeError):
        if not args.allow_missing:
            parser.exit(1, 'Authenticated baseline validation failed; cache cannot be reused.\n')
        print('Cached game is unavailable or invalid; it will be reacquired.')
        result = {'cache_valid': False, 'fresh': False, 'age_seconds': None}
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as output:
            output.write(f"cache_valid={str(result['cache_valid']).lower()}\n")
            output.write(f"fresh={str(result['fresh']).lower()}\n")
    print('Authenticated baseline validated; cache_valid=' + str(result['cache_valid']).lower()
          + '; fresh=' + str(result['fresh']).lower())


if __name__ == '__main__':
    main()
