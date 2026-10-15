# Private port CI adapter

Run with Python 3.11 or newer from the mod-builds root (or use an absolute
path to the script from another directory):

```bash
python3 tools/modsync-ci/ports.py \
  --implementation rust --mode cli \
  --source /private/ModSync.rs \
  --guide /guides/content/k1/full.md \
  --game-dir /private/lane/game \
  --archives /private/lane/archives \
  --work-dir /private/lane/work \
  --report /reports/rust-cli.json
```

Implementations are `rust` and `typescript`; modes are `cli` and `gui`.
Use separate game, archive and work directories for every concurrent lane.
The caller supplies a genuine clean game installation and verified archives.
The adapter does not acquire a game, download mods, or manufacture fixtures.
Archive directories must be lane-local writable copies: native installers can
extract or otherwise mutate their source directory. Never pass the shared cache.
Work must not overlap source, guide, game or archive roots. Reports must be new
files outside all input roots. No cache or Steam adapter is imported.

## Commands inspected in the current ports

Rust uses `cargo build --locked --bin modsync` with a unique `CARGO_TARGET_DIR`,
then that freshly built `target/debug/modsync`:

```text
convert full.md converted.toml --format toml --parse-directions
validate --input converted.toml --game-dir GAME --source-dir ARCHIVES
install --input converted.toml --game-dir GAME --source-dir ARCHIVES --non-interactive --phase all
```

Conversion always starts from the supplied Markdown, with a new output path;
existing TOMLs and merge inputs are never used. Before validation the adapter
requires a nonempty Mod List, equal Markdown section/component counts and
instructions on every component. These are conservative coverage checks, not
semantic proof. Rust's native validation/install gates remain authoritative for
draft review, missing sources, choices and unsupported operations. No review
receipt is created or modified, and no skip, best-effort or auto-confirm flag is
passed. Fresh prose drafts can still block on the port's native review gates;
this adapter does not perform native draft review or pass `--review-draft`.
`--review-manifest` is not consumed by the Rust branch and cannot authorize it.
It never backfills
failed steps. On installation failure preserve the lane and recover using the
port's verified snapshot protocol before any retry.

TypeScript uses `pnpm build`, then the freshly built CLI:

```text
node packages/cli/dist/main.js import-md full.md --output converted.toml --game kotor1
node packages/cli/dist/main.js validate converted.toml --kotor GAME --mods ARCHIVES --all
node packages/cli/dist/main.js install converted.toml --kotor GAME --mods ARCHIVES --all --phase base [--choice GUID=ANSWER ...]
node packages/cli/dist/main.js install converted.toml --kotor GAME --mods ARCHIVES --all --phase widescreen [--choice GUID=ANSWER ...]
```

The guide's immediate parent must be `k1` or `k2`, selecting `kotor1` or
`kotor2`. Dependencies must already be installed using the source repository's
pinned pnpm setup. Unlike Rust's isolated Cargo target, `pnpm build` writes
build outputs in the supplied source tree; keep that checkout private too.
After coverage, both TypeScript modes require `--review-manifest /private/review.json`
before native validation. CLI mode then runs base followed by widescreen,
passing the reviewed choices to each invocation. It stops on the first failure.
The adapter checks guide/converted hashes before each install phase. It does
not pass `--yes`, but the native CLI still defaults unanswered prompts when
stdin is not a TTY. The manifest's explicit-answer checks reduce this exposure;
they do not prove every possible runtime prompt is covered.

## TypeScript review manifest, schema version 1

The manifest is separately authored JSON bound to this exact conversion. Its
top-level fields are:

| Field | Required value |
| --- | --- |
| `schema_version` | `1` |
| `reviewer` | Nonempty string after trimming |
| `unresolved` | Empty array `[]` |
| `guide_sha256` | SHA256 of the supplied Markdown |
| `converted_sha256` | SHA256 of the fresh converted TOML |
| `components` | One review per converted component, in converted order |

Each component review requires:

| Field | Required value |
| --- | --- |
| `guid` | UUID matching the converted component's `Guid` |
| `instructions` | Exact parsed TOML `Instructions` records |
| `options` | Exact parsed TOML `Options` records, or `[]` if absent |
| `phase` | `widescreen` when `IsWidescreen` is truthy, otherwise `base` |
| `assessment` | Nonempty string after trimming |
| `unresolved` | Empty array `[]` |
| `choices` | Object mapping instruction UUIDs to nonempty string CLI answers |

Component UUIDs must be unique. Instruction UUIDs must be globally unique
across component instructions and each option's instructions. Choice UUIDs
are normalized; duplicate normalized keys and references outside that
component's instructions/options are rejected. Every `Choose`, `Patcher` and
`Execute` instruction requires an answer, including those inside options.
Other instruction answers may be supplied. Answers become repeated
`--choice UUID=ANSWER` arguments; the native CLI checks whether they are valid
for an encountered prompt. Missing manifests block with `review_manifest_required`.

