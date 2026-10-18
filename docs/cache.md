# Caches

CI keeps two caches and an optional bucket. They only make runs faster; the
guides in `content/k1/full.md` and `content/k2/full.md` decide what gets installed.
A missing or stale cache is refilled from the next source. A lane stops at restore
only when no source yields a required archive (see
[Source order](#source-order-per-archive)).

| Store | Holds | Lives in | Written by |
| --- | --- | --- | --- |
| Game cache | One encrypted, movie-free copy of K1 or K2 | Actions cache, key `game-<game>-v1` | `game` job |
| Archive cache | Mod downloads named by the guides, at most 2.5 GiB, one entry kept | Actions cache, key `mod-archives-<hash>` (hash of `manifest.json` without timestamps), restored by the `mod-archives-` prefix | `mods` job |
| R2 bucket (optional) | Archives and `manifest.json` | S3-compatible bucket (Cloudflare R2 in CI), prefix `modsync-archives` | `mods` job, only when the bucket secrets exist |

The game cache is described in [steam.md](steam.md). The archives add up to tens
of GB, far over what the Actions cache can hold, so `cache.py refresh` keeps only
what fits `--cache-limit-bytes 2684354560` (2.5 GiB): pinned objects first, then the
smallest. A link whose objects do not fit is reported `over-cache-limit` and its archive
is fetched by the lane that needs it. The optional bucket can hold the full set; an
over-limit archive that the bucket does not hold either is downloaded from its website
by every lane that needs it.

The repository's Actions cache is capped at 10 GB with LRU eviction, shared with the game
caches (K1 ~3.0 GB, K2 ~3.2 GB). 3.0 + 3.2 + 2.7 (2.5 GiB) is ~8.9 GB. `--cache-limit-bytes`
is the knob: 3 GiB gives ~9.5 GB, which is tight; lower it (2 GiB, `2147483648`) if the
games grow. See [ci.md](ci.md#actions-cache-size-and-eviction).

## Source order per archive

`cache.py` (`acquire`) takes the first of these that yields a verified archive. In
short: cached copy, revalidated by a conditional GET against the website; when it
changed or is missing, the optional bucket, then P2P, then the website; when the
website cannot be reached, the last cached copy (`stale-local`). Unchanged files are
never downloaded again:

1. **Local copy from the restored Actions cache.** A valid copy (hashes, sizes,
   integrity and reviewed selection all match) is revalidated by a conditional GET
   (ETag or Last-Modified): a 304 reuses it. A pinned entry is never revalidated.
   A copy with no validators is reused without a request while it is under
   `--max-age-days` old (14 days, the maximum); after that it is downloaded again.
2. **R2 bucket,** only when `--remote` is set (the workflow sets it when the
   bucket credentials and `MOD_CACHE_BUCKET` exist), and only for a hash the
   manifest or a seed pin already knows. An unreachable bucket is skipped, not an
   error.
3. **P2P,** only when the archive has a `p2p` block with a `magnet` in
   `tools/modsync-ci/seed_pins.json` (see `p2p_fetch.py`). It runs
   the ModSync.rs binary (`MODSYNC_BINARY` or
   `.modsync-source/target/debug/modsync`) as
   `torrent-download --sha256 ... --no-proxy` and needs a reachable seeder listed in
   `p2p.peers`; runners cannot accept inbound connections. No pin carries a `p2p`
   block today, so this step is inert.
4. **The website** (the guide link's host; see [Freshness and providers](#freshness-and-providers)).

Whatever lands is re-hashed and integrity-checked before use. If a valid local copy
exists but the website cannot be reached to revalidate it, the last valid local copy
is used for both `restore` and `refresh` (not with `--force`) and the report source is
`stale-local`; a warning without URLs is printed. A `stale-local` refresh leaves the
manifest entry as it was (`checked_at` and validators are not bumped), so the next run
asks the website again. The command fails a link only when no source yields the archive
(`unresolved`). `.bik` is never cached: `kept_objects` skips any object with a
`.bik` name.

`.bik` movies are never cached. `game_cache.py pack` deletes them before sealing,
and `unpack` fails if the restored tree does not match the recorded inventory. The
one mod download that is mostly video is also never cached (see
[Never cached by policy](#never-cached-by-policy)).

## Backend and names

The optional archive bucket is reached through `rclone`. Each job that uses it
defines an rclone remote named `MODCACHE` (type `s3`, provider `Cloudflare`) from
the values below. Write and read credentials are separate pairs. Secrets and
variables only; never put values in the repository. When the read key or
`MOD_CACHE_BUCKET` is empty, `install` and `manual` pass no `--remote` and skip the
bucket.

| Name | Kind | Used by |
| --- | --- | --- |
| `MOD_CACHE_S3_ACCESS_KEY_ID`, `MOD_CACHE_S3_SECRET_ACCESS_KEY` | secret | `mods` (write pair) |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID`, `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | secret | `install`, `manual` (read pair; they get no write pair) |
| `MOD_CACHE_S3_ENDPOINT` | secret | all of the above |
| `MOD_CACHE_BUCKET` | variable | all of the above |
| `CACHE_ENCRYPTION_KEY` | secret | `game`, `install`, `manual`: seals and unseals the game cache only |

`CACHE_ENCRYPTION_KEY` (at least 32 bytes) is not used for the archive bucket.
Archive objects are stored as downloaded, named by their SHA-256.

## Layout

`manifest.json` plus `objects/<sha256>`, in the Actions cache directory and, when
configured, in the bucket. Manifest entries are keyed by the SHA-256 of the guide
URL and record the object hash(es), size, type, upstream validators, the last
attempt and the freshness evidence. Bucket objects are copied with `--immutable`
and nothing is pruned there automatically; the Actions cache directory is pruned
to the 3 GiB limit on every refresh. The `mods` job uses the `mod-cache-writer`
concurrency group so only one refresh writes at a time.

- A download that is not an archive (see [Loose files](#loose-files)) is recorded
  with extension `file`.
- A post that yields several files stores several objects under the one URL id,
  each with its listed file name (and row id, if pinned).

## Refreshing: the `mods` job

Runs on every trigger except fork and Dependabot pull requests, which get no
secrets. It is `continue-on-error`, so a failure never blocks an install. In order:

1. Restore the Actions cache (`mod-archives-` prefix).
2. Optional bucket refresh (`continue-on-error`; needs the write pair and
   `MOD_CACHE_BUCKET`): `refresh_archives.py --max-items 500`, described below.
3. Archive cache refresh (`continue-on-error`): `cache.py refresh --cache-dir
   <dir> --cache-limit-bytes 2684354560`, with `--remote` when the bucket is
   configured. It follows the [source order](#source-order-per-archive),
   revalidates every archive and prunes to the limit (pinned first, then smallest).
4. Compute the key `mod-archives-<hash>`: the first 16 hex digits of the SHA-256 of
   `manifest.json` with `checked_at` and `imported_at` removed (sorted keys), so unchanged
   content gives the same key. The job exposes it as the output `archive_cache_key`.
5. Save the Actions cache under that key (`if: always()`). A `lookup-only` restore skips
   the save when the key already exists, as entries are immutable; a racing duplicate logs
   "Failed to save: Unable to reserve cache ... another job may be creating this cache" at
   info level. A skipped or "already exists" save is not an error.
6. Prune (`continue-on-error`): once the current key is confirmed present, `gh cache list
   --key mod-archives-` and `gh cache delete` every other key, so one mod entry remains.
   `game-*` and `steam-sealed-*` are never touched. This needs `permissions: actions: write`
   on the `mods` job only.

`install` and `manual` only restore the Actions cache; they never save it.

`refresh_archives.py` (step 2) maintains the optional bucket:

1. Pull `manifest.json` from the bucket (not the objects).
2. Drop links listed under `uncached` in `archive_selections.json` (see
   [Never cached by policy](#never-cached-by-policy)): they are never contacted and
   any manifest entry they have is removed. Pick up to 500 due links from both
   full guides: never attempted first, then the
   oldest attempt. A link is due unless it was verified within the last 14 days and
   its stored files still match the reviewed selection. Stop early at the time
   limit (2100 s) or when under 3 GiB of disk is free.
3. For each link: restore that link's existing objects and verify them, refresh it
   with `cache.py` (conditional GET where the host gives validators), verify the
   result, upload the object and the manifest, then delete the local copy. The
   size cap for one link is the smaller of the free disk minus the 3 GiB reserve
   and 20 GiB (`MAX_LINK_BYTES`). The disk term alone used to be the cap, which on
   a large runner was higher than 20 GiB and let a 25.7 GB post through.

Unresolved links (login walls, dead links, CAPTCHAs, non-archives, oversized
downloads, posts needing a selection) are reported as totals with a reason class,
not as errors. The script exits 1 only when the backend is unreachable, the
manifest is corrupt or an upload does not persist. If the `MODCACHE` access key,
secret key and endpoint are not all set, or `MOD_CACHE_BUCKET` is empty, it logs a
notice or warning and exits 0 without refreshing. Output never contains URLs,
remote paths or secrets.

To retry one link: `refresh_archives.py --max-items 1 --candidate-id <id>`, with the
ID from `cache.py discover`.

## Restoring: install and manual jobs

```sh
python tools/modsync-ci/cache.py restore --guide content/<game>/full.md \
  --cache-dir <dir> --destination <archives> \
  [--remote MODCACHE:<bucket>/modsync-archives]
```

`--archive-map <path>` (restore only) also writes `{guide link URL: restored filename
or [filenames]}` for restored links, for `modsync convert --archive-map`.

Install and manual jobs restore the Actions cache first (key
`needs.mods.outputs.archive_cache_key`, else the `mod-archives-` prefix when `mods`
failed or was skipped), then run `cache.py restore` against it, with `--remote` only when the read pair and
`MOD_CACHE_BUCKET` exist. They never save the cache. For every link in the guide,
restore takes the first source that yields a verified archive (see
[Source order](#source-order-per-archive)), then:

- checks the hashes, sizes and archive integrity of the object and copies each
  into a lane-local directory under its original file name. A duplicate file name,
  a hash mismatch, or a loose file that is not a reviewed selection fails that
  link.
  When two links under one guide mod heading would restore to the same stored name,
  the link with a reviewed seed pin keeps the name (otherwise the link whose own URL
  file name it is, otherwise guide order) and each other link gets
  `<stem>-<first 8 characters of its id>.<ext>`. Stored names and manifests are not
  changed, and the same name under different mod headings still fails.

A link the manifest does not know yet is fetched from the website like any other
(there is no pre-populated-cache requirement), and a repaired or newly fetched
entry is written to the local manifest. Because the lanes do not save the Actions
cache, that repair lasts for the run only. The report's `source` field for each
link is `local`, `revalidated`, `recovered` (bucket or P2P), `website` or
`stale-local`.

A link fails (`unresolved`) when no source yields a valid archive: it is not in the
local cache, the bucket and P2P cannot supply it, and the website cannot be
fetched or is blocked (login walls, Nexus, GameFront, CAPTCHAs).

A link listed under `uncached` is not looked up in the manifest at all. Restore
reports it with `status: "uncached-by-policy"` and a reason that starts with
`intentionally not cached by policy`, so it is distinguishable from `unresolved`
(a link the cache failed to supply), and it still counts against the exit code.

If any link remains unresolved or uncached by policy, the command exits 1 and the job stops at the
restore step, before any install work. This is the only hard failure: a lane cannot
start with a guide whose archives are not all present, but a stale cache, an
unreachable bucket, an inert P2P step or an unreachable website with a valid local
copy do not fail it.

The one exception is opt-in and per lane: `--allow-uncached-by-policy VALUE`
(repeatable, `restore` only). `VALUE` is one of:

- `SECTION`: the exact `##` heading of a guide section the lane does not install.
  Covers every `###` step under it.
- `SECTION::STEP`: the exact `##` heading, then `::`, then the exact `###`
  heading of one step (for example
  `--allow-uncached-by-policy "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching"`).
  Covers only that step. Matching is exact and case-sensitive; an empty side,
  a missing side or more than one `::` is a usage error.

A policy-uncached link is then reported as `uncached-skipped` and does not fail
the command only if every `###` step that references it is covered by some
`VALUE`. A reference in any other step or section, under no `###` step, or in no
step at all keeps `uncached-by-policy` and exit 1; so does every unresolved link
that is not on the `uncached` list. The step form claims nothing about the rest
of its section, so a second step that references the same link, or another
policy link in a neighbouring step, still fails unless it is named too.
The report lists each skipped step in a top-level `skipped_steps` array
(`guide`, `section`, `heading`, `url`, `reason`), and the entry carries the same
steps in `skipped_steps`. Use `--report` to keep it with the lane's artifacts.
The flag asserts what the lane installs; restore cannot check that, so pass it
only after confirming the lane never installs the named section or step (see
[ci.md](ci.md#install-lanes-and-uncached-links)).

## Freshness and providers

- A conditional GET (ETag or Last-Modified) answered with 304 for a verified
  object counts as fresh. A full download must pass the archive check first. A
  failed `refresh` marks the entry stale and keeps its bytes; a failed check on
  `restore` falls back to the last valid local copy. A download without
  upstream validators is accepted as verified, is reused without a request for
  14 days (`--max-age-days`, at most 14) and is downloaded again after that.
  Pinned entries are never revalidated.
- Hosts:

  | Host | Handling |
  | --- | --- |
  | Direct `.zip`/`.7z`/`.rar` links | Plain HTTP |
  | DeadlyStream | File page, then the attachment row(s); sends a `Referer`; several files need a reviewed selection |
  | Dropbox | Fetched directly with `dl=1` (any `dl`/`raw` query is replaced) |
  | MEGA | `megatools`; the tool must produce exactly one file |
  | Google Drive | `gdown`; exactly one file |
  | Nexus Mods | Never fetched; see [Known gaps](#known-gaps) |
  | GameFront | Never fetched; see [Known gaps](#known-gaps) |

  The client does not run JavaScript and never touches CAPTCHAs.
- A DeadlyStream file page that comes back as HTML with no download links (a degraded or
  transient variant of the page) is fetched again, up to `--retries` more times (default 2)
  with 4 s, 8 s, ... backoff capped at 20 s. Reasons stay distinct: a failed fetch reports
  `HTTP <status>` or `network request failed`; a page that stays empty reports `landing page
  returned no download links after retries; ...` (reason class `network` in the bucket refresh
  totals, not `adapter-required`); a reviewed `archive_selections.json` file that is not among
  the listed files reports `reviewed archive selection for file <id> matches 0 ...` and is
  never retried, nor is a CAPTCHA/challenge page.
- `cache.py import-existing` copies archives you already have locally into the
  cache as unconfirmed entries (dry run unless `--apply`). Restore ignores them
  until a refresh confirms them.

`cache.py` takes `discover`, `refresh`, `restore` and `import-existing`, plus
`--guide` (repeatable), `--cache-dir`, `--remote` and `--report`. See `--help`.

## Integrity checks

- ZIP: CRC test of every member with Python `zipfile`. If `zipfile` rejects the file
  (for example a zip whose member paths use backslashes), the check falls back to
  `7zz`/`7z t`, then `bsdtar`, and the file is rejected only when no available
  tool accepts it.
- RAR: `unrar t` if installed, then `7zz`/`7z t`, then `bsdtar`. A tool that fails
  or cannot run falls through to the next; the file is rejected only when every
  available tool fails.
- 7z: `7zz`/`7z t`; a failure from 7-Zip is final. `bsdtar` is used only when
  7-Zip is not installed.
- The type comes from the file's magic bytes. HTML pages and unknown formats are
  rejected whatever the file name says.
- A file fetched by MEGA/Google Drive tools, and any loose file, must be at least
  64 bytes. Smaller files are treated as error pages.

## Loose files

Some posts serve one file that is not an archive (an installer executable, a
single `.tga`, a `.dlg`). Such a download is accepted only when
`archive_selections.json` lists it with `"loose": true`. It is stored with
extension `file` and checked only for the 64-byte minimum and for not starting
with `<` (HTML). Its name must match the reviewed name exactly; restore refuses a
`file` object whose link has no matching loose selection.

## Never cached by policy

Top-level `uncached` in `archive_selections.json` is keyed by DeadlyStream file id
(or file page URL) or, for Nexus Mods, `nexus:<mod id>` (any game) or
`nexus:<game>/<mod id>` (for example `nexus:kotor/1214`; the game is the first path
segment of the mod URL, compared case-insensitively). Each entry needs a non-empty
`reason`. Only reviewed entries belong here: a link is listed because the guide
documents it as optional or unusable for the lane and nothing the build installs
reads its output, not because it is hard to download. A listed link is never
fetched, never uploaded and never restored:

- `refresh_archives.py` removes it from the work list before any network or bucket
  access, deletes its manifest entry and `refresh_attempts` record, and counts it
  as `uncached_by_policy` in the summary. Objects an earlier run already uploaded
  stay in the bucket as unreferenced orphans; purge them by hand.
- `cache.py refresh` reports `uncached-by-policy` (not a failure), `import-existing`
  never copies it, and `refresh_one` raises before opening a connection.
- `cache.py restore` reports `uncached-by-policy` and exits 1 (see above) unless `--allow-uncached-by-policy` names the section (or `SECTION::STEP`) covering
  every step that references the link; the status is then `uncached-skipped`.

Today the list holds three reviewed entries, each a reviewed decision with its
reason in `archive_selections.json`:

| Key | Link | Why it is never cached | Referencing steps (`##` / `###`) |
| --- | --- | --- | --- |
| `2503` | DeadlyStream K2 cutscene videos | Bink video, about 25.7 GB for the pinned main and `_mods` files; `.bik` is never cached, the CI game baseline has no movies and nothing else in the guide reads them. The guide says to use only one K2 cutscene mod, and the alternative (Nexus 1066) is pinned. Its `selections` entry stays so the exact files are on record if the policy is ever lifted | K2: `Enhanced Widescreen Support Mods` / `K2 Cutscenes Rescaled` |
| `nexus:1214` | Quicker TSLPatching (Nexus mod 1214) | Windows registry helper; the guide documents that it does not work on Mac or Linux, and nothing in the build consumes its output | K1 and K2: `Optional Install Assistant - Quicker TSLPatching` / `Quicker TSLPatching` |
| `nexus:1306` | KOTOR Remastered Cutscenes (Nexus mod 1306) | Tier 4 optional, loose movie `.bik` files, never cached; mutually exclusive with the installed Cutscenes Rescaled post (DeadlyStream 2380) | K1: `Optional Widescreen & Extended Widescreen Support Mods` / `Remastered Cutscenes` |

Exact flag strings the Rust install lanes pass (wired in `ci.yml`; `ports.py`
exempts, records and deselects the steps, see
[ci.md](ci.md#install-lanes-and-uncached-links)):

- K1 restore:
  `--allow-uncached-by-policy "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching" --allow-uncached-by-policy "Optional Widescreen & Extended Widescreen Support Mods::Remastered Cutscenes"`
- K2 restore:
  `--allow-uncached-by-policy "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching" --allow-uncached-by-policy "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled"`
  (step form only: the section also holds the pinned 1066 cutscene alternative,
  which restore must keep supplying.)

The shipped-policy tests assert that these flags skip exactly the steps above and
nothing else in either real guide.

## Reviewed selections

Some DeadlyStream posts list several files, and the guide either names one, leaves
the choice to the reader, or names none. `tools/modsync-ci/archive_selections.json`
records the reviewed choice so CI fetches exactly that and fails closed on
everything else. A post with several listed files and no entry is left unresolved
unless exactly one of the files is an archive. A listed name that no longer
matches exactly one row is also unresolved; re-review the entry.

Top-level `selections` is keyed by DeadlyStream file id (or file page URL).
`tool_selections` is keyed by `google-drive:<file id>` or `mega:<file id>` (or a
file URL). Every entry may carry a `reason`.

| Form | Meaning |
| --- | --- |
| `"<id>": "name.7z"` or `{"file": "name.7z"}` | Fetch the one listed file with this exact name |
| `{"file": "name.zip", "r": "19298"}` | Same, matched on the page's numeric row id `r`; needed when rows share a name |
| `{"files": ["a.7z", {"file": "b.zip", "r": "12"}]}` | Fetch every listed file; each is a name or `{file, r}`. `file` and `files` cannot both be set |
| `{"file": "tool.exe", "loose": true}` | The post serves one non-archive file; exactly one file, no `r`, a plain filesystem-safe name that is not `.zip`/`.7z`/`.rar` |
| `tool_selections` entry | Always the loose form: `{"file": "x.dlg", "loose": true}` |

Entries whose guide leaves the choice open say so in their `reason`
(`guide leaves choice to user; CI default = ...`) and follow one rule: base or
first-listed file, 1920x1080 at 30fps for resolution packs. Where the guide names
no file, the `reason` says that instead. These are CI defaults, not
recommendations; change the entry if a build needs a different variant.

### Direct links

A guide link that is a plain vendor page (not a `.zip`/`.7z`/`.rar` URL and not a
supported host) is dropped by `discover`, so it never reaches restore or
`--archive-map`. Top-level `direct_links` in `archive_selections.json` is the
reviewed exception, keyed by the exact guide link URL:

```json
"direct_links": {"<guide link>": {"url": "https://same-host/path/file.zip", "file": "file.zip",
                                  "sha256": "<64 hex>", "size": 1234, "reason": "..."}}
```

All five fields are required. `url` is an https archive URL without query on the
same host as the guide link; `file` is the filesystem-safe name restore writes;
`sha256` and `size` pin the content. Such a link is kept by `discover`, resolved
to `url` without fetching the landing page, and a download whose hash or size
differs from the entry is rejected (re-review the entry). A link not yet
in the cache is fetched from this URL by `restore` or `refresh`. The archive map
key is the guide link without its query, as for every link.

## Operator seed (Nexus Mods and GameFront)

Nexus Mods and GameFront cannot be fetched by the `mods` job (see
[Known gaps](#known-gaps)): the Nexus file pages sit behind a Cloudflare Turnstile
check and there is no `NEXUS_API_KEY`; GameFront answers with a Cloudflare
challenge (HTTP 403). The challenge is never bypassed. Instead an operator
downloads each file once in a normal browser session, stages it privately, and CI
copies it into the archive bucket only if it matches a reviewed pin.

### Pins

`tools/modsync-ci/seed_pins.json` is `{"schema": 1, "entries": {...}}`. Each entry
is keyed by the guide link id (the same id `cache.py discover` prints) and holds:

- `url`: the guide link. It must still be linked from `content/k1/full.md` or
  `content/k2/full.md` with exactly this URL, or the seed run exits 1 (pins and
  guides have drifted).
- `provider`: `nexus` or `gamefront`. Other providers are not accepted.
- `provenance`: must be `operator-local-download`.
- `objects`: one or more `{archive_name, sha256, size, extension}`. `extension` is
  `zip`, `7z`, `rar` or `file`; `file` is allowed only for a reviewed loose
  selection (see [Loose files](#loose-files)). `size` must be positive, a SHA-256
  may appear once per pin, and one `archive_name` may not stage different content
  under two pins. A post with several files is bound to the reviewed
  multi-file selection in `archive_selections.json`.

A pin that `cache.py` rejects (wrong provider or provenance, bad hash, missing
fields) is a hard error for the seed script, not a silent skip. The file holds
hashes, sizes and file names only, never credentials.

### What a seed run records

For each pin the script copies one staged file at a time to scratch space and
checks, in order: the file exists in staging, its size equals the pin, its SHA-256
equals the pin (a difference is `pin-mismatch` and fails the run), and the usual
[integrity check](#integrity-checks) passes. Only then does it upload
`objects/<sha256>` and write the manifest entry with `status: "pinned"` and
`evidence: "operator-seed"` (plus `metadata_unknown`). The local copy is deleted
after every pin. Nothing that fails a check is published.

Consequences of `pinned`:

- Restore treats a valid pinned entry as fresh: no 14-day expiry, no upstream
  request. The `mods` job does not refresh it either (`refresh_archives.py` skips
  pinned entries). An entry is `pinned` only if its manifest objects still match
  the pin and the reviewed selection; otherwise it is an ordinary stale entry.
- Pins are the only way a Nexus or GameFront link becomes restorable. A link with no
  pin stays `blocked-pending-user-action` or unresolved, and the lane that needs
  it stops at restore.
- A pin that is already in the bucket with the right object sizes is reported
  `already-seeded` and not rewritten. `--force` (CLI only; the workflow does not
  pass it) reseeds.

### Running it

1. Download each pinned file by hand. Upload it to a flat folder named
   `modsync-seed-staging` on a private rclone-reachable remote, one file per pin,
   named exactly `archive_name` (no subfolders). The workflow uses this default
   folder name; `seed_archives.py --staging-path` can change it for local runs only.
2. Store the staging `rclone.conf` (it must define a `[STAGING]` remote), base64
   encoded, as the secret `SEED_STAGING_RCLONE_CONF_B64`, available to the
   `mod-cache-seed` environment. The `seed` job runs in that environment and also
   needs the write pair (`MOD_CACHE_S3_ACCESS_KEY_ID`,
   `MOD_CACHE_S3_SECRET_ACCESS_KEY`), `MOD_CACHE_S3_ENDPOINT` and the
   `MOD_CACHE_BUCKET` variable. After a real seed it re-runs with the read pair
   (`--verify`) to prove every pinned id restores, as an install job would.
3. Dry run first. It downloads and fully verifies every staged file against its pin
   and writes nothing to the cache:

   ```sh
   gh workflow run ci.yml -R kolto-labs/mod-builds --ref <branch> -f seed_archives=true -f seed_dry_run=true
   ```

4. Then seed for real:

   ```sh
   gh workflow run ci.yml -R kolto-labs/mod-builds --ref <branch> -f seed_archives=true -f seed_dry_run=false
   ```

`<branch>` must contain the workflow with these inputs. With `seed_archives=true`
every other job is skipped, and the `seed` job shares the `mod-cache-writer`
concurrency group with the `mods` job so the two never write the manifest at once.
The run prints only sanitized totals and per-pin outcomes (`verified`, `seeded`,
`already-seeded`, `restores`, or `FAILED: <reason>`) to the log and the step
summary: no URLs, file names, remote paths or secrets. The script's own modes are
`--dry-run` and `--verify` (read-only); exit 1 is any pin or infrastructure
failure, exit 2 a usage or configuration error.

### Trust caveat

The staging remote is an account-wide OneDrive token, not a credential scoped to
the staging folder: the secret grants access to the whole account. The script
masks credential-looking values in the decoded config and keeps it in a private
temporary file, but that does not narrow what the token can reach. Use it only
for the seeding window. When seeding is done, delete the staging folder, delete
the `SEED_STAGING_RCLONE_CONF_B64` secret and revoke the token, so no standing
access to the account remains in CI.

### Never cached: the Bink-video post

The K2 cutscene-video post (DeadlyStream file 2503, about 25.7 GB of `.bik` video)
is deliberately not seeded and not in `seed_pins.json`. It is listed under
`uncached` (see [Never cached by policy](#never-cached-by-policy)): `.bik` is never
cached, by `game_cache.py` or by this archive cache. `cache.py restore` reports it
as `uncached-by-policy` and exits 1 for the K2 guide unless the lane passes
`--allow-uncached-by-policy "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled"`.
The Rust install lanes pass that step form (see
[ci.md](ci.md#install-lanes-and-uncached-links)); the section form is wired
nowhere.

## Known gaps

These links do not get cached today, so a guide that contains any of them cannot
pass restore. They are blockers outside this repository's code, and none is worked
around.

- **Nexus Mods:** 35 distinct links across the two guides (`cache.py discover`; 37 with the 2 GameFront links). There is no
  `NEXUS_API_KEY`, and the file pages sit behind a Cloudflare Turnstile check. The
  client reports `blocked-pending-user-action` and never bypasses the check. The
  refresh summary classifies the missing key as `credentials-required`; only a real
  provider challenge page is classified `captcha-user-action`.
- **GameFront:** 2 links. The client has no adapter and does not request them; a
  manual request returns HTTP 403 with a Cloudflare challenge.

Links with a reviewed pin are the exception: once seeded they restore from the
bucket, which needs the bucket credentials (see
[Operator seed](#operator-seed-nexus-mods-and-gamefront)). Every other
Nexus or GameFront link above stays blocked.
- **DeadlyStream post 2503** (K2 cutscene videos): the entry pins the main file and
  its `_mods` file, 1920x1080 at 30fps. The two together are about 25.7 GB of Bink
  video. It is listed under `uncached` (see
  [Never cached by policy](#never-cached-by-policy)), so restore reports
  `uncached-by-policy` and fails for the K2 guide unless the lane passes
  `--allow-uncached-by-policy "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled"`.
  The only step that references the post is `K2 Cutscenes Rescaled` in that
  section. The CI game baseline has no movies, no other guide step reads them, and
  the guide says to use only one of the two K2 cutscene mods (the other, Nexus
  1066, is pinned and still restored). The Rust install lanes pass the step form
  and `ports.py` deselects the component (see
  [ci.md](ci.md#install-lanes-and-uncached-links)); the other lanes still stop
  at restore.
