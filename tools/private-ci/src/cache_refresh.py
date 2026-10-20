#!/usr/bin/env python3
"""Private cache refresh adapter used by the scheduled workflow.

The complete source inventory and S3 configuration are supplied only through
the private FULL_BUILD_CONFIG secret; no provider credentials are committed.
"""
import argparse, json, os, pathlib, subprocess, sys

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=pathlib.Path)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = json.loads(os.environ['FULL_BUILD_CONFIG'])
        cache = config['cache']
        sources = pathlib.Path(os.environ.get('RUNNER_TEMP', '.')) / 'private-sources.json'
        sources.write_text(json.dumps({'sources': cache['sources']}), encoding='utf-8')
        command = [sys.executable, 'tools/private-ci/src/mod_cache.py', 'refresh', '--store-config', str(pathlib.Path(cache['storeConfig'])), '--sources', str(sources), '--receipt', str(args.output / 'refresh.json')]
        if args.force: command.append('--force')
        return subprocess.run(command).returncode
    except Exception as exc:
        print('Private cache refresh blocked.', file=sys.stderr)
        return 1
if __name__ == '__main__': raise SystemExit(main())
