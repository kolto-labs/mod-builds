# CI

Game acquisition, the archive cache and every install lane run from one workflow,
`.github/workflows/ci.yml`. The game cache is refilled from Steam on a miss. The
archive cache is not refilled by install lanes: a lane fails when a job cannot get
the game, when a required archive is not in the cache, or when an install fails.

## Triggers

- Push to `dev`, unless the push only changes `docs/`, `meta.json` or `README.md`.
- Pull requests from this repository. Forks and Dependabot get no secrets, so every
  job is skipped for them.
- A published release.
- Monday and Thursday, 04:23 UTC.
- Manual dispatch.

A new push to a pull request cancels its previous run. Other runs are not cancelled.

## Jobs

```
game (k1, k2) ──┬──> install (k1|k2 × rust|typescript × cli|gui)   8 jobs
mods ───────────┴──> manual (k1|k2)                                2 jobs
p2p-loopback                                                       1 job, standalone
```

`install` and `manual` wait for `game` and `mods` but still run when either of
them failed, as long as `game` was not skipped. Each lane is independent
(`fail-fast: false`).

**`game`** — Looks up the sealed game in the Actions cache (nothing is downloaded
on a hit). On a miss it downloads the game once with SteamCMD, removes `.bik`
movies, seals it and saves it. If there is no complete game at the end, the job
fails. Runs k1, then k2, on one Steam account. The Monday/Thursday schedule keeps
both game caches in use; see [steam.md](steam.md).

**`mods`** — Runs `refresh_archives.py --max-items 500` to download new or stale
mod archives into the S3-compatible (Cloudflare R2) cache, with a 35-minute limit.
It installs `7zip` (with `7zip-rar`), `megatools`, `libarchive-tools` (`bsdtar`),
`unrar` when available, and `gdown`, so `cache.py` can fetch MEGA and Google Drive
links and check RAR/7z archives. It is the only job that adds anything to the
cache, and the only one given the write credentials. It is an accelerator and
`continue-on-error` is set, so a failure never blocks an install. See
[cache.md](cache.md).

**`install`** — Starts with the lane checks below, then checks out the ModSync
source for the implementation (the repository named by `MODSYNC_RS_REPOSITORY` or
`MODSYNC_TS_REPOSITORY`, read with `MODSYNC_SOURCE_TOKEN`; empty values fail the
job). It restores the game cache (a miss fails the job) and unpacks it, restores
the archives for `content/<game>/full.md` from the R2 cache, and runs `ports.py` to
install the full guide. `gui` lanes run under Xvfb. See [ports.md](ports.md).

