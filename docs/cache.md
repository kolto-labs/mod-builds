# Full-guide archive cache

`tools/modsync-ci/cache.py` is a Python 3.10+ standard-library CLI shared by Rust
and TypeScript installer jobs. Run from this repository root. It reads Markdown
links in `content/k1/full.md` and `content/k2/full.md`, never executes guide text,
and never runs an installer. Workflow and installer adapters consume its JSON.

```bash
python3 tools/modsync-ci/cache.py discover --report discovery.json
python3 tools/modsync-ci/cache.py refresh --cache-dir .modsync-cache --report refresh.json
python3 tools/modsync-ci/cache.py restore --cache-dir .modsync-cache --destination downloads --report restore.json
python3 tools/modsync-ci/cache.py import-existing --resource-index /path/to/resource-index.json --archive-dir /path/to/archive-directory --cache-dir .modsync-cache --max-items 20
```

`import-existing` defaults to a dry run. It inspects at most `--max-items` exact
URL matched candidates (1–500), starting at `--offset` (default 0), joining only
an exact unique `HandlerMetadata.PrimaryUrl` to exactly one listed archive
filename. Unmatched guide URLs are counted separately and do not consume the limit. It requires the
listed filename to be a regular local file with no symlink or hard-link aliases,
and does not print provider URLs. Multiple index rows, multiple listed variants,
missing files, and unsafe paths are reported without choosing a variant. Use the
returned `next_offset` for the next batch; `truncated` indicates that more exact
URL matches remain. This lets batches cover all matched candidates.
An existing manifest association appears as `existing-association` in dry-run
output; that label does not claim the local bytes match it.

Pass `--apply` to check the listed file size against `--max-bytes` and available
space, preserving a 3 GiB reserve, then copy eligible archives into a temporary
cache object. Reads are bounded by the initial file size and `--max-bytes`. The
command calculates SHA256 while copying, runs the existing archive integrity
check on the staged copy, and links the verified copy atomically into
`objects/<sha256>`. The original archive is never moved or modified. An existing
manifest association is reported as `already-present` only when its SHA256 and
size match the imported file and its cache object verifies; otherwise the command
reports `manifest-conflict` without replacing the existing entry. The complete
manifest document, including other top-level fields, is preserved. A new manifest
entry records `status: stale-unknown`,
`evidence: local-import`, `metadata_unknown: true`, and a local `imported_at`; it
has no `checked_at`, endpoint validators, or upstream version claim. Import does
not use the network or `--remote`. It does not enable restore: restore continues
to require accepted freshness evidence until a separate integration changes that
gate. Existing manifest entries are preserved and reported rather than replaced.

Use repeated `--guide content/k1/full.md` / `--guide content/k2/full.md` to select
guides. Inline links, reference definitions and URL autolinks are recognized;
fenced and inline code are excluded. Only recognized archive URLs and provider
file-page shapes are inventoried; ordinary documentation links are excluded.
Discovery is an inventory, not a claim that every link is an archive.

## Providers and freshness

Direct `.zip`, `.7z`, `.rar` URLs and explicit DeadlyStream download/attachment
endpoints are requested as archives. Landing pages are read with a 2 MiB limit;
only a unique explicit archive/download link can resolve automatically. Multiple
file options require an adapter. MEGA client-side decryption and Nexus authenticated
downloads are unresolved and require a provider adapter. Google Drive and
GameFront also require adapters. R2 stores acquired bytes; it cannot resolve
these provider requirements. The client maintains an in-memory cookie jar and
sends a DeadlyStream Referer, without importing browser cookies, executing
JavaScript, interacting with CAPTCHAs or running arbitrary command hooks.

Refresh resolves landing pages again when due, then sends conditional GET to the
actual archive endpoint using its saved ETag and/or Last-Modified. A verified local
object plus endpoint HTTP 304 establishes freshness. A full HTTP 200 must pass
archive integrity checks before publication; upstream ETag, Last-Modified or a
provider version/file ID is recorded. Version IDs alone never suppress a due GET.
With ETag or Last-Modified, a validated HTTP 200 produces `fresh`. Without either,
it produces accepted status `verified`, `metadata_unknown=true` and evidence
`download-without-upstream-metadata`: a complete integrity-checked download, but
no validator for conditional reuse. Both statuses are reusable within the age
window. A local SHA256 match alone cannot establish or renew freshness. An item
refresh failure marks an existing entry `stale-unknown` while preserving its bytes.
Workflow transfer failures are reported separately and do not prove an upstream check.

Successful checks remain reusable for at most 14 days (`--max-age-days` may shorten
this). `--force` checks immediately. Restore is offline and refuses expired,
unknown or corrupt entries. With `--remote`, restore transfers backend data first
but never contacts providers. The implemented refresh workflow runs daily at
02:41 UTC and attempts only due entries using the 14-day gate. Access alone does
not renew `checked_at`; negative ages and ages of 14 days or more are rejected.

