'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const lib = require('../src/lib');

function inputEnv(overrides = {}) {
  return { INPUT_NAME: 'bundle-1', INPUT_DIRECTORY: '/tmp/some/dir', ...overrides };
}

test('parseInputs applies defaults', () => {
  const parsed = lib.parseInputs(inputEnv());
  assert.equal(parsed.name, 'bundle-1');
  assert.equal(parsed.directory, path.resolve('/tmp/some/dir'));
  assert.equal(parsed.retentionDays, 90);
  assert.equal(parsed.compressionLevel, 0);
  assert.equal(parsed.resultFile, '');
  assert.equal(parsed.ifExists, 'fail');
});

test('parseInputs reads explicit values', () => {
  const parsed = lib.parseInputs(
    inputEnv({
      'INPUT_RETENTION-DAYS': '7',
      'INPUT_COMPRESSION-LEVEL': '6',
      'INPUT_RESULT-FILE': 'out/result.json',
      'INPUT_IF-EXISTS': 'SKIP',
    })
  );
  assert.equal(parsed.retentionDays, 7);
  assert.equal(parsed.compressionLevel, 6);
  assert.equal(parsed.resultFile, path.resolve('out/result.json'));
  assert.equal(parsed.ifExists, 'skip');
});

test('parseInputs requires name and directory', () => {
  assert.throws(() => lib.parseInputs({ INPUT_DIRECTORY: 'x' }), /required.*name/);
  assert.throws(() => lib.parseInputs({ INPUT_NAME: 'x' }), /required.*directory/);
  assert.throws(() => lib.parseInputs(inputEnv({ INPUT_NAME: '   ' })), /required.*name/);
});

test('parseInputs rejects invalid artifact names', () => {
  for (const char of lib.INVALID_NAME_CHARS) {
    assert.throws(() => lib.parseInputs(inputEnv({ INPUT_NAME: `a${char}b` })), /not allowed in artifact names/);
  }
});

test('parseInputs rejects out-of-range or malformed numbers and modes', () => {
  assert.throws(() => lib.parseInputs(inputEnv({ 'INPUT_RETENTION-DAYS': '91' })), /retention-days/);
  assert.throws(() => lib.parseInputs(inputEnv({ 'INPUT_RETENTION-DAYS': '-1' })), /retention-days/);
  assert.throws(() => lib.parseInputs(inputEnv({ 'INPUT_COMPRESSION-LEVEL': '10' })), /compression-level/);
  assert.throws(() => lib.parseInputs(inputEnv({ 'INPUT_COMPRESSION-LEVEL': 'high' })), /compression-level/);
  assert.throws(() => lib.parseInputs(inputEnv({ 'INPUT_IF-EXISTS': 'overwrite' })), /if-exists/);
});

test('collectFiles lists regular files recursively and rejects empty directories', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'artstore-test-'));
  try {
    assert.throws(() => lib.collectFiles(root), /contains no regular files/);
    fs.mkdirSync(path.join(root, 'nested', 'deeper'), { recursive: true });
    assert.throws(() => lib.collectFiles(root), /contains no regular files/);
    fs.writeFileSync(path.join(root, 'a.bin'), 'a');
    fs.writeFileSync(path.join(root, 'nested', 'deeper', 'b.bin'), 'b');
    const files = lib.collectFiles(root);
    assert.equal(files.length, 2);
    assert.deepEqual(lib.relativeFileList(root, files), ['a.bin', 'nested/deeper/b.bin']);
    assert.throws(() => lib.collectFiles(path.join(root, 'missing')), /does not exist/);
    assert.throws(() => lib.collectFiles(files[0]), /not a directory/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('missingRuntimeVars reports names only', () => {
  assert.deepEqual(lib.missingRuntimeVars({}), ['ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RESULTS_URL']);
  assert.deepEqual(lib.missingRuntimeVars({ ACTIONS_RUNTIME_TOKEN: 't', ACTIONS_RESULTS_URL: 'u' }), []);
  assert.doesNotMatch(lib.runtimeMissingMessage(['ACTIONS_RUNTIME_TOKEN']), /secret-value/);
});

test('writeResultFile writes the documented JSON shape', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'artstore-test-'));
  try {
    const target = path.join(root, 'sub', 'result.json');
    lib.writeResultFile(target, lib.buildResult({ name: 'n', id: 5, size: 10, digest: 'sha256:x', files: ['a'] }));
    assert.deepEqual(JSON.parse(fs.readFileSync(target, 'utf8')), {
      name: 'n',
      id: 5,
      size: 10,
      digest: 'sha256:x',
      files: ['a'],
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
