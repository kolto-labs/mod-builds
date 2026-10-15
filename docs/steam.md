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
is always required. A password is optional when saved configuration is supplied
or already exists in the private state; when present it is used as the login
fallback alongside that configuration. Optionally
supply `STEAM_CONFIG_VDF`, the base64-encoded SteamCMD `config/config.vdf` from
an authorized session. It is restored with mode 0600 below the private Steam
home. Base64 is transport encoding, not encryption. `STEAM_GUARD_CODE` can carry
a fresh five-character authorization code. Saved configuration may still require
Steam Guard authorization; browser cookies are not SteamCMD authentication.

Credentials appear only in a temporary mode-0600 runscript, deleted after the
process exits. Process arguments contain only the executable and runscript path;
the child environment excludes credential variables. SteamCMD console output is
discarded because arbitrary session identifiers cannot reliably be redacted.
The private event log contains only the game identifier and verified/failed.
SteamCMD's own diagnostic and authentication files remain inside the private
state directory. Do not upload any Steam logs or state as workflow artifacts.

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

This helper does not configure workflow caches or artifact uploads. Workflow
owners must enforce these private storage and no-log-upload requirements.
