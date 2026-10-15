# Steam acquisition

Use Python 3.12+ and SteamCMD's Linux 32-bit libc/libstdc++ dependencies, or
Windows. The account must own the requested game. Both hosts explicitly acquire
Windows content: K1 app 32370 and K2 app 208580.

```sh
python3 tools/modsync-ci/steam.py acquire --game k1 \
  --destination /private/work/k1 --state-dir /private/steam-state \
  --report /private/reports/k1.json --timeout 3600
```

Supply `STEAM_USERNAME` and `STEAM_PASSWORD` through GitHub Actions secrets as
environment variables. Never interpolate them into shell commands. The username
is always required. Saved-state login initially omits the password, even when
one is supplied. A narrowly scoped password fallback for the diagnosed
`no_cached_credentials` failure is incoming; do not assume it is available in
the deployed acquisition path or that it has successfully acquired a game.
Other saved-state failures must not trigger a password retry. Optionally
supply `STEAM_CONFIG_VDF`, the base64-encoded SteamCMD `config/config.vdf` from
an authorized session. It is restored with mode 0600 below the private Steam
home. Base64 is transport encoding, not encryption. `STEAM_GUARD_CODE` can carry
a fresh five-character authorization code. Saved configuration may still require
Steam Guard authorization. The acquisition workflow also accepts
`STEAM_CONFIG_VDF_GZIP`: base64-encoded gzip-compressed `config.vdf`, decoded
before invoking the helper; it takes precedence over `STEAM_CONFIG_VDF`.
It also accepts a versioned bundle containing `config.vdf` and only allowlisted
`ssfn*` and `registry.vdf` candidates below the private Steam home. Legacy
compressed raw `config.vdf` remains supported. Capture and restore reject links,
unsafe paths, duplicate entries, and values exceeding the 48 KiB GitHub secret
limit; they fail instead of truncating. Compression and base64 do not encrypt
authentication state.

When acquisition is due, the workflow checks public egress through two IP
geolocation services and requires agreement on IP and US state. It publishes
the public IP, state, city, country, and source details in the
`steam-egress-location-<game>-<run>-<attempt>` artifact with one-day retention,
then waits 60 seconds before Steam authentication. Geolocation is approximate
and does not establish Steam's observed location. Authentication starts
automatically when the delay expires; Steam may separately request approval in
its mobile app. Saved-state portability across fresh runners remains unproven,
so bundle support is not evidence of a successful saved login.

Browser web-session cookies authenticate Steam websites, not the Steam client
protocol used by SteamCMD. A signed-in browser does not establish SteamCMD
authorization; use authorized SteamCMD saved state or client login instead.
See Valve's [SteamCMD login documentation](https://developer.valvesoftware.com/wiki/SteamCMD#SteamCMD_Login).

Credentials appear only in a temporary mode-0600 runscript, deleted after the
process exits. Process arguments contain only the executable and runscript path;
the child environment excludes credential variables. SteamCMD console output is
discarded by the standalone helper because arbitrary session identifiers cannot
reliably be redacted. The acquisition workflow captures it privately for diagnosis.
The private event log contains only the game identifier and verified/failed.
SteamCMD's own diagnostic and authentication files remain inside the private
state directory. Never upload plaintext Steam logs or authentication state.
On acquisition failure, the workflow can redact known secrets and seal diagnostic
logs using `DIAGNOSTICS_ENCRYPTION_KEY`, then upload only the encrypted
`steam-diagnostics-*.seal` artifact with **one-day retention**. Without that key,
no diagnostic artifact is uploaded. Redaction alone is insufficient protection;
the workflow removes local private logs and state during cleanup. This encrypted
failure-diagnostics path supersedes the former blanket no-log-upload rule.

Keep state on an access-controlled private runner disk (mode 0700 directories),
or persist it only through an encrypted private store. Never use public GitHub
Actions caches for authentication or configuration, including base64 secrets.
POSIX mode bits do not establish Windows ACL privacy: provision a Windows state
directory accessible only to the runner account. Run under a restrictive umask
on Linux. Do not share a state directory between untrusted users or jobs.

