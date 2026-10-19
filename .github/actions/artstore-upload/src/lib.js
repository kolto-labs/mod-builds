'use strict';

const fs = require('node:fs');
const path = require('node:path');

const INVALID_NAME_CHARS = ['"', ':', '<', '>', '|', '*', '?', '\\', '/', '\r', '\n'];
const IF_EXISTS_MODES = ['fail', 'skip'];
const RUNTIME_ENV_VARS = ['ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RESULTS_URL'];

function readInput(env, name, { required = false } = {}) {
  const key = `INPUT_${name.replace(/ /g, '_').toUpperCase()}`;
  const value = String(env[key] === undefined ? '' : env[key]).trim();
  if (required && value === '') {
    throw new Error(`Input required and not supplied: ${name}`);
  }
  return value;
}

function parseIntegerInput(raw, name, min, max, fallback) {
  if (raw === '') {
    return fallback;
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(`Input '${name}' must be an integer between ${min} and ${max}, got '${raw}'`);
  }
  const value = Number(raw);
  if (value < min || value > max) {
    throw new Error(`Input '${name}' must be between ${min} and ${max}, got ${value}`);
  }
  return value;
}

function validateArtifactName(name) {
  const bad = INVALID_NAME_CHARS.filter((char) => name.includes(char));
  if (bad.length > 0) {
    const shown = bad.map((char) => JSON.stringify(char).slice(1, -1)).join(' ');
    throw new Error(`Input 'name' contains characters not allowed in artifact names: ${shown}`);
  }
  return name;
}

function parseInputs(env = process.env) {
  const name = validateArtifactName(readInput(env, 'name', { required: true }));
  const directory = path.resolve(readInput(env, 'directory', { required: true }));
  const retentionDays = parseIntegerInput(readInput(env, 'retention-days'), 'retention-days', 0, 90, 90);
  const compressionLevel = parseIntegerInput(
    readInput(env, 'compression-level'),
    'compression-level',
    0,
    9,
    0
  );
  const resultFileRaw = readInput(env, 'result-file');
  const resultFile = resultFileRaw === '' ? '' : path.resolve(resultFileRaw);
  const ifExists = (readInput(env, 'if-exists') || 'fail').toLowerCase();
  if (!IF_EXISTS_MODES.includes(ifExists)) {
    throw new Error(`Input 'if-exists' must be one of ${IF_EXISTS_MODES.join(', ')}, got '${ifExists}'`);
  }
  return { name, directory, retentionDays, compressionLevel, resultFile, ifExists };
}

function collectFiles(directory) {
  let stat;
  try {
    stat = fs.statSync(directory);
  } catch (error) {
    throw new Error(`Input 'directory' does not exist or is not readable: ${directory}`);
  }
  if (!stat.isDirectory()) {
    throw new Error(`Input 'directory' is not a directory: ${directory}`);
  }

  const files = [];
  const pending = [directory];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        pending.push(full);
      } else if (entry.isFile()) {
        files.push(full);
      }
    }
  }

  if (files.length === 0) {
    throw new Error(`Directory '${directory}' contains no regular files to upload`);
  }
  return files.sort();
}

function relativeFileList(directory, files) {
  return files.map((file) => path.relative(directory, file).split(path.sep).join('/'));
}

function missingRuntimeVars(env = process.env) {
  return RUNTIME_ENV_VARS.filter((key) => !env[key]);
}

function runtimeMissingMessage(missing) {
  return (
    `${missing.join(' and ')} not set. Runtime artifacts can only be uploaded from a JavaScript action ` +
    'running inside a GitHub Actions job (run: steps do not receive these variables).'
  );
}

function buildResult({ name, id, size, digest, files }) {
  return { name, id, size, digest: digest || '', files };
}

function writeResultFile(resultFile, result) {
  fs.mkdirSync(path.dirname(resultFile), { recursive: true });
  fs.writeFileSync(resultFile, `${JSON.stringify(result, null, 2)}\n`);
}

module.exports = {
  INVALID_NAME_CHARS,
  RUNTIME_ENV_VARS,
  buildResult,
  collectFiles,
  missingRuntimeVars,
  parseInputs,
  relativeFileList,
  runtimeMissingMessage,
  validateArtifactName,
  writeResultFile,
};
