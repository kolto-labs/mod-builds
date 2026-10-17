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
   same game is accepted as a fallback. A hit ends the job; Steam is not contacted.
2. On a miss, run SteamCMD, forcing the Windows build, and stop at the first
   attempt that leaves `chitin.key` in the install directory:
   1. With `STEAM_CONFIG_VDF` set, sign in with that saved login and the username
      only.
   2. Otherwise, or if that did not produce the game, sign in with
      `STEAM_USERNAME` and `STEAM_PASSWORD`. Steam sends an approval request to
      the Steam mobile app; if it is not approved in time, that attempt fails (K2
      approvals have timed out this way).
   3. If that also failed, try each code in `STEAM_GUARD_CODES` in turn (see
      [Backup-code fallback](#backup-code-fallback)).
3. `tools/modsync-ci/game_cache.py pack` drops every `*.bik` movie, checks for
   `chitin.key`, `dialog.tlk`, and the game executable, records a SHA-256
   inventory, and encrypts the tree with `CACHE_ENCRYPTION_KEY`.
4. Save the result under `game-<game>-v1`.

The job fails only if the game is still missing after these steps. Nothing partial
is saved.

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
downloads the game again, which needs a Steam sign-in: an approval in the mobile
app, unless `STEAM_CONFIG_VDF` is set or an unused backup code is available.

## Secrets

Names only; set them as repository Actions secrets and never print or commit them.

| Name | Used for |
| --- | --- |
| `STEAM_USERNAME` | Steam account that owns both games |
| `STEAM_PASSWORD` | Password for that account; used when `STEAM_CONFIG_VDF` is unset or its login fails, and with each backup code |
| `STEAM_CONFIG_VDF` | Optional. Base64 of an authenticated SteamCMD `config.vdf` |
| `STEAM_GUARD_CODES` | Optional. Whitespace-separated Steam Guard backup codes; each works once |
| `CACHE_ENCRYPTION_KEY` | Encrypts and decrypts the game cache |

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

If the saved config (`STEAM_CONFIG_VDF`) and the password sign-in both fail, the `game` job tries each code in the optional `STEAM_GUARD_CODES` secret as `+login <user> <password> <code>`. It stops as soon as `chitin.key` appears. Each code can only be used once, so a code is spent whether or not the rest of the run succeeds.

Get the codes from Steam → Account details → Manage Steam Guard → Get backup codes. Set the secret from your own terminal so the value never shows up in a log:

    gh secret set STEAM_GUARD_CODES -R kolto-labs/mod-builds   # paste the codes separated by spaces, then Ctrl-D

When the codes run out, generate a new set and update the secret.
