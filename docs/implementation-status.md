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
- **`mods`:** refresh up to 500 due archive links per run into the S3-compatible
  (Cloudflare R2) cache through `rclone`, with the write credentials. Unresolved
  links are reported, not errors, and the job never blocks an install. It is the
  only job that adds links to the cache. See [cache.md](cache.md).
- **`install` (k1, k2 × rust, typescript × cli, gui):** restore and verify the game
  (a cache miss fails the lane), restore archives with read-only credentials,
  then run `ports.py`. Archive restore is fail-closed: any link not restored and
  verified stops the lane before install. TypeScript lanes fail at the start if
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
| Mod archive cache | Partial. Contents are not readable from a workstation without the bucket credentials; per-run totals are in the `mods` step summary |
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
- **ModSync.ts:** has no P2P. Physical installation is unavailable:
  `installer.ts` throws for every non-dry-run install, so a TypeScript lane cannot
  complete an install even with a reviewed manifest. Only dry-run prediction on a
  virtual filesystem works.

## Known external blockers

None of these is bypassed.

| Blocker | Evidence | Effect |
| --- | --- | --- |
| Nexus Mods | 37 links in the two guides; no `NEXUS_API_KEY`; Cloudflare Turnstile on file pages | Never cached, reported `blocked-pending-user-action` (summary class `credentials-required`); the CAPTCHA is never solved |
| GameFront | 2 links; HTTP 403 with a Cloudflare challenge | Never cached; the client has no adapter |
| DeadlyStream post 2503 (K2 cutscenes) | About 25.7 GB of Bink video; listed under `uncached` in `archive_selections.json` | Never downloaded or uploaded; restore reports `uncached-by-policy` and K2 restore fails |

Because restore is fail-closed, a guide that contains any of these links stops at
the restore step in every install and manual lane.

## Rust lane gaps

- 69 K1 components had no `Instructions` when last measured, and `ports.py` stops
  the Rust lane on that (`guide_steps_without_instructions`).
- Generated draft instructions need a human review with notes. `gui_rust.py`
  cannot authorize one and stops.
- Archive file names containing `[ ] * ? { }` were reported to abort
  `--auto-generate-local`. That behavior is in ModSync.rs; it was not re-checked
  for this update.
- Widescreen sections are not covered by either GUI driver: `gui_rust.py` refuses
  an input with widescreen-only components and `gui_typescript.py` refuses a
  selection with widescreen mods.

## Next steps

1. Resolve or deliberately handle the three blockers above; until then no guide
   restores completely.
2. Let scheduled runs keep filling the archive cache; triage links that stay
   unresolved from the `mods` step summary.
3. Commit reviewed `ci/<game>-typescript-review.json` manifests and
   `ci/<game>-manual-plan.json` plans.
4. Finish the TypeScript physical install and the Rust archive-aware install work
   (including the missing `Instructions` and the file-name limitation), then run
   every lane against the cached baselines.
