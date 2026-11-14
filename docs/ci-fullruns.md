# Repository-owned full-run CI

This repository owns its entire CI variant: workflow entry points, local reusable stages, Python integration tools, policies and test fixtures. No workflow calls another repository's workflow. Each repository can evolve its variant independently.

## The repository-specific boundary

| Concern | ModSync.rs variant | mod-builds variant |
| --- | --- | --- |
| Subject being tested | Triggering engine commit | Triggering guide commit |
| Engine revision | Own `github.sha` | Reviewed exact counterpart SHA |
| Guide revision | Reviewed exact counterpart SHA | Own `github.sha` |
| Counterpart pin | `.github/fullrun.json` guide pin | `.github/fullrun.json` engine pin |
| Push paths | CI/tooling plus Rust production inputs | CI/tooling plus guide/user-script inputs |
| Local stages/tools | Owned by ModSync.rs | Owned by mod-builds |
| Actions caches and new artifacts | Owned by the execution repository/ref | Owned by the execution repository/ref |
| Existing domain CI | Production gates, native synthetic proofs, manual packages | Target-owned engine-check, native synthetic proof and manual package variants; metadata/release stay separate |

The small role configuration selects the data boundary; it does not fetch shared CI code. Local `./.github/workflows/...` calls and `github.repository`/`github.sha` checkouts are deliberate: every caller is local. The plan rejects execution under a foreign repository.

All counterpart revisions must be 40 lowercase hexadecimal characters. Update the reviewed pin in a commit or pass an exact manual `counterpart_sha`. The engine, guides, run, plan and attempt identities are recorded; a default-branch checkout cannot silently choose the tested engine.

## Entry points and composition

`Full-run CI` is the operator entry point. Pushes and PRs execute `check`: local Python/tooling/workflow checks, with no game-decryption, diagnostics, Steam or provider credentials passed to that path. Manual dispatch selects:

| operation | Required stages | Side effects |
| --- | --- | --- |
| check | Plan + tooling | Public contract-only plan/verdict |
| full | Plan + tooling + games + archives + install matrix + P2P loopback | Explicitly admitted acquisition/install; optional producer cache writes |
| games | Plan + tooling + baseline preparation/verification | Optional verified sealed-cache and encrypted-backup publication |
| archives | Plan + tooling + archive preparation | Optional bounded public-mod cache save |
| install | Plan + tooling + requested install lanes | Consume existing verified inputs; producers intentionally skipped |
| seed | Plan + tooling + reviewed staged import | Dry-run by default; explicit import uses the seed environment |
| p2p | Plan + tooling + production CLI loopback | Random synthetic transfer and wrong-hash rejection |

Select K1/TSL/both and CLI/GUI/both. Install matrices use `fail-fast: false`; valid lanes still attempt recovery when another producer/lane fails. The final collector requires every job in the requested scope to succeed and preserves a failed verdict. Skipped, cancelled, absent and incomplete required work fails that scope. Nonproducer operations ignore the shared producer-write checkbox.

The stage workflows are reusable **inside this repository**. The operator entry point can run each stage separately without duplicating its implementation. Full runs stay manual; this change does not enable periodic downloads, mutate repository settings, or publish a release.

## Configuration and credentials

| Setting | Purpose |
| --- | --- |
| `.github/fullrun.json` | Owned role, repository identities, immutable counterpart pin and inherited-toolkit provenance |
| `CACHE_ENCRYPTION_KEY` | Authenticate/decrypt sealed game and retained archive objects |
| `DIAGNOSTICS_ENCRYPTION_KEY` | Retain private stage evidence as authenticated ciphertext |
| `MODSYNC_SOURCE_TOKEN` (optional) | Explicit source checkout and locked dependency prefetch where access is needed |
| `ARTSTORE_READ_TOKEN` (optional) | Read an explicitly configured legacy artifact repository |
| `FULLRUN_ARTIFACT_REPOSITORY` (optional variable) | Optional legacy artifact read namespace; new publication remains in this repository |
| `MOD_CACHE_BUCKET`, `MOD_CACHE_S3_ENDPOINT` (optional) | Existing S3-compatible archive store, not a required new service |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID`, `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | Read-only archive consumers |
| `SEED_STAGING_RCLONE_CONF_B64`, write credentials | Existing staged import; `mod-cache-seed` environment; no implicit seed during installs |
| `GAME_SEED_URL_K1/K2`, `GAME_SEED_KEY_K1/K2`, `GAME_SEED_SHA256_K1/K2` | Reviewed owned game seeds |
| `FULLRUN_ALLOW_STEAM` (optional variable) | Explicitly enable fallback acquisition in the designated account-owning variant |
| `STEAM_*` | Only the opted-in producer fallback uses these; single-use code consumption remains opt-in |

