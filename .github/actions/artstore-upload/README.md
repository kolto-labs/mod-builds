# artstore-upload

Local JavaScript action that uploads every regular file under a directory as a single GitHub Actions runtime artifact.

Why it exists: `run:` steps do not receive `ACTIONS_RUNTIME_TOKEN` / `ACTIONS_RESULTS_URL`, and the REST API has no artifact upload endpoint. Only a JavaScript action using `@actions/artifact` can upload, so shell/Python tooling stages files and calls this action.

## Usage

```yaml
- uses: ./.github/actions/artstore-upload
  id: upload
  with:
    name: artstore-parts-${{ github.run_id }}
    directory: ${{ runner.temp }}/artstore-parts
    result-file: ${{ runner.temp }}/artstore-upload.json
- run: |
    echo "artifact id: ${{ steps.upload.outputs.artifact-id }}"
    cat "$RUNNER_TEMP/artstore-upload.json"
```

## Inputs

| Input | Default | Notes |
| --- | --- | --- |
| `name` | required | Must not contain `" : < > | * ? \ /`. Immutable per run. |
| `directory` | required | All regular files below it are uploaded; it is the artifact root. Fails if it holds no files. |
| `retention-days` | `90` | 0-90 (0 = repository default). |
| `compression-level` | `0` | 0-9; 0 because parts are already encrypted. |
| `result-file` | empty | Writes `{name,id,size,digest,files:[...]}` (`files` relative, `/`-separated). |
| `if-exists` | `fail` | `skip` reuses an existing artifact of the same name (result file gets `"skipped": true`). |

Outputs: `artifact-id`, `size`, `digest`.

File contents and environment variables are never logged; only counts, names, ids and sizes.

## Development

```sh
npm install
npm test                # node --test (input parsing/validation, file collection)
npm run build           # ncc build src/index.js -o dist --license licenses.txt
```

`dist/` (including the lazy chunk `835.index.js`) is generated and must be committed after any change to `src/` or dependencies. Runtime is `node24`, matching `actions/upload-artifact`.

`@actions/artifact` and `@actions/core` are ESM-only, so `src/index.js` loads them via dynamic `import()`, which ncc bundles.

## Verification limits

Outside a runner, `node dist/index.js` with `INPUT_*` variables fails with a message about the missing `ACTIONS_RUNTIME_TOKEN`. That proves the bundle loads, not that an upload works; a real upload can only be proven inside a GitHub-hosted or self-hosted runner job.
