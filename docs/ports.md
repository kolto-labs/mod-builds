# Port install lanes

The `install` job in `.github/workflows/ci.yml` runs `tools/modsync-ci/ports.py`
for every combination of game (`k1`, `k2`), implementation (`rust`, `typescript`)
and mode (`cli`, `gui`).

## In CI

Each lane:

1. Checks out the ModSync source from `MODSYNC_RS_REPOSITORY` or
   `MODSYNC_TS_REPOSITORY` using `MODSYNC_SOURCE_TOKEN`. Rust uses the repository
   default branch; TypeScript uses `vars.MODSYNC_TS_REF` (workflow env
   `MODSYNC_TS_REF`, default `feat/physical-install`), the unmerged ModSync.ts branch
   that adds opt-in physical install.
2. Restores `game-<game>-v1` (a miss fails the lane) and runs `game_cache.py
   unpack`, which verifies every file. See [steam.md](steam.md).
3. Restores archives with `cache.py restore --archive-map`. This is best effort; a missing
   archive fails the install step that needs it. See [cache.md](cache.md).
4. TypeScript lanes install Wine (`wine wine64 wine32:i386`, no recommends): `Execute`
   and the legacy TSLPatcher run through `wine` outside Windows.
5. Runs `ports.py`, passing `--review-manifest ci/<game>-<impl>-review.json` when
   that file exists. TypeScript requires it; Rust ignores it. Every lane also passes
   `--restore-report`, the `cache.py restore --report` JSON of step 3 (see
   [Reviewed policy skips](#reviewed-policy-skips)); the restore step passes the same
   `--allow-uncached-by-policy` step flags to all implementations.

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
convert full.md converted.toml --format toml --parse-directions --auto-generate-local --source-path ARCHIVES [--archive-map MAP]
convert full.md converted-bound-pending.toml --format toml --parse-directions --source-path ARCHIVES --resolved-archives resolved-archives.json   # with --archive-map; see below
convert converted-bound-pending.toml converted-bound.toml --format toml --source-path ARCHIVES --auto-generate-local --archive-map MAP
convert converted[-bound].toml converted-reviewed.toml --format toml --source-path ARCHIVES --review-verified-drafts
validate --input converted.toml --game-dir GAME --source-dir ARCHIVES [--deselect GUID ...]
install --input converted.toml --game-dir GAME --source-dir ARCHIVES [--deselect GUID ...] --non-interactive --phase all
```

`validate` takes the same `--deselect GUID` flags (see below).

The two middle `convert` lines are the archive binding pass and run only with
`--archive-map`; see [Archive binding](#archive-binding). The last `convert` reads
`converted-bound.toml` when the binding pass was used, otherwise `converted.toml`, and
replaces `converted.toml` with `converted-reviewed.toml` before it is hashed. See
[Verified draft review](#verified-draft-review).

### Archive binding

ModSync.rs `1c67994` also lets `--review-verified-drafts` clear a method-bound loose-file
draft that `convert --resolved-archives` wrote (`bind_loose_method`, replayed by the
verifier). Every component of the real guides has an `InstallationMethod`, so this is the
only draft the verifier can clear there. `--resolved-archives` binds only a method-only
loose-file component that has no instructions yet and conflicts with
`--auto-generate-local`, so the pass runs **before** local generation, from the guide:

1. The plain `convert` runs as before and its `converted.toml` is kept as the fallback.
2. `ports.py` builds `resolved-archives.json` from `converted.toml` and the
   `--archive-map` JSON: `[{componentGuid, filenames: [file], sha256, sizeBytes}]`. A
   component is listed only if **every** link in its `ResourceRegistry` maps (links are
   compared without query, fragment and userinfo, as `cache.py` keys them) to the same
   **one** file, that file is a `.zip`/`.7z`/`.rar` regular file (not a symlink) with a safe
   literal name in the archive directory, and the GUID appears once. Multi-archive,
   partly mapped and unmapped components are skipped. The hash and size are computed from
   the file, and the converter rechecks them. Nothing is listed, and no pass runs, when no
   component qualifies.
3. `bind_archives`: `convert full.md converted-bound-pending.toml --parse-directions
   --source-path ARCHIVES --resolved-archives JSON`.
4. `bind_generate_local`: `--auto-generate-local --archive-map MAP` on that output, so
   every component the binding did not take still gets its generated instructions.
5. The result is used only if both steps exit 0, no component or option was lost, no
   component lost its instructions, draft record or prose-draft warning, and no unbound
   component lost instructions. Otherwise `converted.toml` is kept, the lane continues,
   and `draft_review.bind_failed` holds the reason (`bind_archives_failed`,
   `bind_generate_local_failed`, `bound_output_drops_instructions_or_drafts`,
   `bound_output_components_changed`, `archive_map_unreadable`, ...). An interrupt or
   timeout still stops the lane.

`draft_review.bound` is the number of components the pass bound (`Generator =
"resolved-filenames"`); it is 0 when the pass did not run. Binding happens after
`convert` generated instructions, so the order matters: a binding pass on the finished
`converted.toml` binds 0 of 139 on k2, because the generated instructions already make
every component ineligible.

### Verified draft review

`--review-verified-drafts` (ModSync.rs `aa7b355`) clears `DraftReview.RequiresReview`
only for a draft that a fresh replay of its one hash-bound local archive reproduces
exactly, and records a `Verification` receipt. Prose, method-bound, gapful, edited,
bare-filename and multi-archive drafts stay fenced. `ports.py` never sets
`RequiresReview` itself; this tool is the only automatic clearing.

- **Report.** The lane report gets `draft_review`: `bound` (see [Archive binding](#archive-binding)),
  `cleared` (a count) and `skipped`
  (`component_guid`, `name`, `reason` of each draft left fenced, parsed from the
  `{cleared, skipped}` JSON the converter prints on stderr into
  `review_verified_drafts.log`). The parse is kept even when the step fails.
- **Block before validate.** After coverage and the reviewed-policy/widescreen scope are
  known, and before `validate`, the lane stops with `unreviewed_draft_instructions_remain:
  <guid> (<heading>); ...` when any component it would install still has an unreviewed
  draft. The reason and `draft_review.unreviewed` list every such component (an option
  is listed as `Owner / Option` with its own GUID); stderr repeats them. Reviewed-policy
  skips and, for the GUI lane, widescreen-only components excluded from it are never
  checked.
- **What "installed" means here.** The CLI lane checks components with `IsSelected =
  true` (all when none is selected, as the Rust loader does) and their selected options.
  The GUI lane installs every non-skipped, non-excluded component, so it checks all of
  them and all nested options, like `gui_rust.py`.
- **What counts as unreviewed.** `DraftReview` without `RequiresReview = false`, a
  `Result` that is missing, names another component or still lists gaps, conditions or
  rejected drafts, or (without `DraftReview`) the prose-draft `InstallationWarning`. The
  reviewed-identity receipt is verified only by the Rust `validate`/`install`.
- Rust lanes only; TypeScript lanes do not run it.
- **What it clears on the real guides.** The verifier accepts a draft whose `DraftReview`
  has `Generator = "resolved-filenames"` and a `Derivation`, with no gaps and no prose-draft
  warning. It does not accept an `--auto-generate-local` draft (no `Generator`/`Derivation`),
  so with `aa7b355` alone nothing cleared (0 of 151 on k2; k1 was not measured). From `1c67994` it
  also accepts the `MethodContext` that `bind_loose_method` wrote, which is why the
  [archive binding pass](#archive-binding) exists. Prose drafts, patcher-method components,
  multi-archive and unmapped components stay fenced and still stop the lane through the
  block below.
- **Measured (k2, ModSync.rs `1c67994`, real `content/k2/full.md`, synthetic one-file
  archives for the 139 uniquely mapped components, not the real archives).** The pass
  bound 40 and the review cleared 40; 111 stayed fenced. Instruction counts were unchanged
  (147 of 151 components, 267 instructions: 42 components from the guide's own steps plus
  105 generated). The same run without the pass cleared 0. The real archives were not
  available, so the real count depends on their contents (the replay must reproduce the
  bound instructions exactly) and may differ from 40.

### Components deselected pending human review

CI run 39136954201 stopped every Rust install lane at the `draft_review` gate: the
review clears only drafts a replay of one hash-bound archive reproduces, so prose,
multi-archive and executable-method drafts stayed unreviewed (k2 CLI 118, k2 GUI 113,
k1 CLI 150, k1 GUI 142 components; the GUI lists are the CLI lists without the
widescreen-only components it excludes, so the CLI list is the union). On the repository
owner's explicit instruction (2026-10-18, delegating the review sign-off), those
components are **omitted from the Rust install lanes** instead of being approved, so the
remainder installs:

- They are committed `lane_skips` entries (see [Lane skips](#lane-skips-steps-that-need-no-restore-report))
  carrying `guid` and `kind`. Nothing is reviewed or approved: the drafts keep
  `RequiresReview = true`, the verifier is unchanged, and the lane still deselects them with
  `--deselect <Guid>` for `validate` and `install` (and `gui_rust.py --deselect`). The
  reason of each entry says `deselected pending human review, delegated by owner 2026-10-18`.
- **Counts.** k1: 150 `unreviewed_draft` entries (8 are widescreen-only). k2: 118
  `unreviewed_draft` (5 widescreen-only) plus 1 `dependent_of_deselected`, `Extended
  Korriban Arrival`, which depends on the deselected `TSLRCM`; the Rust selection cascade
  (`--deselect` also deselects every selected dependent) would drop it silently, so it is
  listed. Each game also keeps its hand-authored `4GB Patcher` entry; k2 additionally
  skips `K2 Cutscenes Rescaled` through the restore report. Measured with a
  pre-verified-review Rust build (`validate --deselect` of the whole set on the
  `--parse-directions` conversion of the real guides), the cascade auto-deselects only
  components already in the set (k1: 4 of them, k2: 20), and no `Restrictions`,
  `InstallBefore`, `InstallAfter` or option reference points at a deselected component.
- **New drafts still block.** Only the listed GUIDs are exempt from the gate. Any other
  component with an unreviewed draft stops the lane with
  `unreviewed_draft_instructions_remain` exactly as before, and a pinned `guid` that no
  longer matches the converted component blocks (`policy_skipped_guid_mismatch`), so a
  changed converter or guide cannot silently retarget a skip.
- **Dependents.** A skipped component with a dependent that is not itself skipped still
  blocks (`policy_skipped_component_has_dependents`); a dependent that is skipped too
  (every dependent of a listed component in the k1 and k2 guides except the one above) is
  fine.
- **Visible summary.** The lane report has `deselected_pending_review`: `count`,
  `by_kind` (`unreviewed_draft`, `dependent_of_deselected`) and one record per component
  (`component_guid`, `heading`, `kind`). `ports.py` repeats it on stderr in the CI log:
  `::warning::ports: N component(s) DESELECTED pending human review ... ` followed by one
  `deselected pending review: <heading> [<guid>] (<kind>)` line per component. These
  components are also in `skipped_by_reviewed_policy`, so the GUI lane never reports them
  as covered; the GUI `gui_covered_components` count excludes them. Remove an entry (or
  review the draft and rerun the generator) to put a component back.
- **Reproducible generation.** `tools/modsync-ci/deselect_unreviewed.py` writes the
  entries from the log lines `ports: unreviewed draft still selected: <guid> (<name>)`:

  ```text
  deselect_unreviewed.py --game k2 --guide content/k2/full.md --converted converted.toml \
      --log install-cli.log --log install-gui.log [--write | --check]
  ```

  `--converted` is any `convert --parse-directions` output of the same guide (GUIDs and
  `Dependencies` do not depend on archives). The tool maps each GUID to its
  `<## section>::<### heading>` step, refuses GUIDs or names the guide does not have, adds
  the transitive dependents, keeps entries without a `kind` (the `4GB Patcher`), and
  replaces the kind-marked ones. Without `--write` it prints the result; `--check` exits 1
  when `archive_selections.json` differs.

TypeScript (`pnpm build`, then the built CLI; the guide's parent directory selects
`kotor1` or `kotor2`):

```text
node packages/cli/dist/main.js import-md full.md --output converted.toml --game kotor1
node packages/cli/dist/main.js validate converted.toml --kotor GAME --mods ARCHIVES --select GUID,GUID,...
node packages/cli/dist/main.js install converted.toml --kotor GAME --mods ARCHIVES --select GUID,GUID,... --phase base [--choice GUID=ANSWER ...] --ci-physical-install
node packages/cli/dist/main.js install converted.toml --kotor GAME --mods ARCHIVES --select GUID,GUID,... --phase widescreen [--choice GUID=ANSWER ...] --ci-physical-install
```

`--select` lists every converted component except the reviewed policy skips (see
[Reviewed policy skips](#reviewed-policy-skips)), lowercase and hyphenated. ModSync.ts
has no `--deselect`: `--select` selects exactly the GUIDs given and turns every other
mod off (`applySelectionFlags`), and it does not pull dependencies in, so a component
that depends on a skipped one fails `validate` with `dependency-missing` (the lane stops
earlier with `policy_skipped_component_has_dependents`). Without skips the list is
every component, as `--all` was.

`--ci-physical-install` is passed to both TypeScript `install` commands (`cli` mode)
and nowhere else. Without it (or `MODSYNC_CI_PHYSICAL_INSTALL=1`) ModSync.ts refuses
any non-dry-run install. It exists only on the ModSync.ts `feat/physical-install`
branch, which the TypeScript lanes check out by default; on the repository default
branch the option is unknown and the install fails. With the flag, ModSync.ts uses a
portable, advisory lockfile in place of native OS ownership, and still refuses
`CreateDirectory` and canonical authored guides. The TypeScript `gui` mode runs
`gui_typescript.py` instead of these commands and does not pass the flag. The lanes
still need the committed `ci/<game>-typescript-review.json`. `validate` and the CLI
`install` read `IsSelected` from nowhere (they use `--select`); the `gui` lane has no
select flag and reads `IsSelected` from its input, so with policy skips it gets
`converted-policy.toml`, a copy of the reviewed TOML in which the skipped components are
also `IsSelected = false` (`report.selected_after_policy_count`). The review hash is
bound to `converted.toml` (before that copy), so it does not depend on the restore
report. The GUI input hash is checked against the copy.

### Runtime quarantine of validate failures (Rust lanes only)

CI run 39141977764 got past the draft-review gate and then stopped the k2 CLI and GUI
lanes in `validate` at `Error: Ebon Hawk Downloadable Map / step 2 (Patcher): File blocks
destination directory: .../Modules/003ebo.mod`. On the owner's standing instruction
(omit what cannot install, visibly, so the remainder installs), the Rust `validate` step
now runs in a bounded quarantine loop. Nothing is quarantined statically: the committed
tables (`archive_selections.json`) are unchanged, and a component only enters the list
because `validate` named it in a failing run.

1. `validate` runs with the normal `--deselect` list. When it exits nonzero, `ports.py`
   reads `validate.log` for the last `Error: <Component> / step N (<Action>): <message>`
   line.
2. The component name must match exactly one converted component (`Name` or `Heading`;
   a case-insensitive match is accepted only when exact matching finds nothing). Its
   GUID, and the GUID of every selected component that depends on it, directly or
   through others (`Dependencies`, the same relation the
   `policy_skipped_component_has_dependents` check uses), are added to the shared
   `--deselect` list. Dependents are listed individually as `dependent_of_quarantined`.
3. `validate` is re-run as `validate_retry_1`, `validate_retry_2`, ... (each stage has its
   own log). The final `--deselect` list, including the quarantined GUIDs, is what `install`
   and, for the GUI lane, `gui_rust.py --deselect` receive. The GUI lane counts quarantined
   components as skipped, never covered (`coverage` is `subset`).

The loop is bounded: at most `--max-quarantine` re-runs (default 15; `0` disables the loop) and at
most `--max-quarantine-fraction` (default 0.15) of the **selected** components in total,
failing components plus dependents, rounded down. "Selected" excludes the reviewed-policy
and GUI-widescreen deselects, so it is small on the current guides (k2 has about 30
installable components, so at most 4 can be quarantined); raise the fraction only
deliberately.

The lane still **hard-fails** (`validate_failed`, or `validate_quarantine_cap_exceeded` for the
caps) when:

- the error names no component (`error_not_attributable`: a panic, `No components are
  selected`, an empty log);
- the name matches no component (`component_unknown`) or more than one
  (`component_ambiguous`);
- the component is already deselected, which would loop (`component_already_deselected`);
- the game directory is missing or empty, or the message says the game directory or
  `chitin.key` is missing (`game_missing`);
- either cap would be exceeded (`iteration_cap_exceeded`, `component_cap_exceeded`; the
  offending component is not quarantined), or quarantine is disabled
  (`quarantine_disabled`).

Restore, convert, coverage and draft-review problems happen before `validate` and are
never quarantined. TypeScript lanes do not use the loop. `install` failures are not
quarantined either: a failed install has already changed the game directory (the K1 CLI
stop `Helena Shan Improvement / step 2 (Patcher): Built-in OdyPatcher reported errors;
restore before retrying` is of that kind), so retrying needs a restored game, which
`ports.py` does not do. Its component and the tool's output are recorded and printed (see
[Failure evidence](#failure-evidence)) instead.

**A green lane with quarantined components is a partial install.** The quarantined
components were not installed and not verified, so the lane is not guide parity, and it
is less complete than the reviewed-policy subset alone. Every one is reported:

- The lane report has `quarantined_validate_failures`, one record per component: `kind`
  (`validate_failure` or `dependent_of_quarantined`), `name`, `component_guid`, `step`,
  `action`, `error_line` (scrubbed, one line), `stage` (the `validate` run that failed)
  and `caused_by` (the failing component's GUID, for dependents). It is an empty list when
  nothing was quarantined. `quarantine` records the caps, the selected-component count, the
  `attempts` and, when the loop refused, `stopped` (`reason`, `component`, `step`,
  `error_line`).
- The CI log prints `::warning::ports: N component(s) QUARANTINED after validate failures
  ... PARTIAL install` and one `::warning::ports: quarantined <name> [<guid>]: validate
  failed at step N (<action>): <error line>` (or `depends on quarantined <guid>`) per
  component, and an `::error::` line when the loop refused.

To bring a component back, fix its instructions (or archive) and rerun; to make an
omission permanent and reviewed, add it to `lane_skips` as for any other skip.

### Failure evidence

The CI log keeps only the last 80 lines of a failed command, which hid the three patcher
errors of the K1 `install` failure. When any command fails, `ports.py` additionally
prints a `::group::<stage> failure evidence` excerpt of the full log: every `Error:` /
`Exception:` / `[Error]` line with up to 60 lines before it and, after the first
`Install stopped` / `CLI command stopped` marker (the marker included), every line that
contains `error` or `failed`; gaps are shown as `[...]`, at most 200 lines are kept (the
latest win, with a count of omitted lines first), lines are cut at 600 characters, and
ANSI escapes, `key=value` secrets, bearer tokens, URL credentials and signed-URL
parameters are scrubbed. The failing component is also written to the lane report as
`failure` (`stage`, `exit_code`, `component`, `step`, `action`, `error_line`; the component
fields are `null` when the output names none) and echoed as `ports: <stage> failed at
component '<name>', step N (<action>)` and a final `::error::ports: <stage> failed ...`
line.

## TypeScript review manifest (schema 1)

A JSON file bound to one exact conversion. Without it the lane stops with
`review_manifest_required`.

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
longer matches and must be regenerated (see [Regenerating](#regenerating-the-delegated-manifests)).
The `phase` rule is `ports.component_phase` (`widescreen` if `IsWidescreen`, else `base`),
used by `review_manifest` and `draft_ts_review.py` alike.

The committed `ci/k1-typescript-review.json` and `ci/k2-typescript-review.json` are
**delegated**, not hand-reviewed: the owner delegated the review sign-off to the
orchestrator on 2026-10-18 (`reviewer` is `delegated-by-owner (orchestrator, 2026-10-18)`)
so the rest of the guide can install. Every answer is a documented default, stated in
each `assessment`: `Patcher` `0` (the first `namespaces.ini` entry, the namespace
prompt's own default), `Execute` `yes` (inert: ModSync.ts's `Execute` never prompts).
There is no `Choose` in either conversion; the generator refuses one. Components omitted by
the reviewed policy stay listed with the assessment `omitted by reviewed policy`.

The check is structural: it matches records, hashes and UUID coverage. It does not
prove the review is correct and does not bypass the port's own install gates.

### Drafting a review manifest

`tools/modsync-ci/draft_ts_review.py` prepares the manual work; it never approves anything:

```bash
python3 tools/modsync-ci/draft_ts_review.py PORTS_WORK/ports-*/converted.toml content/k1/full.md
```

It writes `ci/drafts/<game>-typescript-review.draft.json` and
`<game>-typescript-review.checklist.md`. The draft carries the hashes, component GUIDs and
the verbatim `instructions` and `options` records, and `phase` from
`ports.component_phase`. `reviewer`,
`unresolved`, `assessment` and every `choices` answer are placeholders, and the draft has
`draft: true`, so `ports.review_manifest` blocks it. A human completes it, removes the
`draft*` keys and commits it as `ci/<game>-typescript-review.json`; the tool refuses to
write that path or anything under `ci/`. Use the TOML the lane itself hashed
(`converted_sha256` in its report), not a fresh conversion.

### Regenerating the delegated manifests

`converted_sha256` is the hash of `import-md` output plus `select_base_components`, so it
depends on the guide and on the ModSync.ts ref the workflow builds (`MODSYNC_TS_REF`,
default `feat/physical-install`; repository variable unset at the time of writing). A moved
ref can change it; the lane then stops with `review_manifest_hash_mismatch: converted_sha256
differ ...`, and the message names the fix. From the repository root:

```bash
git clone --branch "$MODSYNC_TS_REF" https://github.com/bodencrouch/ModSync.ts.git ts-ci-ref
(cd ts-ci-ref && pnpm install --frozen-lockfile && pnpm build)    # pnpm 10.33.0, Node 22
python3 tools/modsync-ci/gen_ts_review.py --source ts-ci-ref           # k1 and k2, writes ci/
python3 tools/modsync-ci/gen_ts_review.py --source ts-ci-ref --check   # exit 1 when stale
```

`gen_ts_review.py` converts exactly as the lane does, drafts through
`draft_ts_review.build`, fills the defaults above, drops the `draft*` keys, validates with
`ports.review_manifest` and writes `ci/<game>-typescript-review.json` (deterministic, so a
diff shows what the new ref changed). Review that diff; the defaults are only as good as
the conversion. `--converted FILE` skips the conversion for a TOML the lane already
produced. Conversion is byte-reproducible for a fixed ref and guide.

## Reviewed policy skips

All lanes restore with the reviewed `--allow-uncached-by-policy` step flags
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
- **Selection.** Rust: `validate` and `install` receive `--deselect <component Guid>`
  for exactly those components. The Rust CLI selects every top-level component by
  default and does not read `IsSelected` (no `--use-file-selection`), so the flag is
  the control; `convert` has no deselect option and is unchanged. TypeScript: `validate`
  and both `install` phases receive `--select` with every other component's GUID instead
  of `--all` (see above), and the GUI input copy marks the skipped ones unselected.
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
table, read by `ports.py` (no restore report needed):

```json
"lane_skips": {"k1": [{"section": "Mod List", "heading": "4GB Patcher", "reason": "..."}],
               "k2": [{"section": "Mod List", "heading": "4GB Patcher", "reason": "..."}]}
```

- **Schema.** Keys are `k1` or `k2` (the guide folder name); each value is a list of
  objects with `section`, `heading` and `reason`, all non-empty strings
  without surrounding whitespace, plus optionally a `guid` (lowercase GUID) and a `kind`
  (`unreviewed_draft` or `dependent_of_deselected`), given together; see
  [Components deselected pending human review](#components-deselected-pending-human-review).
  A pinned `guid` must equal the converted component's GUID (`policy_skipped_guid_mismatch`)
  and be unique in the table. A malformed table, an unknown game key, or a
  repeated step blocks the Rust lane (`lane_skips_invalid`, `lane_skips_duplicate`)
  whichever game is being run; a missing file or table skips nothing.
- **Scope.** Only the exact `<## section>::<### heading>` is skipped. A step that is
  not in the guide, appears twice in the counted sections, or has a dependent
  component that is not itself skipped blocks the lane, exactly like a restore-report skip. Every other step
  still needs instructions.
- **Same path as restore-report skips.** The entries are merged with the restore
  report's `skipped_steps` (a step named by both is reported once, as the restore
  report's), exempted from coverage, reclassified in `convert_skips` as
  `skipped_by_reviewed_policy` (so the converter's rejection is not counted as a
  `reader_rejection`), recorded in the lane report's `skipped_by_reviewed_policy`
  (lane-skip records carry `"source": "lane_skips"` and `urls: []`), and passed to
  `validate` and `install` as `--deselect <component Guid>`.
- **TypeScript reads only the unpinned entries.** Entries with a `guid`/`kind` pin record the
  Rust converter's unreviewed-draft state and name Rust GUIDs, so TypeScript lanes skip
  them (the whole table is still validated). They therefore apply the `4GB Patcher` skip
  (the guide-order and Steam-executable reasoning holds for either converter; the reason
  text mentions the Rust converter) plus the restore-report skips, and install the rest.
  TypeScript's own conversion generates instructions for the other 150/119 components, so
  they are not omitted. The Rust `gui` lane applies the table as a GUI subset (see
  [GUI mode](#gui-mode)).

`--restore-report` is accepted by both implementations. The Rust `gui` lane forwards the
same GUIDs to `gui_rust.py --deselect` (see [GUI mode](#gui-mode)); the TypeScript lanes
use `--select` of the remainder.

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
- `draft_review`: `bound`, `bind_failed` (only when the binding pass was not used),
  `cleared`, `skipped` and, when the lane stops on it, `unreviewed` (see
  [Archive binding](#archive-binding) and [Verified draft review](#verified-draft-review)).
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
parity with the manual reference, and for the Rust lanes it is a partial install
whenever the report lists `deselected_pending_review`, `skipped_by_reviewed_policy` or
`quarantined_validate_failures` entries (see
[Runtime quarantine](#runtime-quarantine-of-validate-failures-rust-lanes-only)).
