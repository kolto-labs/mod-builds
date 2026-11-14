#!/usr/bin/env python3
"""Drive the production desktop using native mouse input and screenshot OCR.

No application messages, reflection, test hooks or install CLI commands are used
for validation/install. A missing desktop/input permission fails the lane.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--guides', type=Path, required=True)
    parser.add_argument('--game', choices=['k1', 'k2'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    out = args.output.resolve()
    fixture = Path(__file__).with_name('prove_synthetic_full_build.py')
    common = [sys.executable, str(fixture), '--binary', str(binary), '--guides',
              str(args.guides.resolve()), '--game', args.game, '--output', str(out)]
    subprocess.run(common + ['--prepare-only'], check=True, timeout=900)
    from prove_synthetic_full_build import isolated_environment
    environment = isolated_environment(out)
    os.environ.setdefault('OMP_THREAD_LIMIT', '2')
    import pyautogui
    import pytesseract
    pyautogui.FAILSAFE = True
    events = []
    shots = out / 'screenshots'
    shots.mkdir()
    process = None
    status = 'failed'

    def screen(label):
        if sys.platform.startswith('linux'):
            from PIL import ImageGrab
            shot = ImageGrab.grab(xdisplay=os.environ.get('DISPLAY'))
        else:
            shot = pyautogui.screenshot()
        shot.save(shots / (label + '.png'))
        return shot

    def locate(label, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Desktop exited before clickthrough completed')
            shot = screen('latest')
            crop_left = crop_top = 0
            source = shot
            mode = '--psm 11'
            if label in ('Create snapshot and install', 'Keep telemetry off'):
                bounds = shot.getbbox() if sys.platform.startswith('linux') else (0, 0, shot.width, shot.height)
                if bounds is None:
                    time.sleep(1)
                    continue
                left, top, right, bottom = bounds
                crop_left = int(left + (right - left) * 0.15)
                crop_top = int(top + (bottom - top) * 0.30)
                source = shot.crop((crop_left, crop_top, int(left + (right - left) * 0.85),
                                    int(top + (bottom - top) * 0.70)))
                mode = '--psm 6'
            ocr = source.resize((source.width * 2, source.height * 2))
            data = pytesseract.image_to_data(ocr, config=mode,
                                            output_type=pytesseract.Output.DICT)
            groups = {}
            for i, word in enumerate(data['text']):
                if word.strip():
                    key = tuple(data[k][i] for k in ('block_num', 'par_num', 'line_num'))
                    groups.setdefault(key, []).append(i)
            candidates = []
            for indices in groups.values():
                text = ' '.join(data['text'][i] for i in indices)
                pattern = r'\s*'.join(re.escape(word) for word in re.findall(r'\w+|[^\w\s]', label))
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    start, end = match.span()
                    cursor = 0
                    matching = []
                    for i in indices:
                        stop = cursor + len(data['text'][i])
                        if cursor < end and stop > start:
                            matching.append(i)
                        cursor = stop + 1
                    indices = matching
                    left = min(data['left'][i] for i in indices)
                    top = min(data['top'][i] for i in indices)
                    right = max(data['left'][i] + data['width'][i] for i in indices)
                    bottom = max(data['top'][i] + data['height'][i] for i in indices)
                    # Screenshot pixels and native mouse coordinates differ on Retina.
                    width, height = pyautogui.size()
                    candidates.append((len(text), (crop_left + (left + right) / 4) * width / shot.width,
                                       (crop_top + (top + bottom) / 4) * height / shot.height, text))
            if candidates:
                _, x, y, text = min(candidates)
                return x, y, text
            time.sleep(1)
        raise RuntimeError('Visible control not found: ' + label)

    def click(label):
        x, y, observed = locate(label)
        screen(f'{len(events):02d}-before-click')
        pyautogui.moveTo(x, y)
        pyautogui.mouseDown()
        time.sleep(0.15)
        pyautogui.mouseUp()
        pyautogui.moveTo(5, 5)
        events.append({'label': label, 'ocr': observed, 'x': x, 'y': y})
        (out / 'native-input-events.json').write_text(json.dumps(events, indent=2), encoding='utf-8')
        time.sleep(1)

    try:
        argv = [str(binary), 'gui', '--allow-multiple-instances', '--input',
                str(out / 'synthetic-guide.json'), '--game-dir', str(out / 'game'),
                '--source-dir', str(out / 'mods'), '--settings-dir', str(out / 'settings')]
        (out / 'gui-command.json').write_text(json.dumps(argv, indent=2), encoding='utf-8')
        with (out / 'gui.stdout').open('wb') as stdout, (out / 'gui.stderr').open('wb') as stderr:
            process = subprocess.Popen(argv, cwd=out, env=environment, stdout=stdout, stderr=stderr)
            click('Keep telemetry off')
            click('Validation / install')
            click('Pause sharing to edit')
            click('Run validation')
            locate('Validation passed', timeout=300)
            screen('validation-passed')
            from prove_synthetic_full_build import inventory, snapshot
            before = json.loads((out / 'pre-install-manifests.json').read_text(encoding='utf-8'))
            if before != {'game': snapshot(out / 'game'), 'mods': snapshot(out / 'mods')}:
                raise RuntimeError('Native GUI validation mutated content roots')
            click('Install selected mods')
            locate('Ready to Install')
            click('Next')
            click('Create snapshot and install')
            # Wait for actual byte results; the verifier also checks the ordered
            # durable completion receipt and unchanged synthetic game markers.
            expected = json.loads((out / 'expected-manifests.json').read_text(encoding='utf-8'))
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Desktop exited during installation')
                try:
                    actual = {'game': inventory(out / 'game'), 'mods': inventory(out / 'mods')}
                except FileNotFoundError:
                    # Native engine may rename/delete between enumeration and
                    # hashing. Only the final retained verifier accepts results.
                    time.sleep(1)
                    continue
                if actual == expected:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('GUI installation did not produce expected bytes')
            # Terminal completion is observed in the app, not inferred from bytes.
            locate('Installation complete', timeout=90)
            screen('installation-completed')
            subprocess.run(common + ['--verify-only'], check=True, timeout=120)
            result = subprocess.run([str(binary), 'completion-summary', '--game-dir',
                                     str(out / 'game'), '--source-dir', str(out / 'mods'), '--json'],
                                    capture_output=True, check=True, timeout=30, env=environment)
            (out / 'gui-completion-summary.json').write_bytes(result.stdout)
            receipt = json.loads(result.stdout)
            guide = json.loads((out / 'synthetic-guide.json').read_text(encoding='utf-8'))
            if not receipt or [c['guid'] for c in receipt['components']] != [c['Guid'] for c in guide]:
                raise RuntimeError('Durable GUI completion component order differs from guide')
            status = 'passed'
    finally:
        error = sys.exc_info()[1]
        try:
            screen('terminal')
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
            (out / 'gui-proof.json').write_text(json.dumps({
                'status': status, 'input': 'native mouse clicks', 'events': events,
                'error': str(error) if error is not None else None,
                'platform': sys.platform, 'copyrightedAssets': False,
                'binarySha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                'guideSha256': hashlib.sha256((args.guides / 'content' / args.game / 'full.md').read_bytes()).hexdigest(),
                'limitations': ['Synthetic action families only; real mod builds remain unverified',
                                'Hosted desktop permissions must permit real input and screenshots']
            }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