## Storage and installer contract

The cache directory holds `manifest.json` (schema 1, entries keyed by SHA256 of
the exact guide URL) and `objects/<sha256>` immutable content. Entries record
SHA256, size, archive extension, endpoint identity, sanitized endpoint, validators,
version ID, check timestamp and evidence/status. URL identity includes query and
fragment so different provider file IDs are not conflated. JSON URLs omit all
query parameters, fragments and credentials; errors omit network exception text.
Raw guide URLs are used only in memory. Treat cached upstream validators as
untrusted metadata and keep reports/cache private where appropriate.

Restore preserves sanitized original archive names from Content-Disposition,
then endpoint basenames, with a URL-ID-based fallback. The manifest stores
`archive_name`; legacy entries derive a name. It reports `id`, sanitized `url`,
`file`, and `sha256`. Adapters must use that mapping, not assume URL-ID filenames.
Invalid names, case-insensitive collisions and conflicting destination contents
fail closed; exact-name hash matches are accepted. Copied bytes are SHA256-checked
before publication. Adapters should refuse an unsuccessful report before
installing. Restore may produce a partial directory on failure: never treat directory
presence as success. No extraction or install is performed here.

ZIP integrity uses Python's ZIP CRC validation in a bounded subprocess; RAR/7z
require `7zz` or `7z t`. The integrity timeout applies to both paths.
HTML and unsupported formats fail closed regardless of filename or content type.
Downloads use same-directory temporary files, fsync and atomic replacement;
manifest and restored archive writes are also atomic. Interrupted downloads do not
publish manifest entries. Only one writer may use a cache at once; workflows must
serialize refresh/save jobs. Old objects are retained, without automatic pruning.

For GitHub `actions/cache`, restore/save the whole `--cache-dir`, with a rolling
cache key and restore prefix (GitHub cache keys are immutable). Both installer jobs
can read the same saved directory. Never run multiple concurrent writers.

Optional `--remote remote:path` uses an already configured `rclone copy` backend:
bulk pull of manifest and all objects before refresh/restore, push after refresh.
Only manifest/objects transfer;
temporary download files are excluded. Configure credentials outside the guide and
CLI reports. `rclone` is optional for local/action-cache use. Backend transfer
failure fails the command. Use a dedicated remote prefix and serialize all remote
writers; rclone copy is not a distributed lock or transactional snapshot.

## Implemented daily archive refresh

Source: [archive workflow](../.github/workflows/modsync-cache-refresh.yml) and
[`cache.py`](../tools/modsync-ci/cache.py). The workflow serializes writers in
`kolto-labs/mod-builds`. Scheduled batches attempt up to 20 due links; manual
dispatch accepts 1–10, default 10. Never-attempted links come first, then oldest
attempt. Persisted `refresh_attempts` rotate failures too, preventing unresolved
providers from monopolizing each batch.

Manual dispatch may optionally set `candidate-id` to one exact ID from the
`discover` report (64 lowercase hexadecimal characters), for example:
`gh workflow run modsync-cache-refresh.yml -f candidate-id=<id>`. A selected ID
must match exactly one currently discovered item; malformed or unknown IDs fail
before provider downloads. This refreshes only that item, even if it was already
attempted or is inside the freshness window. Leave the input empty for the normal
due/oldest batch. URLs, guide replacements, and other selector values are not
accepted.

With private R2 configured, it pulls only `manifest.json` (16 MiB cap), then
fetches the selected entry's previous object individually. Size, SHA256 and
archive integrity must pass before conditional upstream refresh. A missing
object can trigger a new upstream download; corrupt restored bytes fail the item.
Before publishing an object, it verifies content-hash identity and fetches any
existing remote copy to check size/SHA256. It persists objects before the complete
manifest, then evicts local objects. An always-run persistence step also attempts
to preserve completed objects and the manifest after failure.

The worker reserves 3 GiB of disk, caps downloads by available space, and stops
after about 35 minutes or on low disk. Per-item download/backend transfers are
bounded at 600 seconds, integrity at 300 seconds, with a 20-second socket timeout
and one request retry. Only sanitized totals/failure stages are published; private
diagnostics are removed. No public archive artifact is created. Without R2,
downloads are runner-local only: this workflow does not save an Actions fallback.

This incremental per-object pull is workflow behavior, not generic CLI behavior.
The [automated](../.github/workflows/modsync-install.yml) and
[manual](../.github/workflows/modsync-manual.yml) consumers currently use the CLI's
bulk restore with read-only R2 credentials, or unseal an existing
`modsync-archives-sealed-v1-` Actions-cache fallback when no remote is configured.

