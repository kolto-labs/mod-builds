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

1. Looks for `ci/<game>-manual-plan.json`. Without it the job logs a notice, skips
   every remaining step and passes, so a green run without a plan means nothing.
2. Installs Wine, Xvfb and archive tools.
3. Restores `game-<game>-v1` and runs `game_cache.py unpack`, which verifies every
   file. See [steam.md](steam.md).
4. Restores archives with `cache.py restore`. This is best effort; a missing
   archive fails the step that needs it. See [cache.md](cache.md).
5. Runs `xvfb-run -a python tools/modsync-ci/manual.py --plan <plan> --guide
   content/<game>/full.md --game-dir ... --archives ... --work-dir ... --report ...`.

Linux runners only: `manual.py` is POSIX only, and Windows patchers run under Wine.

## The plan

`manual.py` executes a human-reviewed JSON plan. It never generates, infers or
approves operations itself. The full contract is in the docstring of
`tools/modsync-ci/manual.py`. In short:

- `guide_sha256` pins the guide; `archive_sha256` binds every archive input file.
- `steps` follow every `###` heading in `## Mod List`, once each, in order. Each
  step lists `operations`: `extract`, `copy`, `delete` or `patcher`.
- `supplemental_sections` gives a written review for every heading outside the
  Mod List and links any steps it affects.
- A `patcher` operation names the original patcher and its SHA-256, exact `argv`
  (optionally after an absolute `wine` path), working directory, success log,
  exact success markers, failure regexes, and expected output files or a required
  manifest change.

Use the mod's own original patcher. Do not substitute ModSync's `vendor/bin`
patchers, OdyPatcher or another HoloPatcher build.

## Execution rules

- Start from a clean game tree; the work directory must not exist yet.
- Snapshot before each step. Run steps in guide order.
- If a step fails, restore its pre-step snapshot and stop. Never skip a step,
  continue past it, or install a fix at the end: loose files are last-writer-wins
  and patchers build on earlier rows in shared `.2da` and `.tlk` files.
- Work, logs, snapshots and reports are private (0700/0600).
- Exit 0 means every step completed and verified; 1 means failed or blocked. There
  is no resume.

## Comparing with the port lanes

Compare only runs with the same guide revision, selections and baseline. Compare
loose files by content hash and containers by their logical resources. Packing-only
differences are acceptable only after the contents are shown to be equal. Each
finding should name the resource, which lanes have it, the difference and the guide
step responsible.

A reviewed plan or a green infrastructure step is not a successful manual install.
Only an actual end-to-end run with every step verified counts.