This is a validator in `ports.py`, not an authenticated approval service or a
complete semantic schema. It checks record equality, hashes, UUID coverage and
nonempty review text; it does not verify reviewer identity, assessment quality,
prose fidelity, option suitability, executable safety or runtime answer validity.
Unknown fields are not rejected. It neither writes nor replaces native review
receipts, and passing it does not bypass native install gates.

Every adapter run builds and converts afresh before reading the manifest;
there is no pause/resume approval step or supplied-TOML mode. A practical review
starts with a blocked run, inspecting its private `converted.toml` and native
diagnostics, then separately reviewing those exact records and hashes. A later
run accepts that manifest only if its fresh conversion matches exactly. New
GUIDs or other changed converter output require a new review; editing the TOML
or reusing a stale manifest is not an approval workflow. Native draft review
and resolution of unsupported or ambiguous prose remain necessary.

## GUI drivers and their scope

GUI mode performs the same fresh build, conversion, coverage and native CLI
validation first, then invokes the sibling driver with a 7000-second budget.
These are real UI install routes, with receipt checks, rather than unconditional
`real_full_guide_gui_driver_unavailable` failures. Neither driver manufactures
approval or retries a failed install.

- **Rust:** `gui_rust.py` drives the production egui desktop in isolated Xvfb
  using OCR and clicks. It requires `xvfb-run`, `Xvfb`, `xauth`, `tesseract`,
  `pyautogui`, `pytesseract` and Pillow. It selects all components, validates,
  creates a snapshot and installs. It rejects unresolved `DraftReview` records,
  inputs containing `WidescreenOnly`, and existing completion/pending recovery
  state. Success requires visible completion plus a native completion receipt
  bound to game/archive roots, all input component identities, and an exact
  hash-verified game file set without symlinks. Separate widescreen phases need
  another GUI route; this driver cannot authorize draft review.
- **TypeScript:** `gui_typescript.py` launches the production server and built
  web UI on loopback, driving headless Chromium through Playwright. This is
  `playwright-web` evidence, not desktop-shell evidence. It requires Node, the
  built web app, `tsx`, Playwright and an installed Chromium runtime. It uses
  the TOML's existing selections/options rather than CLI `--all`, and blocks
  on no selections, selected widescreen/Aspyr-only components or runtime prompts.
  Manifest choices are checked by the adapter but are **not forwarded to the
  GUI driver**. Success requires visible completion, zero failed/skipped mods,
  exact selected-component accounting and physical checkpoint receipts with
  tracked output/deletion checks. The adapter additionally verifies evidence
  artifact hashes and listed game output hashes. These checks cover tracked
  outputs, not the entire game tree or every guide component.

Direct driver invocation accepts a supplied TOML but does not itself build,
convert or establish full Markdown coverage. The adapter's count/instruction
checks also do not prove correct selections, phases or full guide semantics.

## Logs, results and limits

Each invocation creates a random `ports-*` directory beneath work, mode 0700.
Raw build/conversion/validation/install output goes to stage logs there;
TOML and Rust build artifacts stay there too. GUI evidence includes private
screenshots, OCR or loaded data, settings and receipts beneath driver work.
Files are created under umask 077.
Treat the entire work directory and native game/archive outputs as private;
never upload them as public CI artifacts. CLI reports contain reason codes,
implementation, mode, stage exit codes, guide and converted-document SHA256
hashes, and an optional component count, without raw commands or mod names.
GUI reports also embed the driver result, including its absolute private
`evidence_directory`, receipt hashes and completion counts. Do not publish
those reports unchanged under an assumption that all paths are sanitized;
review/redact them before public upload. Stage logs remain private.

Exit 0 means the native CLI command(s) succeeded or the GUI driver's scoped
completion checks passed, not manual-oracle parity, live-game acceptance or
proof of every prose requirement. CLI success is based on process exit codes;
it does not perform the GUI receipt/output verification. Exit 1 is a blocked or
failed lane; exit 2 is invocation/report failure. Each subprocess has a two-hour
deadline and closed stdin. Interrupted/timed-out process groups are terminated;
installation side effects are retained for recovery, never silently retried.
The presence of these drivers is not an accepted full-install result. Acceptance
still requires a real clean-game lane, verified archives, a fresh converter
result reviewed against all guide prose/options, native review gates satisfied,
complete ordered phase/step evidence and the required parity/live-game checks.
This document reflects source inspection of `ports.py`, `gui_rust.py`,
`gui_typescript.py` and the current port CLI contracts; no live full-guide
installation was performed for this documentation update.
