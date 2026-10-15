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
is always required. Saved-state login omits the password, even when one is
supplied; it does not automatically fall back to password login on failure.
A password is used only when saved state is not used. Optionally
supply `STEAM_CONFIG_VDF`, the base64-encoded SteamCMD `config/config.vdf` from
an authorized session. It is restored with mode 0600 below the private Steam
home. Base64 is transport encoding, not encryption. `STEAM_GUARD_CODE` can carry
a fresh five-character authorization code. Saved configuration may still require
Steam Guard authorization. The acquisition workflow also accepts
`STEAM_CONFIG_VDF_GZIP`: base64-encoded gzip-compressed `config.vdf`, decoded
before invoking the helper; it takes precedence over `STEAM_CONFIG_VDF`.
Compression and base64 do not encrypt authentication state.

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

Before installing mods, select English for the complete K1 or K2 full-guide
build and use matching English installer/download choices. The guides contain
components without non-English support; language choices must be settled before
modification. Current acquisition code does **not** enforce or verify language.
For K2, the current default Steam Aspyr version is supported by the full guide;
switching to a legacy branch is not required. Apply the guide's Aspyr-specific
steps in order. See [K1 full guide](../content/k1/full.md) and
[K2 full guide](../content/k2/full.md).

Live evidence remains a failed Steam attempt with exit code **5**; successful
acquisition has not yet been demonstrated. That code alone does not establish
the cause, authentication success, or a verified game baseline.

The standalone helper does not configure workflow caches or artifact uploads.
Workflow owners must enforce private storage and encrypted-only diagnostics.
