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
   that file exists. TypeScript requires it; Rust ignores it. Rust lanes also pass
   `--restore-report`, the `cache.py restore --report` JSON of step 3 (see
   [Reviewed policy skips](#reviewed-policy-skips)).

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
validate --input converted.toml --game-dir GAME --source-dir ARCHIVES [--deselect GUID ...]
install --input converted.toml --game-dir GAME --source-dir ARCHIVES [--deselect GUID ...] --non-interactive --phase all
```

`validate` takes the same `--deselect GUID` flags (see below).

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

## Reviewed policy skips

Rust lanes restore with the reviewed `--allow-uncached-by-policy` step flags
(see [ci.md](ci.md#install-lanes-and-uncached-links)); the restore only asserts
the link is skipped. `ports.py --restore-report restore-report.json` makes the lane
guarantee it. It reads the report's `skipped_steps` (refusing a report that is not
`ok` or names another guide) and, for each exact `<## section>::<### heading>`:

- **Coverage.** The guide's component count and heading order checks are
  unchanged. Only the converted component for that step is exempt from
  `guide_steps_without_instructions` (it has none: no archive was restored for it,
  so the converter cannot draft instructions). Every other component still needs
  instructions. A step that is not a heading of the guide, appears twice in the
  counted sections, or whose component another component lists in `Dependencies`
  blocks the lane.
- **Selection.** `validate` and `install` receive `--deselect <component Guid>`
  for exactly those components. The Rust CLI selects every top-level component by
  default and does not read `IsSelected` (no `--use-file-selection`), so the flag is
  the control; `convert` has no deselect option and is unchanged.
- **Report.** The lane report gets `skipped_by_reviewed_policy`: one record per
  step with `status: "skipped-by-reviewed-policy"`, `section`, `heading`, the
  restore `reason`, `urls`, and `component_guid` (`null` when the converter makes
  no component for the step).

Measured on the real guides with the Rust converter (`--parse-directions`; K1 has
197 components, K2 151). The K2 `K2 Cutscenes Rescaled` skip is a reviewed
decision (never cache `.bik` video; the step only installs movies, nothing depends
on it, and the guide says to use only one of the two K2 cutscene mods, so the
pinned Nexus 1066 alternative is the one kept):

| Step | Converter output |
| --- | --- |
| `Quicker TSLPatching` (K1, K2) | Not a component. Its `##` section is outside the counted sections (`Mod List` and the two widescreen sections), so it adds no heading and no component; recorded only, no deselect |
| K2 `K2 Cutscenes Rescaled` (DeadlyStream 2503) | Component 147 of 151 (`Enhanced Widescreen Support Mods`, immediately before `K2 Remastered Cutscenes`, 148): `IsSelected = true`, `WidescreenOnly = true`, no `Instructions`, two spurious prose-derived `Options` (`resolution`, `both the main`, both unselected and instruction-free), `DraftReview.RequiresReview = true` with `DraftInstructionCount = 0`. Its GUID appears nowhere else in the converted TOML: no component lists it in `Dependencies`, `Restrictions`, `InstallBefore` or `InstallAfter`, so the dependents and ambiguity checks pass on the real guide and exactly one `--deselect <Guid>` is emitted. Selected, validate stops with `unreviewed draft instructions`; deselecting it adds no auto-deselection of any other component (compared against the same run without it, with every other component deselected). The alternative `K2 Remastered Cutscenes` (Nexus 1066, component 148) is a separate component with its own GUID, is not skipped and is not deselected |
| K1 `Remastered Cutscenes` | Component 197 of 197: `IsSelected = true`, `WidescreenOnly = true`, no `Instructions`, no `Options`, `DraftReview.RequiresReview = true` with `DraftInstructionCount = 0`. Even with `--auto-generate-local` and an archive map it stays instruction-free (`No matching archive found for '1306'`). Selected and not deselected, validate stops with `unreviewed draft instructions`; deselected on its own, validate reports `No components are selected` |

### Lane skips: steps that need no restore report

Some reviewed skips are not about an uncached link. The `4GB Patcher` step (K1 and
K2, `Mod List`) restores fine (`direct_links` pins `4gb_patch.zip`), but that zip
holds only the vendor's Windows patcher `.exe`. The Rust converter rejects it
(`No supported game files or patcher data found in mapped archive`), so the
component never gets instructions and coverage failed with `1 guide steps have no
instructions` (K2 Rust CLI run 38979799826; K1 passed only because of an archive
matching tie). The guide also makes the step conditional: K2 says not to run it on
the Aspyr patch or after 3C-FD, K1 says to run it only after widescreen content and
never on the Steam executable unless the widescreen `.exe` was applied.

`tools/modsync-ci/archive_selections.json` therefore carries a small `lane_skips`
table, read by `ports.py` for the **Rust** lanes only (no restore report needed):

```json
"lane_skips": {"k1": [{"section": "Mod List", "heading": "4GB Patcher", "reason": "..."}],
               "k2": [{"section": "Mod List", "heading": "4GB Patcher", "reason": "..."}]}
```

- **Schema.** Keys are `k1` or `k2` (the guide folder name); each value is a list of
  objects with exactly `section`, `heading` and `reason`, all non-empty strings
  without surrounding whitespace. A malformed table, an unknown game key, or a
  repeated step blocks the Rust lane (`lane_skips_invalid`, `lane_skips_duplicate`)
  whichever game is being run; a missing file or table skips nothing.
- **Scope.** Only the exact `<## section>::<### heading>` is skipped. A step that is
  not in the guide, appears twice in the counted sections, or has a dependent
  component blocks the lane, exactly like a restore-report skip. Every other step
  still needs instructions.
- **Same path as restore-report skips.** The entries are merged with the restore
  report's `skipped_steps` (a step named by both is reported once, as the restore
  report's), exempted from coverage, reclassified in `convert_skips` as
  `skipped_by_reviewed_policy` (so the converter's rejection is not counted as a
  `reader_rejection`), recorded in the lane report's `skipped_by_reviewed_policy`
  (lane-skip records carry `"source": "lane_skips"` and `urls: []`), and passed to
  `validate` and `install` as `--deselect <component Guid>`.
- **Not applied elsewhere.** TypeScript lanes do not read the table (they have no
  deselect mechanism). The Rust `gui` lane applies it as a GUI subset (see
  [GUI mode](#gui-mode)).

`--restore-report` is refused for `--implementation typescript`
(`policy_skips_supported_for_rust_only`): those lanes have no deselect mechanism
and `validate --all` would select the step, so `ci.yml` does not pass the flags to
them. The Rust `gui` lane forwards the same GUIDs to `gui_rust.py --deselect`
(see [GUI mode](#gui-mode)).

## GUI mode

GUI mode does the same build, conversion and validation, then hands off to a driver:

- `gui_rust.py` drives the Rust desktop app in Xvfb with OCR and clicks. Success
  needs visible completion plus a native completion receipt that matches the game
  tree. It installs a defined subset (`coverage: subset`):
  - `--deselect GUID` (repeatable; `ports.py` passes each reviewed-policy skip) and
    `--exclude-widescreen` (always passed by `ports.py`). The supplied TOML is never
    edited: IsSelected is written for every component into a private copy
    (`input.toml` beside the evidence) and the app is launched on the copy. ModSync.rs
    loads it through `Guide::load_for_install`, which keeps authored `IsSelected`
    whenever at least one component is selected, so no Rust change is needed.
    The driver refuses a plan with nothing selected, because the app would then
    select everything.
  - The driver skips the `Select all` click when a base component is policy-skipped
    (`Select all` sets every base component selected again), and checks that the
    app's `Base build: N of M mods selected` line shows the expected count.
  - Components that remain selected must not depend on a deselected one
    (`selected_component_depends_on_deselected_component`).
  - Widescreen-only components are excluded, never installed: the report lists them as
    `excluded_widescreen` and `widescreen_phase: not_covered_by_gui_lane`
    (`none_in_input` if the input has none). Without `--exclude-widescreen`, a selected
    widescreen component is refused as before.
  - Draft review is checked on the components that remain selected, and the driver
    never authorizes one.
  - Expected receipt = all components minus `skipped_by_reviewed_policy` minus
    `excluded_widescreen`. `ports.py` requires `completed_components` and
    `expected_components` to equal that count, and the report's skipped and excluded
    lists to equal its own, and records `gui_not_covered` and `gui_covered_components`.
    The skipped and excluded components are never reported as covered.
- `gui_typescript.py` drives the built web UI in headless Chromium via Playwright.
  Success needs visible completion, no failed or skipped mods, and physical
  checkpoint receipts. Manifest choices are not forwarded to this driver.

## Diagnosing a coverage failure

`modsync convert --auto-generate-local` writes a JSON array to **stderr** (one entry per
component: `ComponentGuid`, `ComponentName`, `Success`, `InstructionsGenerated`,
`SkipReason`), even when it also exits nonzero. `ports.py` reads it back out of
`convert.log` after the convert step, whether or not the step failed, and records:

- `convert_skips`: `total`, `generated`, `by_cause` and `components` (guid, name, cause,
  converter `reason`; components that already had instructions are only counted).
- `steps_without_instructions`, when `guide_steps_without_instructions` blocks the lane:
  one record per guide step lacking instructions with `heading`, `component_guid`,
  `urls` (the component's links, with any `#fragment` removed because a MEGA fragment is
  the decryption key) and `skip_reason` (the converter's reason, `null` if it gave none).

Causes: `no_archive_matched`, `mapped_file_missing` (the `--archive-map` file is not in
the archive directory), `reader_rejection` (no supported files or patcher data, bounds,
unsafe names, archive links and special files, invalid UTF-8 names or streams),
`skipped_by_reviewed_policy` (a converter skip of a component that is exempt under
[Reviewed policy skips](#reviewed-policy-skips), matched by GUID, so it is never counted
as `no_archive_matched`), `patcher_path_refusal` (patcher tree without an identifiable installer
executable), `already_had_instructions`, `no_mod_links`, and `other` for wording this
table does not know.

The same information goes to stderr, so one CI log read is enough (reviewed-policy skips
are summarised separately as `; reviewed-policy-skipped=N`):

```text
ports: convert generated instructions for 0 of 197 components; not generated: already_had_instructions=44, no_archive_matched=153
::error::ports: 153 guide steps have no instructions
  - Example Mod [00000000-0000-0000-0000-000000000001] No matching archive found for 'example-mod' <https://example.invalid/files/1-example-mod/>
```

The `install` job seals `report.json` and every `convert.log` with `seal.py` and uploads
the result (`ports-diagnostics-<game>-<impl>-<mode>`, 7 days) even when the lane fails.
The key is the `DIAGNOSTICS_ENCRYPTION_KEY` secret, passed to `seal.py` as
`CACHE_ENCRYPTION_KEY` (at least 32 bytes). Without the secret, the step warns and nothing
is uploaded: never upload the plain files. To read one, run `seal.py unseal --source
lane-diagnostics.seal --destination DIR` with the same key.

## Output

Each run writes logs, the converted TOML and GUI evidence to a private `ports-*`
directory under the work directory. Reports hold reason codes, stage exit codes and
hashes, but GUI reports include private paths; review them before publishing.
Exit 0 means the port's own install completed. It is not, by itself, proof of
parity with the manual reference.
