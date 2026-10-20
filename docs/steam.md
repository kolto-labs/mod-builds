# Steam and the game cache

CI never ships game files. The `game` job in `.github/workflows/ci.yml` gets each
game once, packs it into an encrypted cache, and every later job reads that copy.

| Game | Steam app |
| --- | --- |
| `k1` | 32370 |
| `k2` | 208580 |

## The `game` job

Runs for `k1` and `k2`, one at a time (`max-parallel: 1`), because there is one
Steam account. Fork and Dependabot pull requests get no secrets and skip it.

1. Look up the Actions cache key `game-<game>-v1`. An older cache entry for the
   same game is accepted as a fallback. A hit does not contact Steam.
2. Look up the backup artifact (see [Game backup artifact](#game-backup-artifact)).
   On a cache miss with a live artifact, rebuild the sealed game from it and
   continue at step 7; Steam is not contacted.
3. On a miss for both, restore the game from the encrypted seed when its secrets exist
   (see [Game seed](#game-seed)). A usable seed skips SteamCMD and continues at step 4. A
   missing or failed seed is a `::warning::` and the job falls through to SteamCMD.
4. Without a seed result, run SteamCMD, forcing the Windows build, and stop at the first
   attempt that leaves `chitin.key` in the install directory:
   1. With `STEAM_CONFIG_VDF` set, sign in with that saved login and the username
      only.
   2. Otherwise, or if that did not produce the game, sign in with
      `STEAM_USERNAME` and `STEAM_PASSWORD`. Steam sends an approval request to
      the Steam mobile app; if it is not approved in time, that attempt fails (K2
      approvals have timed out this way).
   3. If that also failed, try each code in `STEAM_GUARD_CODES` in turn (see
      [Backup-code fallback](#backup-code-fallback)).
5. `game_cache.py inventory --require-modules` (the `Seal game` step) prints the restored tree (see
   [Game inventory](#game-inventory)) and fails the job when `modules/` holds no
   `.rim` file. A real Steam KOTOR install always ships `modules/*.rim`, so
   an empty `modules/` means a partial or wrong-depot download.
6. `tools/modsync-ci/game_cache.py pack` repeats that check, drops every `*.bik`
   movie, checks for `chitin.key`, `dialog.tlk`, and the game executable,
   records a SHA-256 inventory, and encrypts the tree with `CACHE_ENCRYPTION_KEY`.
7. Save the result under `game-<game>-v1` and, unless it came from the artifact
   or the cache already had a fresh backup, upload the backup artifact.

The job fails if the game is still missing after these steps (no cache, no live
artifact, no seed, SteamCMD gave nothing) or has no module `.rim` files. Nothing partial is saved. Each SteamCMD attempt that exits nonzero
logs a `::warning::` with its status; the attempt loop still stops at the first
one that leaves `chitin.key`, so a partial download that has `chitin.key` is
caught by the `.rim` check, not retried.

The check looks for `modules` case-insensitively. The K2 lane's runner log names
`.../game/Modules/003ebo.mod` (see [ports.md](ports.md)), and the Linux runner is
case-sensitive, so the earlier `modules/` check (removed in e287887, "Accept valid
Steam KOTOR installs without modules"; the commit gives no other evidence) most
likely failed on a directory spelled `Modules/`. Do not restore a check that
spells the directory in one case; the inventory prints the real spelling.

The restore-key fallback `steam-sealed-v3-excludingmovies-<game>-Linux-` is accepted
by the `game` job's lookup, so a hit on a legacy entry ends the job without
contacting Steam and without running the `.rim` check. If an install lane's
inventory shows a legacy entry with no `.rim` files, delete that cache entry and
re-run `game`.

## Game seed

A last-resort source for a game that the Actions cache and the backup artifact both lack, used
before SteamCMD so a K1 rebuild needs no Steam Guard approval. The seed is the game directory as
`tar | zstd | openssl enc -aes-256-cbc -pbkdf2 -iter 200000`, hosted at a private URL. Build one with:

```
tar -c -C <game dir> . | zstd -T0 | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -pass env:KEY -out seed.bin
```

Per game (`<GAME>` is `K1` or `K2`), repository secrets:

| Secret | Purpose |
| --- | --- |
| `GAME_SEED_URL_<GAME>` | `https://` URL of the encrypted seed |
| `GAME_SEED_KEY_<GAME>` | Passphrase for `openssl` |
| `GAME_SEED_SHA256_<GAME>` | Optional SHA-256 of the file as downloaded |

`tools/modsync-ci/game_seed.py restore` does the work. It masks the URL (`::add-mask::`), passes
it to `curl` on stdin so it is not in the process list, and downloads with `curl -fL` (resumable,
retried, https only) into a temporary file. With the SHA-256 secret set, a mismatch fails the job
with an `::error::`; it never falls through to another source. It installs `zstd` when the runner
lacks it, then streams `openssl enc -d ... -pass env:GAME_SEED_KEY_<GAME> | zstd -d | tar -x -C
$RUNNER_TEMP/game`, so no decrypted copy is written apart from the extracted tree, and deletes the
download. Output `restored=true` is set only when `chitin.key` exists afterwards. Any other
problem (secrets unset, download or decrypt failure, no `chitin.key`) is a `::warning::`, the
partial tree is removed and SteamCMD runs next. The restored tree then takes the same path as a
SteamCMD result: inventory with `--require-modules`, `pack`, cache save and backup artifact upload.

## Game backup artifact

The Actions cache can be evicted and K1 cannot be re-fetched without a human (SteamCMD
waits for a Steam Guard confirmation in the mobile app), so `game` also keeps the sealed
game as the Actions artifact `game-<game>-sealed`. The repository is public and anyone
can download artifacts, so only encrypted data is uploaded: the `baseline.seal` that
`game_cache.py pack` writes (AES-256-CBC with an HMAC, key `CACHE_ENCRYPTION_KEY`) is cut by
`tools/modsync-ci/game_backup.py` into parts of at most 2000 MiB, each sealed again with the
artstore part envelope (same key), next to a `manifest.json` that holds the SHA-256 and
size of the seal and the part list. The movies are not in it (`pack` drops every `*.bik`),
and nothing is uploaded without the key.

Per run, before the cache is used:

- `status` finds the newest unexpired artifact with that exact name in this repository
  (any run or branch; the job has `actions: read`).
- Cache hit, and the artifact is missing or expires within 14 days: the job restores
  the cache, stages the parts and uploads them (`retention-days: 90`, stored without
  recompression). A fresh artifact costs no download. This is what creates the backup
  from the cache that already exists.
- Cache miss with a live artifact: `fetch` downloads `manifest.json` and the parts,
  checks every part's HMAC and digest, and the SHA-256 and size of the joined seal, writes
  `baseline.seal` and the job saves it to the Actions cache so the later jobs take the
  fast path. Steam is not contacted. The install lanes still verify the SHA-256 inventory
  when they unpack it.
- Cache miss, no usable artifact: SteamCMD as before, then pack, save the cache and upload
  the artifact.

An artifact lookup, download, staging or upload failure only logs a `::warning::` (a failed
restore falls through to SteamCMD). The job fails only when neither the cache, an artifact
nor Steam provides the game. A key that differs from the one that sealed the artifact makes
every part fail authentication, so after rotating `CACHE_ENCRYPTION_KEY` delete the old
`game-*-sealed` artifacts and let `game` rebuild them.

## Game inventory

`game_cache.py inventory --game <k1|k2> --game-dir <dir> [--label <text>]
[--require-modules] [--quiet]` is read-only. It prints, flushed, and appends the
same text to `$GITHUB_STEP_SUMMARY`:

- top-level entry names, and the actual spelling of the `modules`, `override` and
  `data` directories (flagged when `modules` is not lowercase);
- `modules/`: counts and total bytes of `*.rim` (without `_s`), `*_s.rim`,
  `*.mod`, `*_dlg.erf`, and all files;
- `override/`: recursive file count and bytes;
- `data/*.bif`: count and bytes;
- sizes of `chitin.key` and `dialog.tlk`;
- `case normalization:` whether [Case normalization](#case-normalization) has been
  applied (every canonical top-level directory and every `modules/`/`override/`
  file name is lowercase), or how many names are still not lowercase, or `BLOCKED`
  with the first problem (collision or symlink). The `game` job's inventory always
  says `NOT applied` for a Steam K2 tree: the packed cache keeps Steam's spelling;
- the `steamapps/appmanifest_*.acf` state (`StateFlags`, `buildid`) when it
  still exists, which is only right after SteamCMD.

It runs in the `game` job right after SteamCMD or the seed (label `after SteamCMD` or `after seed`), and in
every `install` lane (label `<implementation> <mode>`) and the `manual` lane
(label `manual`) right after `Normalize game case`, so a failure such as a TS
`GFFList ... not found in modules/<name>.mod` can be read against what the cached
game really holds. The output is never an input to any cache key.

Install and manual jobs restore the same key (a miss fails the job) and run
`game_cache.py unpack`, which decrypts the cache and refuses to continue unless
every file matches the inventory. The result has no movies; it is a baseline for
mod installs, not a playable copy.

GitHub evicts cache entries that go unread for seven days or when the repository
exceeds its cache quota. The workflow runs on a schedule (`23 4 * * 1,4`, Monday
and Thursday 04:23 UTC); each scheduled run looks up both game caches in `game`
and restores them in the `install` and `manual` jobs, so entries are read
at least twice a week. That keeps them from the seven-day idle eviction; it does
not protect against quota eviction. Eviction is not an error: the next run
rebuilds the cache from the `game-<game>-sealed` artifact when one is live. Without one it
uses the game seed when configured, and only then downloads the game again, which needs a Steam sign-in: an approval in the mobile
app, unless `STEAM_CONFIG_VDF` is set or an unused backup code is available.

## Case normalization

Evidence (run 39240372283, k2 lanes): the Steam K2 tree has `Modules/` (capital M)
with 82 `*.rim`, 82 `*_s.rim`, 82 `*_dlg.erf` and no `*.mod`, `override` with 60
files, and `data/` with 11 `*.bif`. On the case-sensitive Linux runner that broke
two things. The TypeScript CLI retry died with `Checkpoint baseline failed before
mutation: Checkpoint path has inconsistent case spelling`, and mod installers write
`modules/<name>.mod` (lowercase, as TSLPatcher does on case-insensitive Windows), so
Linux lookups missed `Modules/`.

`game_cache.py normalize-case --game-dir <dir> [--game <k1|k2>] [--label <text>]`
fixes this on Linux (elsewhere it prints a skip and exits 0):

- Renames a top-level directory that is a case-variant of `modules`, `override`,
  `lips`, `streamsounds`, `streammusic`, `streamwaves`, `streamvoice`,
  `texturepacks`, `data`, `movies` or `saves` to the lowercase name. Other
  directories (for example `SaveGames`) keep their name.
- Lowercases every file name inside `modules/` and `override/` (recursively; subdirectory
  names are left alone).
- Never renames the executable, `chitin.key`, `dialog.tlk` or `swkotor*.ini`.
- Plans everything first and renames nothing when a problem is found. A hard error
  names both entries when two names collide case-insensitively (`Modules` and
  `modules`, or `A.TXT` and `a.txt`), and for any symlink in those trees or at the
  game root, or a game directory that is itself a symlink. It never follows a
  symlink and every path it touches is checked to stay inside the game directory.
- Is idempotent: a second run renames nothing and says `already normalized`.
- Prints, flushed, the counts (top-level directories renamed, files lowercased of
  files scanned per tree) and at most 20 per-rename lines, then `... and N more`.
  The same text goes to `$GITHUB_STEP_SUMMARY`.

It is an install-time step only. The `install` lanes and the `manual` lane run it
after `Unpack game` has decrypted the cache and checked every file against the SHA-256
inventory, and before `Game inventory`; the `game` job never runs it, so the cache
is packed, and verified, with Steam's original spelling. Mod installers
therefore see a case-normalized tree, the same view a case-insensitive Windows
install gives them (`Modules/` and `modules/` are one directory there).

## Secrets

Names only; set them as repository Actions secrets and never print or commit them.

| Name | Used for |
| --- | --- |
| `STEAM_USERNAME` | Steam account that owns both games |
| `STEAM_PASSWORD` | Password for that account; used when `STEAM_CONFIG_VDF` is unset or its login fails, and with each backup code |
| `STEAM_CONFIG_VDF` | Optional. Base64 of an authenticated SteamCMD `config.vdf` |
| `STEAM_GUARD_CODES` | Optional. Whitespace-separated Steam Guard backup codes; each works once |
| `CACHE_ENCRYPTION_KEY` | Encrypts and decrypts the game cache and the backup artifact |

## STEAM_CONFIG_VDF: sign in once, locally

`STEAM_CONFIG_VDF` carries a SteamCMD login you already approved. When it is set,
the `game` job writes it to `~/Steam/config/config.vdf` on the runner and runs
`+login <STEAM_USERNAME>` with no password and no prompt, so CI never needs a
Steam Guard approval.

Do this once, on your own machine:

```sh
steamcmd +login <steam_username> +quit
```

Enter the password and approve the sign-in in the Steam mobile app when asked.
SteamCMD saves the login in `config/config.vdf` under its Steam directory,
`~/Steam/config/config.vdf` for the `steamcmd` wrapper. If you also run the desktop
Steam client, check which file was just written by its modification time
(`ls -l`). `~/.local/share/Steam/config/config.vdf` belongs to the desktop client;
don't use it unless that is the file SteamCMD just updated.

Then send it straight to GitHub without displaying it:

```sh
base64 -w0 ~/Steam/config/config.vdf | gh secret set STEAM_CONFIG_VDF -R kolto-labs/mod-builds
```

The file is a login credential for your account. Keep it out of the repository,
shell history, chat and logs. Base64 is only an encoding. If CI starts failing to
sign in, the saved login is no longer accepted: repeat these steps and set the
secret again. GitHub limits a secret to 48 KB.

## Language

Install in English and pick English installer and download options; some guide
components have no other language. For K2, use the current default Steam version;
the full guide covers its Aspyr-specific steps.

## Backup-code fallback

If the saved config (`STEAM_CONFIG_VDF`) and the password sign-in both fail, the `game` job tries each code in the optional `STEAM_GUARD_CODES` secret as `+login <user> <password> <code>`. It stops as soon as `chitin.key` appears.

Get the codes from Steam → Account details → Manage Steam Guard → Get backup codes. Set the secret from your own terminal so the value never shows up in a log:

```sh
gh secret set STEAM_GUARD_CODES -R kolto-labs/mod-builds    # paste the codes separated by spaces, then Ctrl-D

```

When you need to refresh them, generate a new set and update the secret.

---
