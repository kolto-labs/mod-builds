# CI

Game acquisition, the archive cache and every install lane run from one workflow,
`.github/workflows/ci.yml`. Caches make runs faster. A run fails when a job cannot
get the game or an install fails, not because a cache was empty.

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

**`game`** — Restores the sealed game from the Actions cache. On a miss it
downloads the game once with SteamCMD, removes `.bik` movies, seals it and saves
it. If there is no complete game at the end, the job fails. Runs k1, then k2, on
one Steam account. See [steam.md](steam.md).

**`mods`** — Runs `refresh_archives.py` to download new or changed mod archives
into the Cloudflare R2 cache, up to 20 links per run. It installs `7zip` (with
`7zip-rar`), `megatools` and `gdown` so `cache.py` can fetch MEGA and Google Drive
links and check RAR/7z archives. It is an accelerator and `continue-on-error` is
set, so a failure never blocks an install. See [cache.md](cache.md).

**`install`** — Checks out the ModSync source for the implementation (the
repository named by `MODSYNC_RS_REPOSITORY` or `MODSYNC_TS_REPOSITORY`, read with
`MODSYNC_SOURCE_TOKEN`; empty values fail the job). It restores the game cache
(a miss fails the job) and unpacks it, restores whatever archives for
`content/<game>/full.md` the R2 cache has, and runs `ports.py` to install the full
guide. A missing archive fails the install step that needs it. `gui` lanes run
under Xvfb. See [ports.md](ports.md).

The `rust` lanes build with Cargo, which fetches private git dependencies
(`kolto-labs/OdyPatcher`, `holowan-biolabs/odynsscomp`). A step before the build
points `git` at `MODSYNC_SOURCE_TOKEN` (`url.<token URL>.insteadOf
https://github.com/`) and sets `CARGO_NET_GIT_FETCH_WITH_CLI=true`. The token is
read from the secret into an environment variable and masked. It is never printed,
cached or uploaded.

**`p2p-loopback`** — Standalone; needs no game, cache or other job. Checks out the
Rust ModSync source (same variable, secret and credential setup as `install`),
builds `modsync`, then runs `tools/modsync-ci/p2p_loopback.py`. The script creates
a random 3 MB zip, starts `modsync torrent-seed <zip> --no-proxy`, reads the JSON
line it prints (`magnet`, `sha256`, `peer`), downloads with `modsync
torrent-download <magnet> <out> --sha256 <hash> --peer 127.0.0.1:<port>
--no-proxy`, and compares the result byte for byte with `cmp`. It then repeats the
download with a wrong `--sha256` and requires a refusal and no output file. The
whole transfer stays on the runner. It exercises the Rust implementation only.

**ModSync.ts and P2P:** ModSync.ts has no P2P implementation. Peer-to-peer
transfer is not implemented there, so no job covers it and the `typescript` lanes
make no P2P claim.

**`manual`** — Installs the same guide step by step with each mod's original
patcher under Wine, from a human-reviewed plan at `ci/<game>-manual-plan.json`.
If the plan file is absent, the job logs a notice, skips every step and passes. A
green `manual` job therefore proves nothing until the plan exists. See
[manual-lane.md](manual-lane.md).

## Secrets and variables

Names only.

| Name | Kind | Purpose |
| --- | --- | --- |
| `STEAM_USERNAME`, `STEAM_PASSWORD` | secret | Steam account that owns both games |
| `STEAM_CONFIG_VDF` | secret, optional | Saved SteamCMD login, so CI needs no Steam Guard approval |
| `CACHE_ENCRYPTION_KEY` | secret | Seals and unseals the game cache |
| `MOD_CACHE_S3_ACCESS_KEY_ID`, `MOD_CACHE_S3_SECRET_ACCESS_KEY` | secret | R2 read/write, `mods` job only |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID`, `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | secret | R2 read only, `install` and `manual` |
| `MOD_CACHE_S3_ENDPOINT` | secret | R2 endpoint |
| `MOD_CACHE_BUCKET` | variable | R2 bucket |
| `MODSYNC_SOURCE_TOKEN` | secret | Read access to the ModSync source repositories and their private Cargo git dependencies |
| `MODSYNC_RS_REPOSITORY`, `MODSYNC_TS_REPOSITORY` | variable | Repositories to build for `rust` and `typescript` |

Each secret is only exposed to the steps that use it.

## Running it by hand

```sh
gh workflow run ci.yml -R kolto-labs/mod-builds --ref dev
gh run list -R kolto-labs/mod-builds --workflow ci.yml
```
