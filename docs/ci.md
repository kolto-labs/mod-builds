# CI

Game acquisition, the archive cache and every install lane run from one workflow,
`.github/workflows/ci.yml`. The game cache is refilled from Steam on a miss. Mod
archives come from a chain of sources (see [Mod archive sources](#mod-archive-sources));
every cache in it is only an accelerator. A lane fails when a job cannot get the
game, when a required archive cannot be obtained from any source, or when an
install fails.

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
mods ───> artstore refresh                                         schedule / dispatch only
```

`install` and `manual` wait for `game` and `mods` but still run when either of
them failed, as long as `game` was not skipped. Each lane is independent
(`fail-fast: false`).

**`game`** — Looks up the sealed game in the Actions cache (nothing is downloaded
on a hit) and the newest unexpired `game-<game>-sealed` backup artifact. On a miss it
refills the cache from that artifact; only without an artifact does it download the game
once with SteamCMD, remove `.bik` movies, seal it and save it. If there is no complete game,
or `modules/` holds no `.rim` file, at the end, the job fails (it prints a game inventory
first). A cache hit with no backup, or one that expires within 14 days, restores the cache
and uploads the backup (90 days); a fresh SteamCMD download uploads it too. The backup is the
`CACHE_ENCRYPTION_KEY` seal cut into sealed parts of at most 2000 MiB (artstore envelope, so
the public artifact holds no game data in the clear) plus `manifest.json`. Backup problems
are `::warning::`s. Runs k1, then k2, on one Steam account. The Monday/Thursday schedule keeps
both game caches and backups fresh; see [steam.md](steam.md#game-backup-artifact).

**`mods`** — Restores the newest `mod-archives-*` Actions cache, runs `cache.py
refresh` over both guides, and saves the result under `mod-archives-<hash>` with
`if: always()`, so a cancelled or timed-out run keeps its partial progress. `<hash>` is
the first 16 hex digits of the SHA-256 of `manifest.json` with the per-run timestamps
(`checked_at`, `imported_at`) removed and keys sorted, so unchanged content keeps the
same key. Entries are immutable: a lookup (`lookup-only`) skips the save when that key
exists, and if two runs race for it the save action logs "Failed to save: Unable to
reserve cache ... another job may be creating this cache" at info level. That line, or a
skipped save, is not an error. The job exposes the key as the output
`archive_cache_key` (empty when there was no manifest). After the save, a
`continue-on-error` prune step runs `gh cache list --key mod-archives-` and
`gh cache delete`s every key except the current one, once it has confirmed that the
current key is listed, so only one mod entry stays. It never touches `game-*` or
`steam-sealed-*`. The job sets `permissions: actions: write` (with `contents: read`)
for that step; the top level stays `contents: read`. `refresh` revalidates every
archive and fetches only new or changed ones (see below), then keeps at most 2.5 GiB
of objects (`--cache-limit-bytes 2684354560`; pinned first, then smallest; `.bik` never).
Archives that do not fit stay in the manifest and are fetched by the lanes. Before
that, an optional step runs `refresh_archives.py --max-items 500`, which keeps the
R2 bucket current when its write secrets exist and otherwise skips itself. The job
installs `7zip` (with `7zip-rar`), `megatools`, `libarchive-tools` (`bsdtar`),
`unrar` when available, and `gdown`, so `cache.py` can fetch MEGA and Google Drive
links and check RAR/7z archives. It is the only job that saves the cache or gets the
R2 write credentials. It is an accelerator and `continue-on-error` is set (also on
both refresh steps), so a failure never blocks an install. See [cache.md](cache.md).
The refresh also uses and feeds the artifact store, and the job ends with the publish
steps described in [Artifact store (artstore)](#artifact-store-artstore).

**`artstore refresh`** — Runs only on the schedule and on manual dispatch (not for a
`seed_archives` dispatch), after `mods`, in the same `mod-cache-writer` concurrency
group. It renews artifact-store blobs that are close to expiry and prunes the store;
see [Artifact store (artstore)](#artifact-store-artstore). It is not a dependency of
any lane.

**`install`** — Starts with the lane checks below, then checks out the ModSync
source for the implementation (the repository named by `MODSYNC_RS_REPOSITORY` or
`MODSYNC_TS_REPOSITORY`, read with `MODSYNC_SOURCE_TOKEN`; empty values fail the
job). It restores the game cache (a miss fails the job with "game cache evicted —
re-run the game job") and unpacks it (the unpack verifies the SHA-256 inventory), runs
`game_cache.py normalize-case` (install-time only: lowercases `Modules/` and the other
canonical top-level directories and the file names in `modules/` and `override/`, so
mod installers see a case-normalized, Windows-equivalent tree; see
[steam.md](steam.md#case-normalization)), prints a game inventory (also in
`$GITHUB_STEP_SUMMARY`; it states whether the normalization has been applied; see
[steam.md](steam.md#game-inventory)), restores
the Actions cache that `mods` saved in the same run (key from
`needs.mods.outputs.archive_cache_key`, falling back to the `mod-archives-` prefix when
`mods` failed or was skipped; a miss is not an error), restores the archives for `content/<game>/full.md` from the chain
below, and runs `ports.py` to install the full guide. `gui` lanes run under Xvfb and install `python3-tk` so the runner's `python` can import `tkinter` (PyAutoGUI/MouseInfo needs it; `gui_rust.py` runs under the same interpreter via `sys.executable`).
See [ports.md](ports.md).

### Mod archive sources

`cache.py restore` and `cache.py refresh` get each archive from the first source
that works. Nothing is downloaded that is unchanged.

1. **Local copy** (the restored Actions cache). A copy whose size and SHA-256 match
   the manifest and that passes the archive check is revalidated cheaply: one
   conditional request with the stored `ETag` / `Last-Modified`, and `304` reuses
   it. A seed-pinned copy is never revalidated. With no validators (MEGA, Google
   Drive, some hosts) it is reused if checked within `--max-age-days` (14); older
   than that it is fetched again. A post that yields several files revalidates
   each file with its own validators.
2. **Artifact store**, for a missing or corrupt copy whose hash the manifest knows,
   when a token is in the environment; see [Artifact store (artstore)](#artifact-store-artstore).
   A miss is silent.
3. **R2 bucket**, for a missing or corrupt copy whose hash the manifest (or a seed
   pin) knows, when its secrets exist (`--remote`). The bucket manifest is merged
   (the newer `checked_at` wins, so seeded pins survive) and only the missing
   objects are pulled one by one, never the whole bucket. An unreachable bucket is
   a warning and is skipped for the rest of the run. Lanes never write to it.
4. **P2P**, same condition: `try_p2p(item, dest)` in `cache.py` runs
   `p2p_fetch.fetch` when `tools/modsync-ci/p2p_fetch.py` exists and the seed pin
   carries a `p2p` block with a magnet; otherwise it returns `False`. The result
   is re-hashed and archive-checked before use. A file that changed upstream has
   an unknown new hash, so it cannot come from here; it comes from the website.
5. **The website** (DeadlyStream, MEGA, Google Drive, direct links). A link the
   cache has never held is fetched here too. Nexus and GameFront need a pin: they
   have no headless adapter.
6. **The last cached copy**, when a copy exists but the website cannot be reached or
   rejects the request (`restore` only; `refresh` reports it as unresolved). A
   warning names the link by id prefix.

A step with no source left fails: the restore exits non-zero and the lane stops
before `ports.py`. Each report entry has a `source` (`local`, `revalidated`,
`recovered`, `website`, `stale-local`). `.bik` is never cached. The install job's
restore also writes
`$RUNNER_TEMP/archive-map.json` (`cache.py restore --archive-map`): a flat
`{guide link URL: filename | [filenames]}` object, URLs without query or fragment
(one exception: an old-style mega link at the host root, whose only identity is its
fragment, is keyed `https://mega.nz/modsync-fragment/<sha256(fragment)[:16]>`, so the
key carries no share key; see `cache.link_key`), filenames relative to the archives
directory, restored links only. `ports.py` looks links up with the same key. A bare
shared-host entry that maps to different files is left unresolved on purpose. `ports.py
--archive-map` passes it to the Rust `modsync convert --archive-map`; the
TypeScript lanes ignore it. A guide link that is uncached by policy
(Nexus Mods, GameFront and the K2 cutscene post, reported as `uncached-by-policy`;
see [cache.md](cache.md#known-gaps)) stops restore unless the lane passes
`--allow-uncached-by-policy` for it; every install lane (Rust and TypeScript) does so
for the reviewed steps below. The restore also writes `$RUNNER_TEMP/restore-report.json`
(`--report`), which every install lane hands to `ports.py --restore-report`.

### Install lanes and uncached links

`cache.py restore --allow-uncached-by-policy VALUE` lets a lane proceed past a
policy-uncached link (see [cache.md](cache.md#restoring-install-and-manual-jobs)) when every guide step that
references it is covered by a `VALUE`. `VALUE` is a `##` section name (whole section) or
`<## section>::<### heading>` (one step only). Only a lane that never installs the
named section or step may pass it. Checked against the lane code on the current
`ModSync.rs` and `ModSync.ts` sources:

| Lane | Installs the K2 `Enhanced Widescreen Support Mods` section? | Flag |
| --- | --- | --- |
| `rust` + `cli` | Yes. `ports.py` runs `install --phase all`; the converter leaves widescreen components selected (`IsSelected = true`, `WidescreenOnly = true`) | Not allowed |
| `rust` + `gui` | No. `ports.py` passes `--exclude-widescreen` to `gui_rust.py`, which deselects every widescreen-only component in a private copy of the converted TOML and reports it as not covered | Not wired |
| `typescript` + `cli`/`gui` | Not selected for install (`select_base_components`). Reviewed policy skips are left out by `--select` of the non-skipped component GUIDs (TypeScript has no `--deselect`), and both lanes require a reviewed manifest that does not exist yet. The `cli` lane now attempts a physical install (`--ci-physical-install`, see below); the `gui` lane does not | Not wired |
| `manual` | Unknown: depends on the human-reviewed `ci/<game>-manual-plan.json`, which does not exist. The job is non-gating: without the plan it installs nothing, ends green with a `::warning::` and a `$GITHUB_STEP_SUMMARY` line ("manual lane skipped: no reviewed plan; draft attached"), and uploads `draft_plan.py` output as artifact `manual-drafts-<game>` (14 days) | Not wired |

Reviewed policy (step-scoped, never a whole section). **Wired for all four install
lanes** (`rust`/`typescript` x `cli`/`gui`) in the `Restore required mod archives`
step, which both the Rust and the TypeScript install lanes run; the `manual` lane does
not pass them:

| Link | Game | Step (`##` / `###`) | Reason |
| --- | --- | --- | --- |
| `nexus:1214` Quicker TSLPatching | K1, K2 | `Optional Install Assistant - Quicker TSLPatching` / `Quicker TSLPatching` | Windows registry helper, documented not to work on Mac or Linux; nothing consumes its output. Not a component in the converter output |
| `nexus:1306` Remastered Cutscenes | K1 | `Optional Widescreen & Extended Widescreen Support Mods` / `Remastered Cutscenes` | Tier 4 optional loose `.bik` movies, never cached; mutually exclusive with the installed Cutscenes Rescaled post (DeadlyStream 2380). A selected, widescreen-only component without instructions in the converter output |
| `2503` K2 Cutscenes Rescaled | K2 | `Enhanced Widescreen Support Mods` / `K2 Cutscenes Rescaled` | Reviewed decision: the project rule is never to cache `.bik` video, and this post is about 25.7 GB of it (the pinned main and `_mods` files). The step only places `.bik` files in `movies/`, the CI game baseline carries no movies, and no other guide step reads or depends on them. The guide itself says to use only one of the two K2 cutscene mods; the other, naelavok's Nexus 1066 (`K2 Remastered Cutscenes`), is pinned, still restored and not skipped. A selected, widescreen-only component without instructions in the converter output |

Flags `ci.yml` passes to `cache.py restore` in Rust lanes:

- K1: `--allow-uncached-by-policy "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching" --allow-uncached-by-policy "Optional Widescreen & Extended Widescreen Support Mods::Remastered Cutscenes"`
- K2: `--allow-uncached-by-policy "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching" --allow-uncached-by-policy "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled"`

Restore only asserts the skip. `ports.py --restore-report` makes the lane enforce
it: the skipped steps are exempted from the `guide_steps_without_instructions`
coverage check, recorded in the lane report as `skipped_by_reviewed_policy`, and
their components are passed to `validate` and `install` as `--deselect <Guid>`;
see [ports.md](ports.md#reviewed-policy-skips) for the converter evidence.

The K2 flag names the one step, never the `Enhanced Widescreen Support Mods`
section: the section also holds `K2 Remastered Cutscenes` (1066) and the other
widescreen steps, which must still be restored and installed. Passing the section
form for this post is not done in any lane. The `rust` + `gui` lane gets the
flag too and installs a defined subset: the skipped component is deselected in a
private copy of the converted TOML (see [GUI subset](ports.md#gui-mode)), listed in
the lane report as `skipped_by_reviewed_policy` and never counted as covered. Every
other Nexus and GameFront link still fails restore.

The `typescript` lanes get the same `--allow-uncached-by-policy` flags and
`--restore-report`. TypeScript has no `--deselect`, so `ports.py` passes `--select`
with the GUIDs of the non-skipped components to `validate` (and the CLI `install`; the GUI lane
drives a policy-filtered copy of the TOML), and the
skipped steps are recorded in the lane report as `skipped_by_reviewed_policy`. The
lanes install Wine (`wine wine64 wine32:i386`) before `ports.py` for legacy patchers
and `Execute` steps, and still require the reviewed manifest described below.

- **`typescript` lanes** first require `ci/<game>-typescript-review.json`, a
  human-reviewed manifest. If it is missing, the job fails; it never passes
  without one. `ports.py` then validates it against the guide hashes. No
  `ci/*-typescript-review.json` exists yet (it must be human-authored), so these
  lanes still fail at that step and are not claimed to pass. Policy skips work as
  for the Rust lanes, through `--restore-report` and `--select` of the non-skipped
  GUIDs.
  - **Source ref.** The `typescript` lanes check out ModSync.ts at
    `vars.MODSYNC_TS_REF`, default `feat/physical-install` (workflow-level env
    `MODSYNC_TS_REF`), not the repository default branch. That branch is unmerged
    and adds an opt-in, CI-only physical install; the default branch
    (`claude/modsync-nodejs-typescript-0ea3qs` at the time of writing) throws for
    any non-dry-run install. The Rust lanes keep the repository's default branch.
  - **NSS script compiler.** `CompileList` steps need a native compiler. Before
    ModSync.ts runs, each `typescript` lane builds the `bionsscomp` CLI with
    `cargo install --git https://github.com/holowan-biolabs/odynsscomp --rev $NSSCOMP_REV
    --config 'patch."https://github.com/arrenkaetris/kotor-formats".kotor-ncs-isa.git="https://github.com/holowan-biolabs/kotor-formats"'
    --config 'patch."https://github.com/arrenkaetris/kotor-formats".kotor-ncs-isa.tag="v0.1.0"'
    bionsscomp-cli --root $RUNNER_TEMP/nsscomp` (workflow-level env
    `NSSCOMP_REV`, `00c53a9`; keep it equal to the rev in the Rust lanes'
    `Cargo.lock`). There is no `--locked` and there are two `--config` patches
    because odynsscomp's `bionsscomp` dev-dependency `kotor-ncs-isa` points at
    `https://github.com/arrenkaetris/kotor-formats`, an account that no longer
    exists (404; the locked commit `2d70164` is gone), so `--locked` cannot
    resolve. The patches redirect that URL to `holowan-biolabs/kotor-formats`
    tag `v0.1.0` (`d4ed9a8`). The proper long-term fix is an odynsscomp commit
    repointing the dev-dependency; then bump `NSSCOMP_REV` together with the Rust
    lanes' lock and drop the patches. The step also exports `MODSYNC_NSS_COMPILER` (path to the executable, called
    as `<bin> -c x.nss -o x.ncs -g 1|2 -n nwscript.nss -i <dir>`) and
    `MODSYNC_NSS_GAME` (`1` for k1, `2` for k2) for the install step. The install
    directory is cached with `actions/cache` keyed on runner OS and rev; the cache
    only speeds things up. On a miss the step builds, and a build failure, or a
    binary that is missing or fails `--version`, fails the lane. The repository is
    private, so the build reuses the Rust lanes' token step (below): on a cache miss
    `git` is pointed at `MODSYNC_SOURCE_TOKEN` through the same `insteadOf` rewrite,
    and the rewrite is removed again from the global git config once `cargo install`
    succeeds. On a cache hit the token step is skipped. The token is masked and never
    printed or cached; the cache holds only the installed binary. The engine-side use
    of the two variables lives in ModSync.ts, not here.
  - **Physical install (`cli` lane).** After `validate`, `ports.py` runs
    `install ... --phase base` and `install ... --phase widescreen`, each with
    `--ci-physical-install`. ModSync.ts then guards the game and archive roots with
    a portable lockfile (advisory; stale locks are never stolen) instead of native
    OS ownership. The flag is passed only to these two TypeScript `install`
    commands: not to `validate`, `import-md`, the Rust lanes, or the `manual` lane.
  - **`gui` lane.** `gui_typescript.py` drives the web UI and gets no flag. On the
    feat/physical-install commit (`2b3fecb`) its docs state the server and GUI
    routes still report `physicalRestore: false`, so a physical GUI install is not
    expected to work until ModSync.ts changes; untested here.
  - Still refused by ModSync.ts with the flag: `CreateDirectory` instructions and
    canonical authored guides (`rustAuthoringGuide`); Patcher/Execute steps run
    but cannot be rolled back from checkpoints.
- **`rust` lanes** use `ci/<game>-rust-review.json` only if it exists. They build
  with Cargo, which fetches private git dependencies (`kolto-labs/OdyPatcher`,
  `holowan-biolabs/odynsscomp`). A step before the build (a `typescript` lane
  also runs it on an NSS compiler cache miss) points `git` at
  `MODSYNC_SOURCE_TOKEN` (`url.<token URL>.insteadOf https://github.com/`) and sets
  `CARGO_NET_GIT_FETCH_WITH_CLI=true`. The token is read from the secret into an
  environment variable and masked. It is never printed, cached or uploaded.
  `ports.py` stops a Rust lane before install when a converted component has no
  `Instructions` (`guide_steps_without_instructions`); 69 K1 components were in
  that state when last measured.
  It then runs `modsync convert --review-verified-drafts` on the converted TOML
  (clears review only for drafts replay-verified against one local archive) and stops
  before `validate` with `unreviewed_draft_instructions_remain`, listing every GUID,
  if a component it would install still has an unreviewed draft; see
  [ports.md](ports.md#verified-draft-review). Only `convert.log` is sealed and
  uploaded; the `draft_review` summary is in the sealed `report.json`.
- **GUI lanes** do not cover the separate widescreen phase. The Rust GUI lane runs
  one base phase over a defined subset: every converted component except the
  reviewed-policy skips and the widescreen-only components, which are
  listed in the report (`gui_not_covered`, `excluded_widescreen`,
  `widescreen_phase: not_covered_by_gui_lane`), never claimed as installed. A
  standalone `gui_rust.py` still refuses an input that selects widescreen-only
  components unless `--exclude-widescreen` is passed, and `gui_typescript.py`
  refuses a selection that contains widescreen mods. `gui_rust.py` also cannot
  authorize a draft review (`draft_review_receipts_require_human_source_review_driver_cannot_authorize`)
  for any component it would install; draft instructions need a human review with
  notes. A deselected draft component is not installed, so it does not stop the driver. Archive file names containing
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
The job is non-gating while the plan is missing: the `Require the reviewed manual
plan` step emits a `::warning::` and a step summary ("manual lane skipped: no reviewed
plan; draft attached"), runs `draft_plan.py --out-dir "$RUNNER_TEMP/drafts"` and uploads
the drafts as `manual-drafts-<game>` (14 days), skips every install step and ends green.
Nothing is installed or verified in that case, so the lane is not evidence of a manual
install. When the plan exists, the job restores the game and archives the same way as
`install` and runs `manual.py`, which still fails the job if the plan fails validation
or the game or archives are missing. `report.json` is uploaded as `manual-report-<game>`
(`if: always()`, 14 days). See [manual-lane.md](manual-lane.md).

## Secrets and variables

Names only.

| Name | Kind | Purpose |
| --- | --- | --- |
| `STEAM_USERNAME`, `STEAM_PASSWORD` | secret | Steam account that owns both games |
| `STEAM_CONFIG_VDF` | secret, optional | Saved SteamCMD login, so CI needs no Steam Guard approval |
| `STEAM_GUARD_CODES` | secret, optional | Single-use Steam backup codes, tried after the other sign-ins fail |
| `CACHE_ENCRYPTION_KEY` | secret | Seals and unseals the game cache, the `game-<game>-sealed` backup artifact and the artifact-store blobs (not the archive bucket) |
| `DIAGNOSTICS_ENCRYPTION_KEY` | secret | Seals the `install` lane report and convert log before upload ([ports.md](ports.md#diagnosing-a-coverage-failure)) |
| `MOD_CACHE_S3_ACCESS_KEY_ID`, `MOD_CACHE_S3_SECRET_ACCESS_KEY` | secret, optional | R2 read/write, `mods` job only |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID`, `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | secret, optional | R2 read only, `install` and `manual` |
| `MOD_CACHE_S3_ENDPOINT` | secret, optional | S3-compatible (R2) endpoint |
| `MOD_CACHE_BUCKET` | variable, optional | R2 bucket |
| `MODSYNC_SOURCE_TOKEN` | secret | Read access to the ModSync source repositories and their private Cargo git dependencies |
| `MODSYNC_RS_REPOSITORY`, `MODSYNC_TS_REPOSITORY` | variable | Repositories to build for `rust` and `typescript` |
| `MODSYNC_TS_REF` | variable (optional) | ModSync.ts ref the `typescript` lanes check out; default `feat/physical-install` |

Each secret is only exposed to the steps that use it. Without the R2 values the bucket
is skipped and the Actions cache, P2P and the websites still supply every archive.

## Actions cache size and eviction

The repository's Actions cache is limited to 10 GB and GitHub evicts the least
recently used entries first (and any entry not used for 7 days). The mod archives
are tens of GB, so the cache holds only a subset: `--cache-limit-bytes` in `ci.yml`
(2.5 GiB) keeps pinned objects first, then the smallest, and never `.bik`; `mods` also
skips a link whose objects do not fit. The lanes fetch the rest themselves, from the
bucket, P2P or the website.

Archives that do not fit are still published to the artifact store, so the lanes stop
re-downloading them. With `--emit-stage-manifest`, `cache.py refresh` acquires such an
archive (local, artifact store, bucket, P2P, website; Nexus and other blocked links are
skipped as before) only when the store lacks one of its objects or is about to expire it,
moves the object to `<cache-dir>.stage-only` (a sibling of the cache directory, never part
of the saved cache; `--stage-dir` overrides it) and lists it in the stage manifest. Pruning
still runs before `actions/cache/save`, so the saved cache stays under `--cache-limit-bytes`.
`--stage-max-bytes` (default 3 GiB, one pack) skips an object that is larger,
`--stage-total-bytes` (default 9 GiB, the three upload packs) caps one run, and `.bik` is
never fetched for this. The manifest records every object's hash once downloaded, so
`cache.py restore` in the lanes asks the artifact store for it by hash before the bucket,
P2P and the website. Objects that wait for a later run show as `over-cache-limit`.

The mod entry is keyed by content (`mod-archives-<hash>`), and `mods` deletes the older
`mod-archives-*` entries after saving, so exactly one mod entry stays. Keying by run id
used to add a ~3 GB entry per run; the total passed 10 GB and eviction removed the k1 game
cache (`steam-sealed-v3-excludingmovies-k1-Linux-…`) between `game` and the install
lanes, which failed on `fail-on-cache-miss`.

Budget (decimal GB, as GitHub counts):

| Entry | Size |
| --- | --- |
| Game k1 | ~3.0 |
| Game k2 | ~3.2 |
| Mod archives at 2.5 GiB (`2684354560`) | ~2.7 |
| Total | ~8.9 of 10 |

`--cache-limit-bytes` is the knob. At 3 GiB (`3221225472`) the total is ~9.5 GB, which
fits but leaves little room; 2.5 GiB leaves margin. If the games grow, lower it, to
2 GiB (`2147483648`) for example. Between the save and the prune the old and new mod
entries coexist (~5.4 to 5.9 GB), so the total briefly reaches ~11.6 to 12 GB. GitHub
applies the 10 GB cap asynchronously, and the prune runs right after the save to keep that
window short; it is the one place an eviction can still happen. If a game cache is evicted anyway, the lane
fails with "game cache evicted — re-run the game job": re-run the `game` job, which
refills it from the `game-<game>-sealed` backup artifact (Actions artifacts do not count
toward the 10 GB cache cap) and only falls back to Steam when there is no live artifact, then
re-run the failed lane.

## Artifact store (artstore)

An archive tier between the local Actions cache and the R2 bucket: mod archives
are kept as encrypted GitHub Actions *artifacts* (up to 90 days of retention, outside the
10 GB Actions cache cap), addressed by SHA-256. It only speeds things up. An empty
store, a missing token or key, an expired pack or an API error is a silent miss and the
next source (`--remote`, P2P, the website) supplies the archive. `.bik` movies are never
stored. Code: `tools/modsync-ci/artstore.py` (CLI), `cache.py` (tier hook), and the
local action `.github/actions/artstore-upload` (artifacts can only be uploaded from a
JavaScript action, not from a `run:` step).

How it is laid out:

- A **pack** is an artifact `artstore-pack-<run>-<attempt>-<n>` holding `<stored_sha256>.part`
  members: archives split into parts and sealed with `CACHE_ENCRYPTION_KEY`.
- An **index** is a small artifact `artstore-index-<run>-<attempt>-<n>` holding
  `index.json`, which maps each archive SHA-256 to its parts. A reader merges the newest
  three index artifacts and checks them against the live artifact list, so a pack that
  expired or failed to upload simply counts as missing.

Restore. `cache.py` uses the store whenever `ARTSTORE_TOKEN`, `GITHUB_TOKEN` or `GH_TOKEN`
is in the environment (there is no workflow toggle; `--no-artstore` turns it off by hand).
It tries the store first for an object the manifest knows by hash, then `--remote`, then
P2P. Every blob is re-verified against its SHA-256 after unsealing. `ci.yml` sets
`GITHUB_TOKEN: ${{ github.token }}` and `CACHE_ENCRYPTION_KEY` on the `cache.py` steps
of `mods`, `install` and `manual` only.

Publish (in `mods`). `cache.py refresh` is run with `--emit-stage-manifest`, which writes
`{path, sha256}` for the cached objects the store lacks or will lose soon. The steps after
the cache prune then:

1. `Stage artifact store packs` runs `artstore.py stage` into `$RUNNER_TEMP/artstore-pack`
   with a 3 GiB pack budget. If everything is already stored it writes nothing.
2. `Upload artifact store pack 1`, `2` and `3` upload `pack1`, `pack2` and `pack3` of the
   stage output through `artstore-upload`, each only when that pack exists. The 2.5 GiB
   cache limit normally fits one pack. `artstore.py stage --max-packs` (default and
   maximum 3, one per upload step) seals at most that many packs, so a fourth pack is
   never sealed and then thrown away. See [Deferred packs](#deferred-packs).
3. `Upload artifact store index` uploads the index fragment (named by `index_name`), only
   when at least one pack uploaded.

These steps run only on `push` (to `dev`), `schedule`, `release` and `workflow_dispatch`.
Pull-request runs from this repository may restore from the store but never publish.
Every publish step has `continue-on-error: true`, and a final step prints a
`::warning::` when any of them failed, so a store problem never fails `mods` or a lane.
The pack and index artifacts use `if-exists: skip`, so a re-run attempt that reuses a
name is not an error. Without `CACHE_ENCRYPTION_KEY` staging is skipped with a notice.

The last step, `Verify artifact store round trip`, fetches the smallest blob just published (or the smallest one in `artstore.py list` when nothing was published) back with `artstore.py fetch`, compares its SHA-256 and writes `artstore round trip OK: <sha prefix> <bytes> in <seconds>s` to the step summary; a failure only emits a `::warning::`.

<a id="deferred-packs"></a>Deferred packs. When more blobs are due than the
`--max-packs` upload slots hold, the surplus is not sealed. `stage` records each one under
`deferred` in the index fragment (`{sha256: {since, size}}`) and prints a `::notice::`
with the file and byte counts. The fragment is merged like any other index, so the next
`mods` run reads it: `cache.py refresh` fetches and stages the archives that hold a
deferred object first (before they compete for `--stage-total-bytes`), `stage_candidates`
lists deferred hashes first, and `stage` seals them ahead of anything new. A blob leaves
the list when it is stored; one that no run lists again is forgotten after 14 days. The
cap is the number of upload steps in `ci.yml`; do not raise `--max-packs` without adding
upload steps.

Staged totals and upstream failures. `cache.py refresh` logs `stage manifest: N file(s),
B bytes` and the largest objects; `artstore.py stage` logs the manifest total, the sealed
totals and one `pack <name> files=<n> stored_bytes=<b>` line per pack before any upload,
and both append a table to `$GITHUB_STEP_SUMMARY` when it is set. Every archive whose
upstream check failed, or that ends `stale-unknown`, gets one flushed line
(`cache: stale-unknown 797139bf host=example.com reason=ValueError HTTP 403`) and a row in
the refresh summary table (archive, outcome, status, host, reason). Reasons are the
exception class plus the HTTP status or network cause class, with no URLs or tokens.

`artstore refresh` (schedule and dispatch). Archives are stored for at most 90 days and
the store has no way to extend an artifact, so close-to-expiry blobs are copied into a
new pack. The job runs `artstore.py refresh-plan --within-days 21`, fetches those blobs
(at most 8 GiB per run; the rest wait for the next run), stages and uploads them as a new
pack plus index exactly like `mods`, and finally runs `artstore.py prune --apply`, which
deletes expired and superseded pack and index artifacts (with a 24 hour grace period,
keeping the newest five indexes). The prune is skipped only when the run is cancelled. A
failed delete fails the job, which is visible but affects nothing else.

Permissions. The workflow-level `contents: read` stays. `install` and `manual` set
`contents: read` and `actions: read` (list and download artifacts). `artstore refresh`
sets `contents: read` and `actions: write` (delete artifacts). `mods` keeps the
`actions: write` it already had for `gh cache delete`, which covers its reads; no other
job gets `actions`. Secret values are only passed in `env:` of the steps that use them and
`artstore.py` redacts the token and key from its messages.

By hand, from a checkout with a token that has `actions: read` (`GITHUB_REPOSITORY` set):

```sh
python tools/modsync-ci/artstore.py list
python tools/modsync-ci/artstore.py refresh-plan --within-days 21
python tools/modsync-ci/artstore.py prune          # dry run; --apply deletes
```

## Running it by hand

```sh
gh workflow run ci.yml -R kolto-labs/mod-builds --ref dev
gh run list -R kolto-labs/mod-builds --workflow ci.yml
```
