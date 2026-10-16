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
```

`install` and `manual` wait for `game` and `mods` but still run when either of
them failed, as long as `game` was not skipped. Each lane is independent
(`fail-fast: false`).

**`game`** — Restores the sealed game from the Actions cache. On a miss it
downloads the game once with SteamCMD, removes `.bik` movies, seals it and saves
it. If there is no complete game at the end, the job fails. Runs k1, then k2, on
one Steam account. See [steam.md](steam.md).

**`mods`** — Runs `refresh_archives.py` to download new or changed mod archives
into the Cloudflare R2 cache, up to 20 links per run. It is an accelerator and
`continue-on-error` is set, so a failure never blocks an install. It does not run
on pull requests. See [cache.md](cache.md).

**`install`** — Checks out the ModSync source for the implementation (the
repository named by `MODSYNC_RS_REPOSITORY` or `MODSYNC_TS_REPOSITORY`, read with
`MODSYNC_SOURCE_TOKEN`; empty values fail the job). It restores the game cache
(a miss fails the job) and unpacks it. For archives, it restores the current
week's per-game GitHub Actions cache first, then adds available R2 archives when
configured. One CLI lane saves the merged snapshot, including partial snapshots.
Unavailable archives are diagnosed by `ports.py` when installation needs them.
`gui` lanes run under Xvfb. See [ports.md](ports.md) and [cache.md](cache.md).

**`manual`** — Restores archives from GitHub Actions cache first and adds archives
from R2 if configured, then installs the same guide step by step with each mod's
original patcher under Wine, from a human-reviewed plan at
`ci/<game>-manual-plan.json`.
If the plan file is absent, the job logs an error and fails before installation.
The matrix uses `fail-fast: false`, so this does not cancel the other jobs. See
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
| `MODSYNC_SOURCE_TOKEN` | secret | Read access to the ModSync source repositories |
| `MODSYNC_RS_REPOSITORY`, `MODSYNC_TS_REPOSITORY` | variable | Repositories to build for `rust` and `typescript` |

Each secret is only exposed to the steps that use it.

## Running it by hand

```sh
gh workflow run ci.yml -R kolto-labs/mod-builds --ref dev
gh run list -R kolto-labs/mod-builds --workflow ci.yml
```
