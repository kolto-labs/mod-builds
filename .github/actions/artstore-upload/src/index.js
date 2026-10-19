'use strict';

const lib = require('./lib');

let core;

async function loadCore() {
  return import(/* webpackMode: "eager" */ '@actions/core');
}

async function loadArtifactModule() {
  return import(/* webpackMode: "eager" */ '@actions/artifact');
}

async function run() {
  core = await loadCore();
  const inputs = lib.parseInputs(process.env);
  const files = lib.collectFiles(inputs.directory);
  const relativeFiles = lib.relativeFileList(inputs.directory, files);

  const missing = lib.missingRuntimeVars(process.env);
  if (missing.length > 0) {
    throw new Error(lib.runtimeMissingMessage(missing));
  }

  const { DefaultArtifactClient, ArtifactNotFoundError } = await loadArtifactModule();
  const client = new DefaultArtifactClient();

  if (inputs.ifExists === 'skip') {
    let existing;
    try {
      existing = (await client.getArtifact(inputs.name)).artifact;
    } catch (error) {
      if (!(error instanceof ArtifactNotFoundError)) {
        throw error;
      }
    }
    if (existing) {
      core.warning(`Artifact '${inputs.name}' already exists (id ${existing.id}); skipping upload (if-exists: skip)`);
      publish(inputs, lib.buildResult({ ...existing, files: relativeFiles }), true);
      return;
    }
  }

  core.info(`Uploading ${files.length} file(s) from ${inputs.directory} as artifact '${inputs.name}'`);
  const response = await client.uploadArtifact(inputs.name, files, inputs.directory, {
    retentionDays: inputs.retentionDays,
    compressionLevel: inputs.compressionLevel,
  });
  if (response.id === undefined || response.id === null) {
    throw new Error(`Upload of artifact '${inputs.name}' did not return an artifact id`);
  }
  publish(inputs, lib.buildResult({ name: inputs.name, ...response, files: relativeFiles }), false);
}

function publish(inputs, result, skipped) {
  core.setOutput('artifact-id', String(result.id));
  core.setOutput('size', String(result.size));
  core.setOutput('digest', result.digest);
  if (inputs.resultFile) {
    lib.writeResultFile(inputs.resultFile, skipped ? { ...result, skipped: true } : result);
  }
  core.info(`Artifact '${result.name}' id=${result.id} size=${result.size} files=${result.files.length}${skipped ? ' (skipped)' : ''}`);
}

run().catch(async (error) => {
  const message = error instanceof Error ? error.message : String(error);
  if (core === undefined) {
    core = await loadCore();
  }
  core.setFailed(message);
});
