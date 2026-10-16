# Caches

CI keeps two caches. Both only make runs faster; the guides in
`content/k1/full.md` and `content/k2/full.md` decide what gets installed.

| Cache | Holds | Lives in | Written by |
| --- | --- | --- | --- |
| Game | One encrypted, movie-free copy of K1 or K2 | Actions cache, key `game-<game>-v1` | `game` job |
| Archives | Mod archives named by the guides | GitHub Actions cache first; optional Cloudflare R2 fallback | `install` and `manual` jobs; `mods` refreshes R2 |

The game cache is described in [steam.md](steam.md). Archive sets can exceed the
repository's Actions cache quota, so the Actions cache is best effort; R2 can
provide a fallback but is not required when a suitable Actions cache hit exists.

## Names

Secrets and variables only; never put values in the repository.

| Name | Kind | Used by |
| --- | --- | --- |
| `MOD_CACHE_S3_ACCESS_KEY_ID`, `MOD_CACHE_S3_SECRET_ACCESS_KEY` | secret | `mods` (read/write) |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID`, `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | secret | `install`, `manual` (read only) |
| `MOD_CACHE_S3_ENDPOINT` | secret | all of the above |
| `MOD_CACHE_BUCKET` | variable | all of the above |

The jobs pass these to `rclone` as a `MODCACHE` remote.

## Layout

`manifest.json` plus `objects/<sha256>`. Manifest entries are keyed by the SHA-256
of the guide URL and record the archive hash, size, type, upstream validators and
the last attempt. Objects are not overwritten or pruned automatically. The `mods`
job uses the `mod-cache-writer` concurrency group so only one refresh writes at a
time.

## Restoring: install and manual jobs

Each lane first restores the current week's per-game archive snapshot from GitHub
Actions cache. On a miss, it tries R2 when configured. A complete R2 restore is
saved to Actions cache by one CLI lane; incomplete restores are not cached. Cache
entries are keyed by game, guide contents and UTC week, so a later week causes a
fresh R2 lookup. GitHub may evict entries or decline a snapshot that exceeds its
storage limits; lanes then fall back to R2.

If neither source is available, the lane still runs with any archives it has, and
the installer fails when a required archive is missing. R2 is therefore optional
as a service, but a cache hit or another source of the required archive bytes is
still necessary for a complete install.

## Refreshing: the `mods` job

Skipped on pull requests. Marked `continue-on-error`, so a failure never blocks an
install. Runs `tools/modsync-ci/refresh_archives.py --max-items 20`, on every push
to `dev`, on the Monday and Thursday schedule and on manual dispatch:

1. Pull `manifest.json` from R2.
2. Pick up to 20 due links from both full guides: never attempted first, then the
   oldest attempt. Stop early at the time limit or when under 3 GiB of disk is free.
3. For each link, fetch it with `cache.py`, verify the archive, upload the object
   and the manifest, then delete the local copy.

Unresolved links (login walls, dead links, CAPTCHAs, non-archives) are reported as
totals, not errors. The script fails only when the backend is unreachable, the
manifest is corrupt or an upload does not persist. Output never contains URLs,
remote paths or secrets.

To retry one link: `refresh_archives.py --max-items 1 --candidate-id <id>`, with the
ID from `cache.py discover`.

```sh
python tools/modsync-ci/cache.py restore --guide content/<game>/full.md \
  --cache-dir <dir> --destination <archives> --remote MODCACHE:<bucket>/modsync-archives
```

On an R2 fallback, restore pulls the manifest and objects, verifies their hashes
and copies each archive into a lane-local directory under its original file name.
It never contacts the mod hosts. An entry that is missing, corrupt, or last
confirmed more than 14 days ago is reported unresolved and the command exits 1.
The workflow continues with what was restored; the install step that needs a
missing archive then fails.

## Freshness and providers

- A conditional GET (ETag or Last-Modified) answered with 304 for a verified
  object counts as fresh. A full download must pass the archive check first. A
  failed refresh marks the entry stale and keeps its bytes.
- Archive checks: ZIP CRC, and `7z t` for RAR and 7z. HTML pages and unknown formats
  are rejected whatever the file name says.
- Direct `.zip`/`.7z`/`.rar` links and DeadlyStream downloads work. Nexus Mods,
  MEGA, Google Drive and GameFront need a browser or account and stay unresolved.
  The client does not run JavaScript or touch CAPTCHAs.
- `cache.py import-existing` copies archives you already have locally into the
  cache as unconfirmed entries (dry run unless `--apply`). Restore ignores them
  until a refresh confirms them.

`cache.py` takes `discover`, `refresh`, `restore` and `import-existing`, plus
`--guide` (repeatable), `--cache-dir`, `--remote` and `--report`. See `--help`.
