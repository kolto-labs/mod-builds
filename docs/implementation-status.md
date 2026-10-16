# Implementation status

As of 2026-10-15.

## CI design

Everything runs from one workflow, `.github/workflows/ci.yml`; see [ci.md](ci.md).

- **Triggers:** push to `dev`, pull requests (same-repository only; fork and
  Dependabot pull requests skip every job that needs secrets), published releases,
  a Monday and Thursday schedule and `workflow_dispatch`. A newer push to a pull
  request cancels its older run; other runs are not cancelled.
- **`game` (k1, k2; one at a time):** restore the encrypted game cache, or download
  with SteamCMD (saved login if `STEAM_CONFIG_VDF` is set) and save it. Fails only
  if the game is still missing. See [steam.md](steam.md).
- **`mods`:** refresh up to 20 archive links per run into Cloudflare R2. Unresolved
  links are reported, not errors, and the job never blocks an install. See
  [cache.md](cache.md).
- **`install` (k1, k2 × rust, typescript × cli, gui):** restore and verify the game
  (a cache miss fails the lane), restore archives on a best-effort basis, run
  `ports.py`. Lanes run per game even if the other game failed. See
  [ports.md](ports.md).
- **`manual` (k1, k2):** same restores, then `manual.py` with a reviewed plan. With
  no plan committed it skips every step and passes. See
  [manual-lane.md](manual-lane.md).

All jobs use Linux runners because the adapters are POSIX and Xvfb only.

## Current state

| Area | State |
| --- | --- |
| K1 game cache | Present |
| K2 game cache | Missing; Steam mobile-app approvals timed out |
| Mod archive cache | Partial; filling about 20 links per run |
| Review manifests (`ci/<game>-<impl>-review.json`) | None committed |
| Manual plans (`ci/<game>-manual-plan.json`) | None committed |

Without review manifests the TypeScript lanes stop with `review_manifest_required`,
and without manual plans the `manual` job passes without installing anything. No
full-guide install has passed in CI for any lane.

## Next steps

1. Get the K2 cache saved: either approve the sign-in in the Steam mobile app
   during a run, or set `STEAM_CONFIG_VDF` once (see [steam.md](steam.md)) so no
   approval is needed.
2. Let scheduled runs keep filling the archive cache; triage links that stay
   unresolved.
3. Commit reviewed `ci/<game>-typescript-review.json` manifests and
   `ci/<game>-manual-plan.json` plans.
4. Finish the TypeScript physical install and restore path and the Rust
   archive-aware install work, then run every lane against the cached baselines.
