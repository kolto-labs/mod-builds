#!/usr/bin/env python3
"""Drive the real TypeScript web UI with source-local Playwright and tsx.

Requires an existing built packages/web/dist and source-local dependencies.
Does not build, install dependencies, change selections, approve authoring,
answer install prompts, retry, restore, or substitute a simulation. Evidence
is private. The supplied game and archive directories are actual workspaces.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib

import gui_diag


class Blocked(Exception):
    pass


def digest(path, algorithm="sha256"):
    checksum = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            checksum.update(block)
    return checksum.hexdigest()


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


WORKER = r'''
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {readFile, writeFile} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
const config = JSON.parse(await readFile(process.env.MODSYNC_GUI_CONFIG, 'utf8'));
const require = createRequire(join(config.source, 'package.json'));
const {chromium} = require('playwright');
const {createModSyncServer} = await import(pathToFileURL(join(config.source, 'packages/server/src/createServer.ts')).href);
const evidence = async (name, value) => writeFile(join(config.private, name), JSON.stringify(value, null, 2));
let server, browser, page;
let stage = 'launch';
let phase = 'preflight';
const mark = async next => {
  phase = next || phase;
  await writeFile(join(config.private, 'phase.txt'), `${phase}\n${stage}\n`).catch(() => {});
};
await mark();
const clicks = [];
const fail = reason => {throw new Error(reason);};
try {
  const token = randomBytes(32).toString('hex');
  server = await createModSyncServer({token, staticDir: join(config.source, 'packages/web/dist'), settingsPath: join(config.private, 'settings.json')});
  const address = await server.listen(0, '127.0.0.1');
  await mark('first_screenshot');
  browser = await chromium.launch({headless: true});
  page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(30000);
  page.on('dialog', async dialog => {await dialog.dismiss();});
  const read = async route => page.evaluate(async route => {
    const token = new URL(location.href).searchParams.get('token');
    const response = await fetch(route, {headers: {authorization: `Bearer ${token}`}});
    if (!response.ok) throw new Error(`Observation HTTP ${response.status}`);
    return response.json();
  }, route);
  const click = async name => {
    stage = String(name);
    await mark();
    const control = page.getByRole('button', {name, exact: true});
    await control.waitFor();
    if (!await control.isEnabled()) fail('visible_control_disabled');
    await control.click();
    clicks.push({control: String(name), at: new Date().toISOString()});
    await evidence('clicks.json', clicks);
  };
  await page.goto(`${address}/?token=${token}`);
  await page.getByRole('heading', {name: 'Welcome to ModSync', exact: true}).waitFor();
  await mark('install');
  if (await page.getByText('Mock mode is active').count()) fail('mock_ui_rejected');
  await click('Next →');
  await page.getByRole('heading', {name: 'Setup', exact: true}).waitFor();
  await page.getByPlaceholder('/path/to/build.toml').fill(config.input);
  const loaded = page.waitForResponse(response => new URL(response.url()).pathname === '/api/file/load');
  await click('Load');
  const loadResponse = await loaded;
  if (!loadResponse.ok()) fail('real_toml_load_rejected');
  await page.locator('.alert-success').filter({hasText: 'Loaded'}).waitFor();
  const file = await read('/api/file');
  await evidence('loaded-file.json', file);
  const selected = file.mods.filter(mod => mod.isSelected);
  if (!selected.length) fail('explicit_toml_selection_required');
  if (selected.some(mod => mod.isWidescreen)) fail('separate_widescreen_phase_requires_operator_route');
  if (selected.some(mod => mod.aspyrOnly)) fail('platform_choice_requires_operator_route');
  const expected = selected.map(mod => mod.guid);
  await evidence('selection.json', {expected});
  for (const [locator, value] of [
    [page.getByPlaceholder('/home/you/kotor-mods'), config.archives],
    [page.locator("input[placeholder^='C:']"), config.game_dir],
  ]) {
    await locator.fill(value);
    const updated = page.waitForResponse(response => new URL(response.url()).pathname === '/api/settings' && response.request().method() === 'PUT');
    await locator.press('Tab');
    if (!(await updated).ok()) fail('directory_settings_rejected');
  }
  const settings = await read('/api/settings');
  await evidence('settings-observed.json', {kotorDirectory: settings.kotorDirectory, modDirectory: settings.modDirectory, checkpointDirectory: settings.checkpointDirectory});
  if (settings.kotorDirectory !== config.game_dir || settings.modDirectory !== config.archives) fail('workspace_binding_mismatch');
  if (!settings.createCheckpoints) fail('physical_receipt_requires_checkpoints');
  for (let step = 0; step < 12; step++) {
    if (await page.getByRole('heading', {name: 'Validation', exact: true}).count()) break;
    await click('Next →');
  }
  await page.getByRole('heading', {name: 'Validation', exact: true}).waitFor();
  stage = 'validation-wait';
  await mark();
  await page.getByRole('button', {name: 'Run validation again', exact: true})
    .or(page.locator('.pill-ok').filter({hasText: 'Ready to install'}))
    .or(page.locator('.pill-error'))
    .first()
    .waitFor({timeout: 900000});
  await page.getByRole('button', {name: 'Run validation again', exact: true}).waitFor({timeout: 900000});
  if (await page.getByText('Errors must be fixed before installing', {exact: true}).count() || await page.locator('.pill-error').count()) fail('validation_blocked');
  await page.locator('.pill-ok').filter({hasText: 'Ready to install'}).waitFor({timeout: 60000});
  const current = await read('/api/file');
  if (JSON.stringify(current.mods.map(mod => [mod.guid, mod.isSelected, mod.options])) !== JSON.stringify(file.mods.map(mod => [mod.guid, mod.isSelected, mod.options]))) fail('selection_changed');
  await click('Next →');
  await page.getByRole('heading', {name: 'Ready to install', exact: true}).waitFor({timeout: 120000});
  await page.screenshot({path: join(config.private, 'ready.png'), fullPage: true});
  await click('Begin installation');
  await click('I understand, continue');
  await click('I have read them');
  await click(`Install ${expected.length} mods`);
  stage = 'installation';
  for (;;) {
    const status = await read('/api/install/status');
    await evidence('install-status.json', status);
    if (status.pendingPrompt) fail('deliberate_install_choice_requires_operator');
    if (status.summary?.error || status.summary?.recoveryPending) fail('backend_install_gate_or_failure');
    if (!status.running && status.summary) {
      const summary = status.summary;
      if (summary.virtualPrediction || summary.failed !== 0 || summary.skipped !== 0 || summary.succeeded !== expected.length || !summary.checkpointSessionId) fail('physical_install_receipt_incomplete');
      if (summary.mods.length !== expected.length || summary.mods.some(mod => mod.state !== 'Installed') || new Set(summary.mods.map(mod => mod.modGuid)).size !== expected.length || expected.some(guid => !summary.mods.some(mod => mod.modGuid === guid))) fail('receipt_selection_mismatch');
      await page.getByRole('heading', {name: 'Installation finished', exact: true}).waitFor({timeout: 300000});
      await page.screenshot({path: join(config.private, 'finished.png'), fullPage: true});
      await evidence('worker-result.json', {status: 'receipt_required', stage, ui_completed: true});
      break;
    }
    if (await page.getByRole('heading', {name: 'Installation failed', exact: true}).count()) fail('visible_installation_failure');
    await page.waitForTimeout(300);
  }
} catch (error) {
  if (page) await page.screenshot({path: join(config.private, 'blocked.png'), fullPage: true}).catch(() => {});
  await evidence('worker-result.json', {status: 'blocked', stage, reason: error.message, ui_completed: false});
} finally {
  if (browser) await browser.close();
  if (server) await server.close();
}
'''


def verify(args, private, result):
    status_path = private / "install-status.json"
    status = json.loads(status_path.read_text())
    summary = status["summary"]
    settings = json.loads((private / "settings-observed.json").read_text())
    root = Path(settings.get("checkpointDirectory") or args.game_dir / ".modsync/checkpoints")
    session_id = summary["checkpointSessionId"]
    if not isinstance(session_id, str) or not session_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in session_id
    ):
        raise Blocked("invalid_checkpoint_session_identity")
    directory = root / "sessions" / session_id
    session_path = directory / "session.json"
    if session_path.is_symlink():
        raise Blocked("symlink_receipt_rejected")
    session = json.loads(session_path.read_text())
    if (session.get("id") != session_id or session.get("kotorDirectory") != str(args.game_dir)
            or session.get("pendingMutation") or session.get("pendingPublication")):
        raise Blocked("checkpoint_binding_or_recovery_pending")
    checkpoints = session.get("checkpoints", [])
    if len(checkpoints) < 2:
        raise Blocked("physical_checkpoint_receipt_missing")
    state = {}
    receipt_hashes = {"session.json": digest(session_path)}
    for index, checkpoint in enumerate(checkpoints):
        path = directory / f"{index}.json"
        if path.is_symlink():
            raise Blocked("symlink_manifest_rejected")
        manifest = json.loads(path.read_text())
        if (manifest.get("index") != index or checkpoint.get("index") != index
                or manifest.get("id") != checkpoint.get("id")
                or manifest.get("isAnchor") != checkpoint.get("isAnchor")):
            raise Blocked("checkpoint_manifest_identity_mismatch")
        for entry in manifest["entries"]:
            relative = entry["path"]
            target = args.game_dir / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or not target.resolve().is_relative_to(args.game_dir):
                raise Blocked("checkpoint_manifest_path_escape")
            state[relative] = entry.get("sha1")
        receipt_hashes[path.name] = digest(path)
    verified = {}
    for relative, checksum in state.items():
        path = args.game_dir / relative
        if path.is_symlink():
            raise Blocked("symlink_game_output_rejected")
        if checksum is None:
            if path.exists():
                raise Blocked("checkpoint_deleted_output_present")
        elif not path.is_file() or digest(path, "sha1") != checksum:
            raise Blocked("checkpoint_output_hash_mismatch")
        else:
            verified[relative] = digest(path)
    if not verified:
        raise Blocked("no_hash_verified_installed_files")
    for name, checksum in receipt_hashes.items():
        if digest(directory / name) != checksum:
            raise Blocked("receipt_changed_during_verification")
    save(private / "verified-output-hashes.json", verified)
    result.update(status="installed", reason="real_ui_install_and_checkpoint_hashes_verified",
                  receipt_sha256=digest(status_path), checkpoint_receipts=receipt_hashes,
                  verified_game_files=len(verified),
                  output_manifest_sha256=digest(private / "verified-output-hashes.json"))


def execute(args, result):
    for name in ("source", "input", "game_dir", "archives"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    if args.input.suffix.lower() != ".toml" or not args.input.is_file():
        raise Blocked("real_toml_required")
    tomllib.loads(args.input.read_text(encoding="utf-8-sig"))
    if not args.source.is_dir() or not args.game_dir.is_dir() or not args.archives.is_dir():
        raise Blocked("real_source_game_and_archive_directories_required")
    args.work_dir = args.work_dir.resolve()
    roots = (args.source, args.input, args.game_dir, args.archives)
    if any(args.work_dir.is_relative_to(root) or root.is_relative_to(args.work_dir) for root in roots):
        raise Blocked("work_directory_overlaps_inputs")
    if args.game_dir.is_relative_to(args.archives) or args.archives.is_relative_to(args.game_dir):
        raise Blocked("game_and_archive_directories_overlap")
    if (args.game_dir.is_relative_to(args.source) or args.source.is_relative_to(args.game_dir)
            or args.input.is_relative_to(args.game_dir)):
        raise Blocked("install_target_overlaps_source_or_instruction_file")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="gui-typescript-", dir=args.work_dir))
    result["evidence_directory"] = str(private)
    result["input_sha256"] = digest(args.input)
    node = shutil.which("node")
    if not node:
        raise Blocked("missing_node_runtime")
    for relative in ("packages/server/src/createServer.ts", "packages/web/dist/index.html",
                     "node_modules/tsx/dist/loader.mjs", "node_modules/playwright/package.json"):
        if not (args.source / relative).is_file():
            raise Blocked("missing_source_prerequisite:" + relative)
    environment = os.environ.copy()
    environment.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        directory = private / name.lower()
        directory.mkdir(mode=0o700)
        environment[name] = str(directory)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "NODE_OPTIONS"):
        environment.pop(name, None)
    config = {name: str(getattr(args, name)) for name in ("source", "input", "game_dir", "archives")}
    config["private"] = str(private)
    save(private / "config.json", config)
    environment["MODSYNC_GUI_CONFIG"] = str(private / "config.json")
    command = [node, "--import", str(args.source / "node_modules/tsx/dist/loader.mjs"),
               "--input-type=module", "-"]
    process = None
    timed_out = False
    failure = None
    try:
        with (private / "driver.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=args.source, env=environment, stdin=subprocess.PIPE,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.communicate(WORKER.encode(), timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                raise Blocked("ui_timeout_recovery_may_be_required") from None
        worker_path = private / "worker-result.json"
        if not worker_path.is_file():
            raise Blocked("ui_worker_failed_see_private_driver_log")
        worker = json.loads(worker_path.read_text())
        result.update(worker)
        if process.returncode or worker.get("status") != "receipt_required":
            raise Blocked(worker.get("reason", "ui_worker_failed"))
        verify(args, private, result)
        if digest(args.input) != result["input_sha256"]:
            raise Blocked("input_changed_during_install")
    except BaseException as error:
        failure = error
        raise
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if failure is not None:
            result["worker_diagnostics"] = gui_diag.collect(
                private, (("worker", "driver.log", gui_diag.TAIL_LINES),),
                process.returncode if process else None, timed_out,
                (private / "worker-result.json").is_file(), failure,
                paths={private: "$EVIDENCE", args.work_dir: "$WORK_DIR", args.source: "$SOURCE"})
            if not (private / "worker-result.json").is_file():
                result["stage"] = (result["worker_diagnostics"]["stage"]
                                   or result["worker_diagnostics"]["phase"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "input", "game-dir", "archives", "work-dir", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=3600, help="Total UI time budget in seconds")
    args = parser.parse_args()
    os.umask(0o077)
    result = {"schema_version": 1, "mode": "gui", "implementation": "typescript",
              "runtime": "playwright-web", "status": "blocked", "stage": "preflight",
              "ui_completed": False,
              "scope": "Supplied TOML selections and options, base phase only",
              "limitations": ["No authoring approval or backend gate bypass",
                              "Interactive install prompts require operator choices",
                              "No dependency installation or source builds",
                              "No automatic recovery or retry",
                              "Checkpoint hashes cover tracked outputs, not full guide parity"]}
    destination = args.report.absolute()
    if destination.exists() or destination.is_symlink() or any(
        destination.resolve().is_relative_to(path.resolve())
        for path in (args.source, args.input, args.game_dir, args.archives)
    ):
        print("gui_typescript: report must be new and outside inputs", file=sys.stderr)
        return 2
    try:
        if not 0 < args.timeout < 604800:
            raise Blocked("timeout_must_be_positive_and_under_one_week")
        execute(args, result)
    except (Exception, KeyboardInterrupt) as error:
        result.update(status="blocked", reason=str(error) if isinstance(error, Blocked) else type(error).__name__)
    destination.parent.mkdir(parents=True, exist_ok=True)
    save(destination, result)
    diagnostics = result.get("worker_diagnostics")
    if diagnostics:
        print("\n".join(gui_diag.render_group(diagnostics)), flush=True)
    print(json.dumps({key: value for key, value in result.items() if key != "worker_diagnostics"}))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    sys.exit(main())
