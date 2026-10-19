# Manual reference lane

The `manual` job in `.github/workflows/ci.yml` installs the full K1 or K2 guide
literally, with each mod's original patcher, to produce a reference install that
the port lanes are compared against.

| Build | Guide |
| --- | --- |
| K1 | `content/k1/full.md` |
| K2 | `content/k2/full.md` |

## In CI

For each game:

1. Looks for `ci/<game>-manual-plan.json`. If it is absent the job emits a
   `::warning::` and a step summary ("manual lane skipped: no reviewed plan"), uploads
   the `draft_plan.py` skeleton as `manual-drafts-<game>` and ends green without
   installing anything. That is a visible, non-gating skip, never a pass: no manual
   evidence exists until a human-reviewed plan is committed. A plan that exists but
   fails validation, or a missing game or archive, still fails the job.
2. Installs Wine, Xvfb and archive tools.
3. Restores `game-<game>-v1` and runs `game_cache.py unpack`, which verifies every
   file, then `game_cache.py normalize-case` (lowercases `Modules/` and the file names
   in `modules/` and `override/`; the plan's installs see a case-normalized,
   Windows-equivalent tree), then prints the game inventory. See
   [steam.md](steam.md#case-normalization).
4. Restores archives with `cache.py restore` using the read-only cache credentials.
   Restore is fail-closed: if any archive of the guide is not in the cache (or fails
   verification), the step exits non-zero and the lane stops before any install
   step. This job never acquires new links; only the `mods` job fills the cache.
   See [cache.md](cache.md).
5. Runs `xvfb-run -a python tools/modsync-ci/manual.py --plan <plan> --guide
   content/<game>/full.md --game-dir ... --archives ... --work-dir ... --report ...`.

Linux runners only: `manual.py` is POSIX only, and Windows patchers run under Wine.

## The plan

`manual.py` executes a human-reviewed JSON plan. It never generates, infers or
approves operations itself. The full contract is in the docstring of
`tools/modsync-ci/manual.py`. In short:

- `guide_sha256` pins the guide; `archive_sha256` binds every archive input file.
- `steps` follow every `###` heading in `## Mod List`, once each, in order. Each
  step lists `operations`: `extract`, `copy`, `delete` or `patcher`, or is a
  reviewed [omission](#omitting-a-step-partial-plans).
- `supplemental_sections` gives a written review for every heading outside the
  Mod List and links any steps it affects.
- A `patcher` operation names the original patcher and its SHA-256, exact `argv`
  (optionally after an absolute `wine` path), working directory, success log,
  exact success markers, failure regexes, and expected output files or a required
  manifest change.

Use the mod's own original patcher. Do not substitute ModSync's `vendor/bin`
patchers, OdyPatcher or another HoloPatcher build.

## Omitting a step (partial plans)

When a step cannot be authored honestly yet (for example its original archive or a
verified patcher run is not available), the reviewer may omit it explicitly instead
of leaving the whole lane without a plan:

```json
{"id": "step-042", "source_heading": "### Example Mod", "omit": {"reason": "original patcher not yet verified under Wine", "reviewed_by": "Reviewer Name"}}
```

Rules, all enforced by `manual.py`:

- The step still names its exact `source_heading` and keeps its place in guide order.
  Every `###` step is either executed or omitted; a missing step is still an error.
- `omit` takes exactly `reason` and `reviewed_by`, both nonempty printable strings
  (no line breaks or control characters; they are echoed into the CI log).
- An omitted step has no `operations` (not even `[]`) and no `timeout_seconds`. Giving
  both `omit` and `operations` is an error, so a step can never be half-executed.
- At least one step must still execute; an all-omitted plan is rejected.
- Omission is never inferred or defaulted. `draft_plan.py` does not write `omit`
  entries, and a draft step with empty `operations` is still rejected.

Omissions are visible, not silent:

- Each omitted step prints a `::warning title=manual step omitted::` line naming its
  id, heading, reviewer and reason, followed by a
  `::warning title=partial manual install::` summary, at the start and again at the
  end of the run.
- The report records every step. Omitted ones have status `omitted` with `reason` and
  `reviewed_by`, and the report carries `partial` (`true` or `false`) and an
  `omitted_steps` list of `{id, heading, reason, reviewed_by}`.
- A run with omissions ends with report status `partial` instead of `complete` and
  exits 0, because every executed step was verified. A run without omissions is
  unchanged: status `complete`, `partial` false.

A partial result is not a full manual reference install. The executed steps ran
without the omitted ones, and later rows in shared `.2da`/`.tlk` files may differ
from a full run. Do not use a partial run to claim parity for any resource the omitted
steps touch, and remove each omission as soon as its step can be authored. The
`cache.py restore` step still restores every archive of the guide; a link that is
uncached by policy must also be passed with `--allow-uncached-by-policy`, which is
independent of `omit`.

## Drafting a plan

`tools/modsync-ci/draft_plan.py` gives the reviewer a starting point. It reads
`content/<game>/full.md` and writes two files under `ci/drafts/`:

- `<game>-manual-plan.draft.json`: one step per `###` heading in `## Mod List`, in
  guide order, plus an entry for every supplemental heading. Every `operations`
  list and every supplemental `review` is empty, and `archive_sha256` is `{}`.
- `<game>-manual-plan.review.json`: every step with status `needs-human-review` and
  the evidence found in the guide: heading and line, the links on the `**Name:**`
  line (with an archive name only when the URL path itself ends in `.zip`, `.7z` or
  `.rar`), the verbatim `**Installation Method:**` text and the lines of each
  `:::` block.

```
python3 tools/modsync-ci/draft_plan.py [--game k1|k2|all] [--force]
```

A draft is not a plan. The tool never invents an operation, `argv`, patcher, hash
or success marker, and it refuses to write `ci/<game>-manual-plan.json` or anything
else in `ci/` outside `ci/drafts/`. `manual.py` rejects a draft three ways: the
top-level `draft` key is an unknown field, empty `operations` are an error, and
empty supplemental reviews are an error. Output is deterministic; an existing draft
that differs is not overwritten without `--force`.

A human authors the real plan from the guide and the original patchers, reviews
every step and supplemental section, drops the `draft` key and commits the result as
`ci/<game>-manual-plan.json`. Do not commit generated or inferred operations as
reviewed.

## Execution rules

- Start from a clean game tree; the work directory must not exist yet.
- Snapshot before each step. Run steps in guide order.
- If a step fails, restore its pre-step snapshot and stop. Never skip a step,
  continue past it, or install a fix at the end: loose files are last-writer-wins
  and patchers build on earlier rows in shared `.2da` and `.tlk` files.
- Work, logs, snapshots and reports are private (0700/0600).
- Exit 0 means every executed step completed and verified (report status `complete`,
  or `partial` when steps were reviewed and omitted); 1 means failed or blocked.
  There is no resume.

## Comparing with the port lanes

Compare only runs with the same guide revision, selections and baseline. Compare
loose files by content hash and containers by their logical resources. Packing-only
differences are acceptable only after the contents are shown to be equal. Each
finding should name the resource, which lanes have it, the difference and the guide
step responsible.

A reviewed plan or a green infrastructure step is not a successful manual install.
Only an actual end-to-end run with every step verified counts. A `partial` run
(omitted steps) is evidence for the executed steps only.
