# Full-guide archive cache

`tools/modsync-ci/cache.py` is a Python 3.10+ standard-library CLI shared by Rust
and TypeScript installer jobs. Run from this repository root. It reads Markdown
links in `content/k1/full.md` and `content/k2/full.md`, never executes guide text,
and never runs an installer. Workflow and installer adapters consume its JSON.

```bash
python3 tools/modsync-ci/cache.py discover --report discovery.json
python3 tools/modsync-ci/cache.py refresh --cache-dir .modsync-cache --report refresh.json
python3 tools/modsync-ci/cache.py restore --cache-dir .modsync-cache --destination downloads --report restore.json
```

Use repeated `--guide content/k1/full.md` / `--guide content/k2/full.md` to select
guides. Inline links, reference definitions and URL autolinks are recognized;
fenced and inline code are excluded. All HTTP links are inventoried, including
documentation links: unsupported or ambiguous links are reported unresolved.
Discovery is an inventory, not a claim that every link is an archive.

## Providers and freshness

Direct `.zip`, `.7z`, `.rar` URLs and explicit DeadlyStream download/attachment
endpoints are requested as archives. Landing pages are read with a 2 MiB limit;
only a unique explicit archive/download link can resolve automatically. Multiple
file options require an adapter. MEGA client-side decryption and Nexus authenticated
downloads are unresolved and require a provider adapter. There is no CAPTCHA
interaction, JavaScript execution, cookie scraping or arbitrary command hook.

Refresh resolves landing pages again when due, then sends conditional GET to the
actual archive endpoint using its saved ETag and/or Last-Modified. A verified local
object plus endpoint HTTP 304 establishes freshness. A full HTTP 200 must pass
archive integrity checks before publication; upstream ETag, Last-Modified or a
provider version/file ID is recorded. Version IDs alone never suppress a due GET.
Objects with no upstream metadata are retained as `stale-unknown` and the command
fails. A local SHA256 match alone cannot establish or renew freshness. Any failed
refresh invalidates the old freshness status while preserving the old bytes.

Successful checks remain reusable for at most 14 days (`--max-age-days` may shorten
this). `--force` checks immediately. Restore is offline and refuses expired,
unknown or corrupt entries. Run refresh on an every-other-week schedule and before
installation when restore is stale; scheduling belongs to the workflows. GitHub
cron has no biweekly primitive: a weekly trigger with this 14-day gate is suitable,
or an explicit alternating-week workflow gate. Preserve the cache between runs.

## Storage and installer contract

The cache directory holds `manifest.json` (schema 1, entries keyed by SHA256 of
the exact guide URL) and `objects/<sha256>` immutable content. Entries record
SHA256, size, archive extension, endpoint identity, sanitized endpoint, validators,
version ID, check timestamp and evidence/status. URL identity includes query and
fragment so different provider file IDs are not conflated. JSON URLs omit all
query parameters, fragments and credentials; errors omit network exception text.
Raw guide URLs are used only in memory. Treat cached upstream validators as
untrusted metadata and keep reports/cache private where appropriate.

Restore copies archives atomically to `<URL-id>.<zip|7z|rar>` and reports each
`id`, sanitized `url`, `file`, and `sha256`. Both Rust and TypeScript adapters must
map the exact guide URL through SHA256 to these filenames; the names are deliberately
independent of HTTP filenames. They should refuse an unsuccessful report before
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
pull before refresh/restore and push after refresh. Only manifest/objects transfer;
temporary download files are excluded. Configure credentials outside the guide and
CLI reports. `rclone` is optional for local/action-cache use. Backend transfer
failure fails the command. Use a dedicated remote prefix and serialize all remote
writers; rclone copy is not a distributed lock or transactional snapshot.

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
