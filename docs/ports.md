# Port install lanes

The `install` job in `.github/workflows/ci.yml` runs `tools/modsync-ci/ports.py`
for every combination of game (`k1`, `k2`), implementation (`rust`, `typescript`)
and mode (`cli`, `gui`).

## In CI

Each lane:

1. Checks out the ModSync source from `MODSYNC_RS_REPOSITORY` or
   `MODSYNC_TS_REPOSITORY` using `MODSYNC_SOURCE_TOKEN`.
2. Restores `game-<game>-v1` (a miss fails the lane) and runs `game_cache.py
   unpack`, which verifies every file. See [steam.md](steam.md).
3. Restores archives with `cache.py restore --archive-map`. This is best effort; a missing
   archive fails the install step that needs it. See [cache.md](cache.md).
4. Runs `ports.py`, passing `--review-manifest ci/<game>-<impl>-review.json` when
   that file exists. TypeScript requires it; Rust ignores it.

Lanes run on Linux only: the adapters and GUI drivers are POSIX and Xvfb only.

## Running locally

Python 3.11+, from the repository root:

```sh
python3 tools/modsync-ci/ports.py \
  --implementation rust --mode cli \
  --source /private/ModSync.rs --guide content/k1/full.md \
  --game-dir /private/lane/game --archives /private/lane/archives \
  --work-dir /private/lane/work --report /private/rust-cli.json
```

Supply a clean game and a lane-local copy of the archives; installers can modify
their source directory. Use separate directories per lane. Work and report paths
must not overlap any input. The adapter never acquires the game or downloads mods.

## What each implementation runs

Every run builds the port and converts the Markdown guide afresh; existing TOMLs
are never used. Before validation the adapter checks for a nonempty Mod List,
matching section and component counts, and instructions on every component. No
skip, best-effort or auto-confirm flag is passed, and failed steps are never
backfilled.

Rust (`cargo build --locked --bin modsync`, isolated target directory):

```text
convert full.md converted.toml --format toml --parse-directions
validate --input converted.toml --game-dir GAME --source-dir ARCHIVES
install --input converted.toml --game-dir GAME --source-dir ARCHIVES --non-interactive --phase all
```

TypeScript (`pnpm build`, then the built CLI; the guide's parent directory selects
`kotor1` or `kotor2`):

```text
node packages/cli/dist/main.js import-md full.md --output converted.toml --game kotor1
node packages/cli/dist/main.js validate converted.toml --kotor GAME --mods ARCHIVES --all
node packages/cli/dist/main.js install converted.toml --kotor GAME --mods ARCHIVES --all --phase base [--choice GUID=ANSWER ...]
node packages/cli/dist/main.js install converted.toml --kotor GAME --mods ARCHIVES --all --phase widescreen [--choice GUID=ANSWER ...]
```

## TypeScript review manifest (schema 1)

A human-reviewed JSON file bound to one exact conversion. Without it the lane stops
with `review_manifest_required`.

| Field | Value |
| --- | --- |
| `schema_version` | `1` |
| `reviewer` | Nonempty string |
| `unresolved` | `[]` |
| `guide_sha256` | SHA-256 of the guide |
| `converted_sha256` | SHA-256 of the fresh converted TOML |
| `components` | One entry per converted component, in order |

Each component entry has `guid`, the exact `instructions` and `options` records,
`phase` (`base` or `widescreen`), a nonempty `assessment`, `unresolved: []` and
`choices` (instruction UUID → CLI answer). Every `Choose`, `Patcher` and `Execute`
instruction needs an answer. If the converter output changes, the manifest no
longer matches and must be reviewed again.

The check is structural: it matches records, hashes and UUID coverage. It does not
prove the review is correct and does not bypass the port's own install gates.

## GUI mode

GUI mode does the same build, conversion and validation, then hands off to a driver:

- `gui_rust.py` drives the Rust desktop app in Xvfb with OCR and clicks. Success
  needs visible completion plus a native completion receipt that matches the game
  tree.
- `gui_typescript.py` drives the built web UI in headless Chromium via Playwright.
  Success needs visible completion, no failed or skipped mods, and physical
  checkpoint receipts. Manifest choices are not forwarded to this driver.

## Output

Each run writes logs, the converted TOML and GUI evidence to a private `ports-*`
directory under the work directory. Reports hold reason codes, stage exit codes and
hashes, but GUI reports include private paths; review them before publishing.
Exit 0 means the port's own install completed. It is not, by itself, proof of
parity with the manual reference.
