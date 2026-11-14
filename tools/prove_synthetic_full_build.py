#!/usr/bin/env python3
"""Production CLI proof from real full.md; all installed bytes are synthetic.

The authored draft is never approved. Safe action *families* are projected onto
independent fabricated payloads, preserving owner/instruction traversal order.
This is parser coverage and engine plumbing evidence, not a KOTOR mod install.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import uuid
import zipfile


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    return {p.relative_to(root).as_posix(): digest(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


def snapshot(root):
    """Capture entry types too: no-write checks must notice empty dirs/links."""
    result = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        key = path.relative_to(root).as_posix()
        if stat.S_ISLNK(mode):
            result[key] = {'type': 'symlink', 'target': os.readlink(path)}
        elif stat.S_ISDIR(mode):
            result[key] = {'type': 'directory'}
        elif stat.S_ISREG(mode):
            result[key] = {'type': 'file', 'sha256': digest(path)}
        else:
            result[key] = {'type': 'special', 'mode': stat.S_IFMT(mode)}
    return result


def owners(components, parent=()):
    for index, component in enumerate(components):
        address = parent + (index,)
        yield address, component
        yield from owners(component.get('Options', []), address)


def isolated_environment(out):
    env = os.environ.copy()
    for key, folder in [('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'),
                        ('XDG_DATA_HOME', 'data'), ('APPDATA', 'appdata'),
                        ('LOCALAPPDATA', 'localappdata')]:
        env[key] = str(out / folder)
    return env


def verify_receipt(binary, out):
    result = subprocess.run([str(binary), 'completion-summary', '--game-dir', str(out / 'game'),
                             '--source-dir', str(out / 'mods'), '--json'],
                            capture_output=True, timeout=120, env=isolated_environment(out))
    (out / 'completion-summary.stdout').write_bytes(result.stdout)
    (out / 'completion-summary.stderr').write_bytes(result.stderr)
    require(result.returncode == 0, 'Completion query failed')
    receipt = json.loads(result.stdout)
    expected = json.loads((out / 'synthetic-guide.json').read_text(encoding='utf-8'))
    require(isinstance(receipt, dict), 'No completion receipt available for order check')
    require(receipt.get('components') == [{'guid': c['Guid'], 'name': c['Name']} for c in expected],
            'Completion receipt component order differs from projection traversal order')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--guides', type=Path, required=True)
    parser.add_argument('--game', choices=['k1', 'k2'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    guide = (args.guides / 'content' / args.game / 'full.md').resolve(strict=True)
    out = args.output.resolve()
    if args.verify_only:
        expected = json.loads((out / 'expected-manifests.json').read_text(encoding='utf-8'))
        actual = {'game': inventory(out / 'game'), 'mods': inventory(out / 'mods')}
        require(actual == expected, 'Synthetic final hashes differ from expected manifests')
        (out / 'final-manifests.json').write_text(json.dumps(actual, indent=2), encoding='utf-8')
        verify_receipt(binary, out)
        print('Synthetic content hashes verified and receipt order checked; real mod installation unproved')
        return
    require(not out.exists(), 'Proof output must be a fresh directory')
    out.mkdir(parents=True)
    game, mods = out / 'game', out / 'mods'
    for root in (game, mods):
        root.mkdir()
    (game / 'Override').mkdir()
    (game / 'Modules').mkdir()
    baseline = f'SYNTHETIC {args.game} baseline; no game resources\n'.encode()
    (game / 'SYNTHETIC_BASELINE.txt').write_bytes(baseline)
    baseline_manifest = inventory(game)
    (out / 'baseline-manifest.json').write_text(json.dumps(baseline_manifest, indent=2), encoding='utf-8')
    env = isolated_environment(out)

    def run(label, argv, success=True):
        result = subprocess.run([str(binary), *map(str, argv)], capture_output=True,
                                cwd=out, env=env, timeout=600)
        (out / f'{label}.stdout').write_bytes(result.stdout)
        (out / f'{label}.stderr').write_bytes(result.stderr)
        (out / f'{label}.command.json').write_text(json.dumps(
            {'argv': list(map(str, argv)), 'exitCode': result.returncode}, indent=2), encoding='utf-8')
        if success:
            require(result.returncode == 0, f'{label} failed; see retained stderr')
        return result

    plain, draft = out / 'plain.json', out / 'draft.json'
    run('convert-plain', ['convert', guide, plain, '--format', 'json'])
    run('convert-draft', ['convert', guide, draft, '--format', 'json', '--parse-directions'])
    plain_components = json.loads(plain.read_text(encoding='utf-8'))
    draft_components = json.loads(draft.read_text(encoding='utf-8'))
    require(isinstance(plain_components, list) and isinstance(draft_components, list),
            'Expected component arrays from production converter')
    require(len(plain_components) == len(draft_components) > 0,
            'Draft generation changed root count or parsed no roots')
    plain_owners = [(address, c.get('Name')) for address, c in owners(plain_components)]
    draft_owners = [(address, c.get('Name')) for address, c in owners(draft_components)]
    original_addresses = {address for address, _ in plain_owners}
    require(plain_owners == [(address, name) for address, name in draft_owners
                             if address in original_addresses],
            'Draft generation removed/reordered/renamed an original recursive owner')
    added_owners = [{'owner': list(address), 'name': name} for address, name in draft_owners
                    if address not in original_addresses]
    (out / 'draft-added-owners.json').write_text(json.dumps(added_owners, indent=2), encoding='utf-8')
    ledger, projected, expected_game = [], [], dict(baseline_manifest)
    expected_mods = {}
    counters = collections.Counter()
    for address, component in owners(draft_components):
        instructions = component.get('Instructions', [])
        review = component.get('DraftReview', {})
        row = {'owner': list(address), 'name': component.get('Name'),
               'guid': component.get('Guid'), 'directionsCharacters': len(component.get('Directions', '')),
               'requiresReview': review.get('RequiresReview', False),
               'draftDiagnostics': review.get('Result'),
               'unresolvedMasters': component.get('UnresolvedMasters', []),
               'instructions': []}
        # Each owner becomes an independent synthetic component. Options are
        # traversed for coverage, not claimed as real selected conditional paths.
        fixture = {'Guid': str(uuid.uuid5(uuid.NAMESPACE_URL, args.game + repr(address))),
                   'Name': 'Synthetic owner ' + '.'.join(map(str, address)),
                   'IsSelected': True, 'Instructions': []}
        for index, instruction in enumerate(instructions):
            action = instruction.get('Action', 'Unset')
            counters[action] += 1
            evidence = {'index': index, 'action': action, 'authored': instruction,
                        'mode': 'omitted', 'reason': 'Requires real assets, choices, process/backend or cleanup semantics'}
            token = 'owner-' + '-'.join(map(str, address)) + f'-step-{index}'
            payload = f'SYNTHETIC {args.game} {token} {action}\n'.encode()
            name = token + '.txt'
            source = f'<<modDirectory>>/{name}'
            synthetic = {'Action': action, 'Source': [source], 'Overwrite': True}
            if action in ('Copy', 'Move'):
                (mods / name).write_bytes(payload)
                synthetic['Destination'] = '<<gameDirectory>>/Override'
                expected_game['Override/' + name] = hashlib.sha256(payload).hexdigest()
                if action == 'Copy':
                    expected_mods[name] = hashlib.sha256(payload).hexdigest()
            elif action == 'Delete':
                (game / 'Override' / name).write_bytes(payload)
                synthetic['Source'] = [f'<<gameDirectory>>/Override/{name}']
            elif action == 'Rename':
                (game / 'Override' / name).write_bytes(payload)
                synthetic['Source'] = [f'<<gameDirectory>>/Override/{name}']
                synthetic['Destination'] = token + '-renamed.txt'
                expected_game['Override/' + token + '-renamed.txt'] = hashlib.sha256(payload).hexdigest()
            elif action == 'Extract':
                archive = mods / (token + '.zip')
                with zipfile.ZipFile(archive, 'w') as stream:
                    stream.writestr(name, payload)
                synthetic['Source'] = [f'<<modDirectory>>/{archive.name}']
                expected_mods[archive.name] = digest(archive)
                expected_mods[token + '/' + name] = hashlib.sha256(payload).hexdigest()
            else:
                row['instructions'].append(evidence)
                continue
            evidence.update(mode='synthetic-action-family', reason='Authored paths and bytes replaced; no semantic equivalence claimed',
                            projected=synthetic)
            row['instructions'].append(evidence)
            # Keep each independent action in its own owner: the engine's
            # same-component Extract source normalization would otherwise bind
            # fabricated independent loose files to an unrelated archive.
            projected.append({**fixture,
                'Guid': str(uuid.uuid5(uuid.NAMESPACE_URL, args.game + repr(address) + ':' + str(index))),
                'Name': fixture['Name'] + f' step {index}', 'Instructions': [synthetic]})
        ledger.append(row)
    require(projected, 'No supported safe action families found')
    (out / 'coverage.json').write_text(json.dumps(ledger, indent=2), encoding='utf-8')
    fixture_path = out / 'synthetic-guide.json'
    fixture_path.write_text(json.dumps(projected, indent=2), encoding='utf-8')
    before = {'game': snapshot(game), 'mods': snapshot(mods)}
    (out / 'expected-manifests.json').write_text(json.dumps(
        {'game': expected_game, 'mods': expected_mods}, indent=2), encoding='utf-8')
    (out / 'pre-install-manifests.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
    if args.prepare_only:
        return
    original = run('authored-install-refusal', ['install', '--input', draft,
                   '--game-dir', game, '--source-dir', mods, '--settings-dir', out / 'settings'], success=False)
    require(original.returncode != 0, 'Unreviewed authored guide unexpectedly installed')
    require(b'unreviewed draft' in original.stderr.lower(),
            'Authored refusal did not demonstrate the draft review gate')
    require(before == {'game': snapshot(game), 'mods': snapshot(mods)},
            'Authored guide refusal changed synthetic content roots')
    run('synthetic-validate', ['validate', '--input', fixture_path, '--game-dir', game,
                             '--source-dir', mods, '--settings-dir', out / 'settings'])
    require(before == {'game': snapshot(game), 'mods': snapshot(mods)},
            'Validation mutated synthetic content roots')
    run('synthetic-install', ['install', '--input', fixture_path, '--game-dir', game,
                            '--source-dir', mods, '--settings-dir', out / 'settings'])
    actual = {'game': inventory(game), 'mods': inventory(mods)}
    (out / 'final-manifests.json').write_text(json.dumps(actual, indent=2), encoding='utf-8')
    require(actual['game'] == expected_game, 'Synthetic game final hashes differ')
    require(actual['mods'] == expected_mods, 'Synthetic mod final hashes differ')
    verify_receipt(binary, out)
    summary = {'game': args.game, 'guideSha256': digest(guide),
               'binarySha256': digest(binary), 'rootComponents': len(draft_components),
               'owners': len(ledger), 'draftAddedOwners': added_owners,
               'originalRecursiveOwnerOrderChecked': True, 'authoredActionCounts': dict(counters),
               'reviewRequiredOwners': sum(row['requiresReview'] for row in ledger),
               'ownersWithoutInstructions': sum(not row['instructions'] for row in ledger),
               'syntheticComponents': len(projected),
               'syntheticInstructions': sum(len(c['Instructions']) for c in projected),
               'omittedInstructions': sum(i['mode'] == 'omitted' for row in ledger for i in row['instructions']),
               'authoredInstallRefused': True, 'syntheticInstallHashesVerified': True,
               'completionReceiptOrderChecked': True,
               'limitations': ['No copyrighted game/mod bytes, real downloads or backend execution',
                               'Safe action families only; authored paths/selection/conditions are replaced',
                               'Unparsed prose, unresolved drafts and omitted actions remain incomplete',
                               'No real K1/TSL installation, resource equivalence or live-game proof']}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()