Archive restore is fail-closed and read-only for the cache: if any link of the
guide cannot be restored and verified, the restore step exits non-zero and the
lane stops before `ports.py`. The install lanes never acquire a link the cache has
not already recorded; only `mods` does. Today that means every lane stops at
restore for a guide that contains an uncached link (Nexus Mods, GameFront and the
K2 cutscene post, reported as `uncached-by-policy`; see
[cache.md](cache.md#known-gaps)).

### Install lanes and uncached links

`cache.py restore --allow-uncached-by-policy SECTION` lets a lane proceed past a
policy-uncached link (see [cache.md](cache.md#restoring-install-and-manual-jobs)) when every guide step that
references it is in a section the lane does not install. Only a lane that never
installs that section may pass it. Checked against the lane code on the current
`ModSync.rs` and `ModSync.ts` sources:

| Lane | Installs the K2 `Enhanced Widescreen Support Mods` section? | Flag |
| --- | --- | --- |
| `rust` + `cli` | Yes. `ports.py` runs `install --phase all`; the converter leaves widescreen components selected (`IsSelected = true`, `WidescreenOnly = true`) | Not allowed |
| `rust` + `gui` | No, but only because `gui_rust.py` refuses any input with widescreen-only components, so the K2 lane cannot complete | Not wired |
| `typescript` + `cli`/`gui` | Not selected for install (`select_base_components`), but `validate --all` selects every mod, and both lanes require a reviewed manifest and an installer that is unavailable | Not wired |
| `manual` | Unknown: depends on the human-reviewed `ci/<game>-manual-plan.json`, which does not exist | Not wired |

No workflow step passes the flag today. The restore report (`--report`) is where a
skipped step is recorded; a lane that passes the flag must keep that report with
its other artifacts.

- **`typescript` lanes** first require `ci/<game>-typescript-review.json`, a
  human-reviewed manifest. If it is missing, the job fails; it never passes
  without one. `ports.py` then validates it against the guide hashes. Even with a
  manifest, a TypeScript lane cannot complete an install today: ModSync.ts
  `installer.ts` throws for any non-dry-run install ("Independent TypeScript
  physical installation is unavailable").
- **`rust` lanes** use `ci/<game>-rust-review.json` only if it exists. They build
  with Cargo, which fetches private git dependencies (`kolto-labs/OdyPatcher`,
  `holowan-biolabs/odynsscomp`). A step before the build points `git` at
  `MODSYNC_SOURCE_TOKEN` (`url.<token URL>.insteadOf https://github.com/`) and sets
  `CARGO_NET_GIT_FETCH_WITH_CLI=true`. The token is read from the secret into an
  environment variable and masked. It is never printed, cached or uploaded.
  `ports.py` stops a Rust lane before install when a converted component has no
  `Instructions` (`guide_steps_without_instructions`); 69 K1 components were in
  that state when last measured.
- **GUI lanes** do not cover the separate widescreen phase. `gui_rust.py` refuses
  an input that contains widescreen-only components, and `gui_typescript.py`
  refuses a selection that contains widescreen mods. `gui_rust.py` also cannot
  authorize a draft review (`draft_review_receipts_require_human_source_review_driver_cannot_authorize`);
  draft instructions need a human review with notes. Archive file names containing
  `[ ] * ? { }` were reported to abort `--auto-generate-local`; that rule lives in
  ModSync.rs and was not re-checked here.

**`p2p-loopback`** — Standalone; needs no game, cache or other job. Checks out the
Rust ModSync source (same variable, secret and credential setup as `install`),
builds `modsync`, then runs `tools/modsync-ci/p2p_loopback.py`. The script creates
a random 3 MB zip, starts `modsync torrent-seed <zip> --no-proxy`, reads the JSON
line it prints (`magnet`, `sha256`, `peer`), downloads with `modsync
torrent-download <magnet> <out> --sha256 <hash> --peer 127.0.0.1:<port>
--no-proxy`, and compares the result byte for byte with `cmp`. It then repeats the
download with a wrong `--sha256` and requires a refusal and no output file.

ModSync.rs implements this transfer with `librqbit`. The job proves only that a
seed and a downloader on the same runner exchange a file byte for byte and that a
wrong hash is rejected. It does not test DHT, trackers, SOCKS5, NAT traversal or
any GUI.

**ModSync.ts and P2P:** ModSync.ts has no P2P implementation, so no job covers it
and the `typescript` lanes make no P2P claim.

**`manual`** — Installs the same guide step by step with each mod's original
patcher under Wine, from a human-reviewed plan at `ci/<game>-manual-plan.json`.
The job fails at its first step if the plan is missing; it never passes without
one. It restores the game and archives the same way as `install`, with the same
fail-closed restore. See [manual-lane.md](manual-lane.md).

## Secrets and variables

Names only.

| Name | Kind | Purpose |
| --- | --- | --- |
| `STEAM_USERNAME`, `STEAM_PASSWORD` | secret | Steam account that owns both games |
| `STEAM_CONFIG_VDF` | secret, optional | Saved SteamCMD login, so CI needs no Steam Guard approval |
| `STEAM_GUARD_CODES` | secret, optional | Single-use Steam backup codes, tried after the other sign-ins fail |
| `CACHE_ENCRYPTION_KEY` | secret | Seals and unseals the game cache (not the archive bucket) |
| `MOD_CACHE_S3_ACCESS_KEY_ID`, `MOD_CACHE_S3_SECRET_ACCESS_KEY` | secret | R2 read/write, `mods` job only |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID`, `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | secret | R2 read only, `install` and `manual` |
| `MOD_CACHE_S3_ENDPOINT` | secret | S3-compatible (R2) endpoint |
| `MOD_CACHE_BUCKET` | variable | R2 bucket |
| `MODSYNC_SOURCE_TOKEN` | secret | Read access to the ModSync source repositories and their private Cargo git dependencies |
| `MODSYNC_RS_REPOSITORY`, `MODSYNC_TS_REPOSITORY` | variable | Repositories to build for `rust` and `typescript` |

Each secret is only exposed to the steps that use it.

## Running it by hand

```sh
gh workflow run ci.yml -R kolto-labs/mod-builds --ref dev
gh run list -R kolto-labs/mod-builds --workflow ci.yml
```