Manual game materialization requires the cache key; private archive/seed/install evidence requires the diagnostics key. Missing required configuration fails admission rather than yielding a green install. Configure existing secrets through the repository's normal authorized process; this implementation does not read/change their values.

Use separate Steam accounts or designate a single acquisition writer for an account shared across repositories; other variants restore verified seeds/backups. GitHub concurrency groups serialize within a repository, not across repositories. A public PR cannot receive these private input keys simply by changing CI code. Native/synthetic/package workflows remain manual where already intended.

Private source tokens are scoped to checkout/dependency prefetch and never written to persistent global Git configuration. The installer receives a scrubbed environment without cache keys, provider keys or runtime tokens. This environment boundary is not a sandbox for malicious approved source; execute only reviewed engine/guide revisions.

## Storage and evidence

Game accelerators/backups contain authenticated ciphertext. Never add decrypted games, private source, credentials or private evidence to Actions cache paths. Public repository caches are readable by untrusted PR code. The archive accelerator contains only the approved mod archive input inventory.

Existing cache keys/formats, archive selections and seed pins are retained. No workflow deletes old caches/artifacts or enables retired mod pack publication/renewal. Artifact quota and retention still apply; another workflow run does not create more allowance.

Current-repository game backups are read before an optional legacy namespace. A verified legacy backup may be reused, but it cannot pretend that a current-repository backup was published. Every candidate baseline must pass unpacked-byte and required-module validation before it is used or published. The installed game tree and archive/extraction workspaces are separate from the small encrypted evidence directory.

A cache save is an accelerator, not a portable output across repositories. Migration of existing bytes is separate operational work; preserve original copies and prove exact restoration before changing read ownership or removing a source.

## Proof boundaries

A complete install requires an installed Rust report, matching requested mode and guide digest, positive component coverage, no omitted components, no partial result and zero reconciled lane errors. GUI subset/widescreen omissions fail complete-guide coverage. Useful partial diagnostics remain retained as failures.

The inherited runner still does its existing conversions, reviewed omissions, quarantine and GUI receipt checks. This integration does not claim independent app-selection parity, independent creator journeys, cross-lane per-mod barriers or native-platform full-guide parity. Those product/harness capabilities remain separate work. `cross_lane_parity: unverified` is explicit in plans/verdicts.

Synthetic CLI/native-GUI proofs use fabricated payloads and independent expected bytes. They do not acquire real mods or install licensed game data. P2P loopback is not external reachability/seeding proof. Manual package artifacts are not releases.

Rust tests and custom Rust harnesses remain frozen. Production builds use only lib/bin targets; no `--all-targets` or Rust test execution is introduced.

## Maintenance

Run the credential-free checks from the repository root:

```sh
python -m pip install pykotor==2.3.12 PyYAML==6.0.2
python -m unittest discover -s tools/modsync-ci -p 'test_*.py'
actionlint -config-file .github/actionlint.yaml .github/workflows/fullrun*.yml .github/workflows/cache-*.yml
```

The tools/format readers and immutable fixture corpus originate at Odyssey commit `1bbca57afc74c102bce6075cd40da205f98959e3`. They are now locally owned copies, not fetched at runtime. Legacy real-guide tests read their pinned fixture corpus under `tools/modsync-ci/fixtures`, not whichever live guide revision happens to be tested. Obsolete TypeScript/R2 YAML-wording assertions are replaced by tests of these variants' actual routing, ownership, credential, coverage and stage boundaries.

Keep behavior in the Python adapters and reusable local stages; keep event admission, runner choice and explicit secret mapping in YAML. Update this repository's tests/docs with its own changes. Cross-repository synchronization is a reviewable maintenance choice, not an execution dependency.

Workflow dispatch files generally must first exist on the default branch to appear in the normal Run workflow UI. Until these feature branches are integrated, push-triggered checks can validate the graph; no full-game run is claimed merely because a branch or workflow file exists.
