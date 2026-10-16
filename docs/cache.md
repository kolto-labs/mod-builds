# Caches

CI keeps two caches. Both only make runs faster; the guides in
`content/k1/full.md` and `content/k2/full.md` decide what gets installed. A cache
never papers over a failure: a guide link that is not cached stops the lane that
needs it.

| Cache | Holds | Lives in | Written by |
| --- | --- | --- | --- |
| Game | One encrypted, movie-free copy of K1 or K2 | Actions cache, key `game-<game>-v1` | `game` job |
| Archives | Mod downloads named by the guides | S3-compatible bucket (Cloudflare R2 in CI), prefix `modsync-archives` | `mods` job |

The game cache is described in [steam.md](steam.md). The archives add up to tens
of GB, too large for the 10 GB Actions cache, so they live in the bucket.

`.bik` movies are never cached. `game_cache.py pack` deletes them before sealing,
and `unpack` fails if the restored tree does not match the recorded inventory. The
one mod download that is mostly video is also never cached (see
[Never cached by policy](#never-cached-by-policy)).

## Backend and names

The archive bucket is reached through `rclone`. Each job that needs it defines an
rclone remote named `MODCACHE` (type `s3`, provider `Cloudflare`) from the values
below. Write and read credentials are separate pairs. Secrets and variables only;
never put values in the repository.

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

`manifest.json` plus `objects/<sha256>`. Manifest entries are keyed by the SHA-256
of the guide URL and record the object hash(es), size, type, upstream validators,
the last attempt and the freshness evidence. Objects are copied with
`--immutable`, and nothing is pruned automatically. The `mods` job uses the
`mod-cache-writer` concurrency group so only one refresh writes at a time.

- A download that is not an archive (see [Loose files](#loose-files)) is recorded
  with extension `file`.
- A post that yields several files stores several objects under the one URL id,
  each with its listed file name (and row id, if pinned).

## Refreshing: the `mods` job

Runs on every trigger except fork and Dependabot pull requests, which get no
secrets. It is `continue-on-error`, so a failure never blocks an install. It runs
`tools/modsync-ci/refresh_archives.py --max-items 500`:

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
  --cache-dir <dir> --destination <archives> --remote MODCACHE:<bucket>/modsync-archives
```

Restore is fail-closed. Install and manual jobs never add a link to the cache; only
the `mods` job does. For every link in the guide, restore:

- fails that link (`cache missing; archive refresh job must populate it`) if the
  manifest has no objects for it. It does not go and fetch a link the cache has
  never held;
- otherwise checks the hashes, sizes and archive integrity of the stored objects
  and copies each into a lane-local directory under its original file name. A
  duplicate file name, a hash mismatch, or a loose file that is not a reviewed
  selection fails that link.

If a stored entry is stale (older than 14 days, an object missing or corrupt, or
the reviewed selection changed), restore re-fetches that one already-known link
from its source and verifies it, then tries to push the repair back. These jobs
hold only the read pair, so that push is expected to fail. The workflow treats a
non-zero first attempt as a warning and runs restore again against the local,
already verified cache; the second attempt decides whether the job continues.

A link listed under `uncached` is not looked up in the manifest at all. Restore
reports it with `status: "uncached-by-policy"` and a reason that starts with
`intentionally not cached by policy`, so it is distinguishable from `unresolved`
(a link the cache failed to supply), and it still counts against the exit code.

If any link remains unresolved or uncached by policy, the command exits 1 and the job stops at the
restore step, before any install work. A lane therefore cannot start with a
guide whose archives are not all present.

## Freshness and providers

- A conditional GET (ETag or Last-Modified) answered with 304 for a verified
  object counts as fresh. A full download must pass the archive check first. A
  failed refresh marks the entry stale and keeps its bytes. A download without
  upstream validators is accepted as verified, and is re-downloaded after 14 days.
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
- `cache.py import-existing` copies archives you already have locally into the
  cache as unconfirmed entries (dry run unless `--apply`). Restore ignores them
  until a refresh confirms them.

`cache.py` takes `discover`, `refresh`, `restore` and `import-existing`, plus
`--guide` (repeatable), `--cache-dir`, `--remote` and `--report`. See `--help`.

## Integrity checks

- ZIP: CRC test of every member.
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
(or file page URL); each entry needs a non-empty `reason`. A listed link is never
fetched, never uploaded and never restored:

- `refresh_archives.py` removes it from the work list before any network or bucket
  access, deletes its manifest entry and `refresh_attempts` record, and counts it
  as `uncached_by_policy` in the summary. Objects an earlier run already uploaded
  stay in the bucket as unreferenced orphans; purge them by hand.
- `cache.py refresh` reports `uncached-by-policy` (not a failure), `import-existing`
  never copies it, and `refresh_one` raises before opening a connection.
- `cache.py restore` reports `uncached-by-policy` and exits 1 (fail-closed, see
  above).

Today the list holds one post, K2 file 2503: Bink video, about 25.7 GB for the
pinned main and `_mods` files. Its `selections` entry stays so the exact files are
on record if the policy is ever lifted.

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

## Known gaps

These links do not get cached today, so a guide that contains any of them cannot
pass restore. They are blockers outside this repository's code, and none is worked
around.

- **Nexus Mods:** 37 links across the two guides (`cache.py discover`). There is no
  `NEXUS_API_KEY`, and the file pages sit behind a Cloudflare Turnstile check. The
  client reports `blocked-pending-user-action` and never bypasses the check.
- **GameFront:** 2 links. The client has no adapter and does not request them; a
  manual request returns HTTP 403 with a Cloudflare challenge.
- **DeadlyStream post 2503** (K2 cutscene videos): the entry pins the main file and
  its `_mods` file, 1920x1080 at 30fps. The two together are about 25.7 GB of Bink
  video. It is listed under `uncached` (see
  [Never cached by policy](#never-cached-by-policy)), so restore reports
  `uncached-by-policy` and fails for the K2 guide. The CI game baseline has no
  movies, and no other guide step reads them; the step has no archive-free
  substitute in the lanes yet.
