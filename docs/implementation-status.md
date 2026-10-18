# Implementation status

As of 2026-10-16. Statements about code and the workflow were checked against this
branch (`fix/ci-archive-restore-and-manual-plans`). Actions cache entries were
read with `gh cache list` on the same day.

## CI design

Everything runs from one workflow, `.github/workflows/ci.yml`; see [ci.md](ci.md).

- **Triggers:** push to `dev`, pull requests (same-repository only; fork and
  Dependabot pull requests skip every job that needs secrets), published releases,
  a Monday and Thursday schedule (`23 4 * * 1,4`) and `workflow_dispatch`. A newer
  push to a pull request cancels its older run; other runs are not cancelled.
- **`game` (k1, k2; one at a time):** look up the encrypted game cache, or
  download with SteamCMD (saved login if `STEAM_CONFIG_VDF` is set, then password,
  then `STEAM_GUARD_CODES`) and save it. Fails only if the game is still missing.
  `.bik` movies are never cached. See [steam.md](steam.md).
- **`mods`:** restores the Actions cache, optionally refreshes the Cloudflare R2
  bucket (write credentials; skipped without them), then runs `cache.py refresh`,
  which revalidates each archive and keeps at most 3 GiB (pinned first, then
  smallest) in the Actions cache. Unresolved links are reported, not errors, and
  the job never blocks an install. It is the only job that saves the cache. See
  [cache.md](cache.md).
- **`install` (k1, k2 × rust, typescript × cli, gui):** restore and verify the game
  (a cache miss fails the lane), restore archives, then run `ports.py`. Per
  archive, restore takes the first of: the restored Actions-cache copy
  (revalidated by conditional GET), the optional R2 bucket (read-only credentials),
  P2P (inert: no pin has a magnet), the website. The last valid local copy is used
  if the website is unreachable. The lane stops before install only when no source
  yields a required archive. TypeScript lanes fail at the start if
  `ci/<game>-typescript-review.json` is missing. Lanes run per game even if the
  other game failed. See [ports.md](ports.md).
- **`manual` (k1, k2):** same restores, then `manual.py` with a reviewed plan. The
  job fails if `ci/<game>-manual-plan.json` is missing. See
  [manual-lane.md](manual-lane.md).
- **`p2p-loopback`:** builds ModSync.rs and runs a loopback seed-to-download
  transfer. See below for what that does and does not prove.

All jobs use Linux runners because the adapters are POSIX and Xvfb only.

## Current state

| Area | State |
| --- | --- |
| K1 game cache | Present, served by the fallback entry `steam-sealed-v3-excludingmovies-k1-Linux-*`; there is no `game-k1-v1` entry |
| K2 game cache | Present: `game-k2-v1`, saved 2026-10-16 |
| Mod archive cache | Partial. The Actions cache holds at most 3 GiB per run; the optional R2 bucket is not readable from a workstation without its credentials. Per-run totals are in the `mods` step summary |
| Review manifests (`ci/<game>-<impl>-review.json`) | None committed (no `ci/` directory exists) |
| Manual plans (`ci/<game>-manual-plan.json`) | None committed |

Both gates are hard failures: without review manifests the TypeScript install
lanes fail at their first step, and without manual plans the `manual` job fails
at its first step. Neither can pass until a human-reviewed file is committed. No
full-guide install has passed in CI for any lane.

## What each implementation does today

- **ModSync.rs:** has peer-to-peer transfer through `librqbit`
  (`torrent-seed`, `torrent-download`). The `p2p-loopback` job proves one thing:
  a seed and a downloader on the same runner move a random 3 MB zip byte for byte,
  and a download with a wrong `--sha256` is refused with no output file. It does
  not test DHT, trackers, SOCKS5, NAT traversal or any GUI.
- **ModSync.ts:** has no P2P. The CI lanes build it from `feat/physical-install`
  (`MODSYNC_TS_REF`) and the `cli` lane passes `--ci-physical-install`; the default
  branch throws for every non-dry-run install. The TypeScript lanes still need
  `ci/<game>-typescript-review.json` and a policy-skip mechanism, and the GUI/server
  route still reports `physicalRestore: false`, so no TypeScript lane is expected to
  complete a full install yet.

## Known external blockers

None of these is bypassed.

| Blocker | Evidence | Effect |
| --- | --- | --- |
| Nexus Mods | 37 links in the two guides; no `NEXUS_API_KEY`; Cloudflare Turnstile on file pages | Never cached, reported `blocked-pending-user-action` (summary class `credentials-required`); the CAPTCHA is never solved |
| GameFront | 2 links; HTTP 403 with a Cloudflare challenge | Never cached; the client has no adapter |
| DeadlyStream post 2503 (K2 cutscenes) | About 25.7 GB of Bink video; listed under `uncached` in `archive_selections.json` | Never downloaded or uploaded; restore reports `uncached-by-policy` and K2 restore fails, except in the Rust install lanes, which pass the reviewed step-scoped `--allow-uncached-by-policy "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled"` and have `ports.py` deselect the component |

A guide that contains any of these links stops at the restore step in every
install and manual lane, because no source can supply the archive.

## Rust lane gaps

- 69 K1 components had no `Instructions` when last measured, and `ports.py` stops
  the Rust lane on that (`guide_steps_without_instructions`).
- Generated draft instructions need a human review with notes. `gui_rust.py`
  cannot authorize one and stops (a deselected draft component is not installed and
  does not stop it).
- Until that review happens, the components whose drafts the verified-draft review leaves
  unreviewed are deselected from the Rust install lanes as visible, committed reviewed
  policy skips (owner decision 2026-10-18; CI run 39136954201): k1 150, k2 118 plus the
  dependent `Extended Korriban Arrival`. This is omission, not approval: the drafts stay
  unreviewed, the lane report lists them under `deselected_pending_review` and the CI
  log prints the count and names, and a new unreviewed draft still blocks. The rest of
  each guide (k1 46 of 197 components, k2 30 of 151, after the `4GB Patcher` and
  `K2 Cutscenes Rescaled` skips) is what these lanes now install, so a green Rust install lane is a
  partial-guide result, not guide parity. Not yet confirmed against the CI-built Rust
  binary; see [ports.md](ports.md#components-deselected-pending-human-review).
- Archive file names containing `[ ] * ? { }` were reported to abort
  `--auto-generate-local`. That behavior is in ModSync.rs; it was not re-checked
  for this update.
- Widescreen sections are not covered by either GUI driver: `gui_rust.py` excludes
  widescreen-only components (listed as not covered) and `gui_typescript.py` refuses
  a selection with widescreen mods.

## Next steps

1. Resolve or deliberately handle the three blockers above; until then no guide
   restores completely.
2. Let scheduled runs keep filling the archive cache; triage links that stay
   unresolved from the `mods` step summary.
3. Commit reviewed `ci/<game>-typescript-review.json` manifests and
   `ci/<game>-manual-plan.json` plans.
4. Finish the TypeScript physical install (review manifests, policy skip, GUI
   route) and the Rust archive-aware install work
   (including the missing `Instructions` and the file-name limitation), then run
   every lane against the cached baselines.