## Why GitHub cache for games and R2 for mod archives?

The stores serve different payloads. The
[Steam workflow](../.github/workflows/modsync-steam-acquire.yml) keeps authenticated
encrypted clean-game baselines in Actions cache, enforcing 4 GB for K1 and 5 GB
for K2: 9 GB combined with nominal 1 GB headroom in the default shared 10 GB
repository cache allowance. Over-budget baselines fail before save. Private R2
holds the larger mod archive object corpus. A roughly 61 GB measured local
archive corpus exceeds that default allowance; it is neither a current R2
occupancy measurement nor the exact set required by both guides. Not all local
archives need to be uploaded or downloaded for each install. External storage
could also hold game baselines; the current split is an implementation choice.

GitHub currently documents a **default 10 GB per repository**, eviction after
**more than seven days without access**, and oldest-access eviction at capacity.
Additional paid cache storage can be configured; 10 GB is not a fixed platform
maximum. Steam acquisition runs daily at 03:17 UTC when `STEAM_AUTH_READY=true`:
daily restore refreshes access, while acquisition normally waits 14 days. Manual
dispatch forces acquisition. Access does not renew the acquisition timestamp or
prevent capacity eviction, particularly while old immutable keys coexist with
replacement baselines.

Artifacts and Git LFS are not free unlimited alternatives. Artifact retention
and plan-dependent storage billing apply; LFS meters storage and download
bandwidth. R2 also charges beyond its free storage/operation allowance; Standard
storage includes 10 GB-month monthly and has no egress bandwidth charges.
Current official references:

- [GitHub cache limits and eviction](https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows#usage-limits-and-eviction-policy)
- [Actions billing and artifact storage](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Git LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)

## Movie-free encrypted game cache: v3 scope

Before sealing, acquisition verifies its complete staging copy against the source
inventory, then removes every case-insensitive `*.bik` file at every depth from
the generated cache copy. The canonical acquired baseline remains intact. This
saves cache space, not the initial Steam download, and does not filter mod archives.

Only encrypted `sealed/*.seal` is saved under
`steam-sealed-v3-excludingmovies-<game>-<OS>-...`; authentication/configuration
state is outside that clean-game tree. The envelope includes the preserved source
inventory, filtered inventory, `refreshed-at`, and `cache-scope.json` containing
schema, game, excluded pattern/count/bytes and source-receipt SHA256.

Both install consumers require this v3 scope and an age strictly below 14 days.
They verify the original receipt hash, derive the exact filtered inventory by
excluding BIK paths, check exclusion totals, reject remaining BIK paths, and
verify both restored tree and independent install copy. This proves a filtered
baseline, not a complete playable installation. See [Steam cache scope](steam.md#encrypted-game-cache-scope).

## Completion evidence

As of 2026-10-15, R2 has partial actual archive objects; manifest references and
discovery counts do not prove that every object exists or every required archive
is available. The latest inspected refresh
[run 38770674881](https://github.com/kolto-labs/mod-builds/actions/runs/38770674881)
failed at `persist-manifest`. Final totals reported zero new verified downloads,
one unresolved entry and `refresh_complete=0`, despite final persistence reporting
success. These per-run totals are not a bucket inventory.

Neither this state nor a successful bounded batch establishes that all 357
candidates were downloaded, that Nexus/MEGA requirements are resolved, or that a
full install is green. Confirm actual objects and unexpired accepted entries for
every required guide choice; installation needs its own successful receipts.

## Limits and exit codes

- `--timeout 30`: per-socket timeout, seconds.
- `--retries 2`: request retries, restricted to 0–5; exponential backoff capped at
  8 seconds; retries only transient transport errors and HTTP 408/429/5xx listed
  in the implementation. Mid-body failures fail the item; rerun refresh to retry.
- `--download-timeout 1800`: total streamed body deadline, checked between reads
  (an individual read can additionally consume the socket timeout).
- `--max-bytes 21474836480`: maximum archive size (20 GiB).
- `--integrity-timeout 300`: external archive validation timeout, seconds.
- `--backend-timeout 1800`: each rclone transfer timeout, seconds.
- `--report PATH`: atomic JSON file in addition to JSON stdout.
- `--destination PATH`: required for restore.
- `--cache-dir PATH`: default `.modsync-cache`.

Exit 0 means successful discovery or all requested refresh/restore entries accepted;
1 means unresolved/stale entries or an operational failure; 2 means invalid CLI
arguments. Discovery can succeed while containing unsupported provider links.
Refresh/restore reports list failures explicitly and do not cache HTML as archives.
This CLI does not provision Rust/Node dependencies or modify either installer.
