# Private installer verification for public mod-build guides

This repository's public K1/TSL guides are the subject of these jobs. The private
ModSync implementations are the tools used to verify them. This is a companion
pipeline for an associated project, not a general-purpose free compute relay.

`gh-private-ci init` generates six manual entry workflows in the repository you
adopt it into: `private-ci-{cli,gui}-{linux,windows,macos}.yml`. Each can select
Rust, TypeScript, or both. This repository does not ship them; its own CI is
`.github/workflows/ci.yml`. Standard public hosted runners currently
have no Actions compute charge; self-hosted machines have their own operating
costs. Encrypted artifacts still consume storage allowance; larger hosted runners
are billed even for public repositories. See [GitHub billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Account restrictions can still prevent allocation. Calling this public
workflow from a **private** repository does not change private caller billing.

## What stays private

Private repository, source revision, commands, and artifact settings live in
repository secrets. Source is downloaded as a pinned snapshot using `gh`, with
no persisted Git credentials or history. Command output goes to a private
temporary log, not public Actions logs. Build commands receive neither the source
download token nor unrelated workflow/Steam secrets. Only `age`-encrypted evidence
is uploaded; originals and source trees are never cached or uploaded.

Job names, actors, platform, timing, success/failure, workflow definitions, and
ciphertext size remain public. Encryption does not hide those facts. Repository
administrators and anyone allowed to modify secret-bearing workflows must be
trusted. This is not a sandbox for malicious private code. Debug/PR execution is
rejected before the private workload. Use reviewed immutable revisions and protect
the public branch and workflow changes.

The evidence directory is fixed to `artifacts/evidence`. It accepts bounded
JSON/log/text/Markdown/TOML/HTML, installer UI PNGs, and validated `trace.zip` files.
Game trees, Steam directories, original binaries/resources, checkpoints, and
unsupported file types are rejected, even if encryption would conceal them.
File filters cannot classify arbitrary renamed content; reviewed recipes must
produce receipts, never asset dumps or game screenshots.

## Configure this fork

Authenticate your authorized account with GitHub CLI. Create a fine-grained source token
with **Contents: read** on only the two private ModSync repositories. Do not use
the public repository's `GITHUB_TOKEN` to fetch those repositories.

Install the pinned encryption tool and keep the private identity on your own
machine:

```sh
python tools/private-ci/src/bootstrap_age.py --directory /your/private/tools/age
/your/private/tools/age/age-keygen -o /your/private/modsync.agekey
/your/private/tools/age/age-keygen -y /your/private/modsync.agekey
```

Windows binaries have `.exe` suffixes. Only the public recipient belongs in
`PRIVATE_CI_RECIPIENT`. Never commit, upload, or store the decryption identity in
the public repository. Protect and back up that identity; losing it loses evidence.

Set these repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `PRIVATE_CI_TOKEN` | Fine-grained read-only source token |
| `PRIVATE_CI_RECIPIENT` | Public `age1…` recipient |
| `PRIVATE_CI_CONFIG_RS` | Private JSON configuration for the Rust snapshot |
| `PRIVATE_CI_CONFIG_TS` | Private JSON configuration for the TypeScript snapshot |

Use [the configuration example](examples/config.example.json) as the schema.
Keep real configuration files outside this public checkout. Commands run from the
private source snapshot, using Bash on Linux/macOS and PowerShell on Windows.
`PUBLIC_CI_WORKSPACE` names the public tooling checkout; `PRIVATE_CI_SOURCE_SHA`
records the tested private revision. Recipes must install their own build/runtime
dependencies, stop on errors, and write only allowed receipts to the evidence path.

This template does not install a source token, Steam credentials, or actual project
configuration. Secret setup is required before private jobs can run.

Run from a local checkout:

```sh
python tools/private-ci/gh-private-ci run --repo OWNER/PUBLIC --platform linux --kind cli --implementation both
python tools/private-ci/gh-private-ci status --repo OWNER/PUBLIC
```

Update a private snapshot without editing public YAML, then dispatch:

```sh
python tools/private-ci/gh-private-ci refresh --repo OWNER/PUBLIC --config /your/private/rust-config.json --config-secret PRIVATE_CI_CONFIG_RS --source-directory /your/private/ModSync.rs
python tools/private-ci/gh-private-ci run --repo OWNER/PUBLIC --platform linux --kind gui --implementation rust
```

Use `--private-ref FULL_SHA` instead of `--source-directory` to select an explicitly
reviewed snapshot. The update sends JSON through stdin and prints neither the
payload nor the SHA. A failed secret update leaves the local file unchanged. No
public workflow is rewritten. Use `PRIVATE_CI_CONFIG_TS` for TypeScript and
`PRIVATE_CI_CONFIG` for generic single-project adopters.

The equivalent `gh workflow run private-ci-cli-linux.yml --repo OWNER/PUBLIC`
works too. Add `--field runner-mode=self-hosted` for an owner-controlled runner
registered with `self-hosted`, its OS label, and `modsync-session`. Public repositories
must not give untrusted pull requests access to those machines or credentials.
Prefer ephemeral runner lifecycles. Catchable interruptions stop owned processes;
SIGKILL, machine failure, or forced cancellation can still leave private temporary
inputs until the owner destroys or cleans the runner. Never expose that filesystem
as a public artifact directory.

Download encrypted artifacts as a repository reader, then decrypt locally:

```sh
gh run download RUN_ID --repo OWNER/PUBLIC --dir /your/private/evidence
/your/private/tools/age/age --decrypt -i /your/private/modsync.agekey -o evidence.tar.gz path/to/evidence.age
```

## Adopt it for another associated project

Fork the public companion/tooling, authenticate locally using `gh`, and create a
configuration for your own project. Generate six pinned callers with:

```sh
python tools/private-ci/gh-private-ci init --library kolto-labs/mod-builds --ref PUBLIC_TOOLING_40_CHARACTER_SHA --tool-path tools/private-ci --private-repo OWNER/PRIVATE --private-ref PRIVATE_40_CHARACTER_SHA --config /your/private/commands.json --directory /your/public/companion
```

The public library and its checkout are pinned to the same immutable revision.
The generated private config is outside the public checkout, mode `0600` on POSIX.
To configure the generic single-project callers, add `--configure-secrets --repo
OWNER/PUBLIC --token-file /your/private/source-token --recipient age1…`. Secrets are
passed to `gh secret set` through stdin; tokens are not command-line arguments.
That setup uses `PRIVATE_CI_CONFIG`, unlike this fork's two implementation configs.
Do not overwrite existing workflows without reviewing them; `--force` is explicit.

## Actual game runs

The preferred actual-game path is an isolated owner-controlled runner with an
installed baseline, tested without publishing or transferring game bytes.
File-only verification and game execution must be distinct recipes; none of these
generic workflows launches a game itself. Original mod archives remain separate
required inputs before a complete original mod-build installation can be claimed.

`src/steam_acquire.py` is a prepared acquisition adapter, **not enabled by the six
generated workflows**. It supports normal authenticated SteamCMD acquisition of
owned apps 32370/208580 on Linux/Windows, forcing Windows depots; native macOS
acquisition is unsupported. It does not bypass ownership, Steam Guard, or DRM.
Credentials must be injected only into the acquisition process using
`STEAM_USERNAME`, `STEAM_PASSWORD`, and possibly a current `STEAM_GUARD_CODE`.
Never paste them in chat or command arguments. Unproved Steam script escaping is
rejected, including quotes/backslashes/control characters in credentials.

A current Guard code may expire; concurrent Steam logins can invalidate sessions.
No account was authenticated, game downloaded, game executed, or game asset
uploaded during preparation of this adapter.

## Validation

```sh
cd tools/private-ci
python -m unittest discover -s tests -v
```

The 49 credential-free tests cover archive containment, secret stripping, private
logs, failure preservation, timeout cleanup, artifact restrictions, pinned callers,
and Steam admission/manifest checks. A separate local end-to-end check fetched a
real private snapshot and verified real `age` encryption/decryption. Those checks
establish the tooling, not a completed real-game build or publisher authorization.

## Use Actions artifacts as a private cache backend

GitHub does not expose a supported upload API for creating an artifact from an arbitrary process. The safe pattern is a two-phase workflow: a trusted job prepares encrypted files, `actions/upload-artifact` uploads a segment, and a second upload publishes a small index containing the segment artifact ID. `src/artifact_store.py` supplies content-addressed chunking, index validation, authenticated cross-run downloads, and garbage collection. It never uploads plaintext and never puts the token in a URL.

Each source archive must be encrypted once and retained as a separate stable age object. Re-encrypting one large tarball on every refresh would randomize its age nonces and defeat deduplication. The private payload command should update only changed objects under `{output-dir}`. Stage that directory:

```sh
python tools/private-ci/src/artifact_store.py stage \
  --input-dir "$RUNNER_TEMP/cache/objects" \
  --output "$RUNNER_TEMP/cache/chunks"
```

Upload `cache/chunks` with `actions/upload-artifact` and capture its `artifact-id` output. Merge that ID into the prior index with `make_index` (or the equivalent small Python step), upload the JSON index as a new artifact, and retain the index artifact ID as the current generation. Unchanged chunk hashes continue pointing at their original segment, so a 14-day refresh only uploads changed payloads. A consumer downloads the index and then runs `fetch-chunks`; every chunk is checked by size and SHA-256 before it is exposed to an installer. `gc` deletes only artifacts not present in the retained index set.

Artifacts remain subject to GitHub's repository quota and retention policy. Set a retention period that fits the account, keep only the current index plus the segment artifacts it references, and run garbage collection after a successful index publication. Artifact names, sizes, run metadata, and hashes remain visible to repository readers; the encrypted payload does not. This is an Actions-storage adapter, not a way to increase the account's quota or hide public repository metadata. The checked-in toolkit workflow proves the planner and integrity checks without credentials or game/mod data.