Acquisition is serialized per state directory and runs `app_update APP validate`
with the Windows platform forced even on Linux. A nonzero exit, timeout,
unfinished app manifest, missing/empty game executable, `chitin.key`, `dialog.tlk`,
or missing modules fails acquisition. The timeout applies to each SteamCMD
process; bootstrap HTTP requests have separate 60-second socket timeouts.
Timeout or interruption kills the owned process group. Reports contain safe
status and, on success, relative paths, sizes, and SHA-256 hashes of game files.
Reports and destinations must be new paths; setup errors may occur before a
report can be created. Failure returns a nonzero exit status.

The state directory retains a baseline and hash manifest. Existing baseline
contents must match that manifest before Steam validation; unexplained changes
fail closed. An interrupted initial acquisition has no accepted manifest and
requires a new private state directory. Inspect and retire the old directory
privately. The install stage must use the independent destination copy, never
the baseline. The copy excludes Steam app metadata and is hash-verified before
publication. Do not hardlink a baseline into a mod install tree. Baseline hashes
prove local copy integrity, not an independent Valve content signature.

## Encrypted game cache scope

The game cache must contain **no `*.bik` files**, matched case-insensitively
at every directory depth (including names such as `nocache.bik` or `INTRO.BIK`).
This exclusion applies only after acquisition to the generated cache copy. It
does not make Steam skip movie files during the initial download. The canonical
Steam baseline and its full acquisition receipt remain unaffected. This saves
cache space; it does not reduce the initial Steam download.

Before filtering, the acquisition workflow verifies the complete cache staging
copy against the source inventory. It preserves the original receipt bytes as
`source-baseline-<game>.json`, removes BIK files from that staging copy, and
regenerates `baseline-<game>.json` as the filtered inventory. Its
`cache-scope.json` records six required fields:

| Field | Meaning |
| --- | --- |
| `schema` | Integer `1`. |
| `game` | `k1` or `k2`, matching the requested game. |
| `excluded_pattern` | Literal `*.bik`, applied case-insensitively. |
| `excluded_count` | Nonnegative integer count of omitted files. |
| `excluded_bytes` | Nonnegative integer sum of omitted file sizes. |
| `source_receipt_sha256` | SHA-256 of the preserved, unmodified source receipt bytes. |

Acquisition, manual-install, and automated-install workflows use the
`steam-sealed-v3-excludingmovies-` cache namespace. Both install consumers
require the six scope fields and validate their types and values, verify the
preserved source receipt SHA-256, and derive the expected filtered inventory
by excluding case-insensitive `.bik` paths from that source receipt. They check
the exclusion count and byte total, reject remaining BIK paths, and verify
both the restored tree and independent install copy against the filtered
inventory. Missing or inconsistent evidence fails closed. This verifies a
movie-excluded cache copy, not a complete playable game installation.

`seal.py` streams the staged tree through encryption instead of first writing
a plaintext archive. The workflow saves only the ciphertext copy
(`sealed/*.seal`) to the cache; authentication state is not part of that tree.

Before installing mods, select English for the complete K1 or K2 full-guide
build and use matching English installer/download choices. The guides contain
components without non-English support; language choices must be settled before
modification. Current acquisition code does **not** enforce or verify language.
For K2, the current default Steam Aspyr version is supported by the full guide;
switching to a legacy branch is not required. Apply the guide's Aspyr-specific
steps in order. See [K1 full guide](../content/k1/full.md) and
[K2 full guide](../content/k2/full.md).

Live evidence remains a failed Steam attempt with exit code **5**, now diagnosed
as `no_cached_credentials`. The scoped password fallback is incoming;
successful acquisition has not yet been demonstrated. Neither the diagnosis
nor the cache verification contract establishes authentication success or a
verified acquired game baseline.

The standalone helper does not configure workflow caches or artifact uploads.
Workflow owners must enforce private storage and encrypted-only diagnostics.
