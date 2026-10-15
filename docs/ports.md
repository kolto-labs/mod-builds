# Private port CI adapter

Run with Python 3.11 or newer from any directory:

```bash
python3 tools/modsync-ci/ports.py \
  --implementation rust --mode cli \
  --source /private/ModSync.rs \
  --guide /guides/content/k1/full.md \
  --game-dir /private/lane/game \
  --archives /private/lane/archives \
  --work-dir /private/lane/work \
  --report /reports/rust-cli.json
```

Implementations are `rust` and `typescript`; modes are `cli` and `gui`.
Use separate game, archive and work directories for every concurrent lane.
The caller supplies a genuine clean game installation and verified archives.
The adapter does not acquire a game, download mods, or manufacture fixtures.
Archive directories must be lane-local writable copies: native installers can
extract or otherwise mutate their source directory. Never pass the shared cache.
Work must not overlap source, guide, game or archive roots. Reports must be new
files outside all input roots. No cache or Steam adapter is imported.

## Commands inspected in the current ports

Rust uses `cargo build --locked --bin modsync` with a unique `CARGO_TARGET_DIR`,
then that freshly built `target/debug/modsync`:

```text
convert --input full.md --output converted.toml --format toml --parse-directions
validate --input converted.toml --game-dir GAME --source-dir ARCHIVES
install --input converted.toml --game-dir GAME --source-dir ARCHIVES --non-interactive --phase all
```

Conversion always starts from the supplied Markdown, with a new output path;
existing TOMLs and merge inputs are never used. Before validation the adapter
requires a nonempty Mod List, equal Markdown section/component counts and
instructions on every component. These are conservative coverage checks, not
semantic proof. Rust's native validation/install gates remain authoritative for
draft review, missing sources, choices and unsupported operations. No review
receipt is created or modified, and no skip, best-effort or auto-confirm flag is
passed. Fresh prose drafts normally block until a human review workflow exists;
this interface deliberately provides no approval override. It never backfills
failed steps. On installation failure preserve the lane and recover using the
port's verified snapshot protocol before any retry.

TypeScript uses `pnpm build`, then `node packages/cli/dist/main.js import-md
full.md --output converted.toml`. Dependencies must already be installed using
the source repository's pinned pnpm setup. Build output is private. The inspected
CLI's `install <file> --mods ARCHIVES --kotor GAME --all` implicitly accepts
prompt defaults when stdin is not a TTY; its phases are `base` and `widescreen`,
not a single full-guide operation. The current adapter therefore performs real
build/conversion and reports
`typescript_unattended_review_and_full_phase_driver_unavailable`, without
starting an unsafe or incomplete install. Synthetic simulation scripts are
never used. Updating the port requires rechecking these contracts.

Both GUI modes fail explicitly with `real_full_guide_gui_driver_unavailable`.
Launching a window or running a synthetic clickthrough is not full-guide GUI
evidence. A genuine driver with review and complete-step accounting is required
before these modes can be enabled.

## Logs, results and limits

Each invocation creates a random `ports-*` directory beneath work, mode 0700.
Raw build/conversion/validation/install output goes only to stage logs there;
TOML and Rust build artifacts stay there too. Files are created under umask 077.
Treat the entire work directory and native game/archive outputs as private;
never upload them as public CI artifacts. Only publish the sanitized JSON report.
It contains fixed reason codes, implementation, mode, stage exit codes, guide
and converted-document SHA256 hashes, and an optional component count. It excludes
source paths, commands, mod names and raw exception or subprocess text.

Exit 0 means the native install command succeeded, not manual-oracle parity,
live-game acceptance or proof of every prose requirement. Exit 1 is a blocked or
failed lane; exit 2 is invocation/report failure. Each subprocess has a two-hour
deadline and closed stdin. Interrupted/timed-out process groups are terminated;
installation side effects are retained for recovery, never silently retried.
No live full-guide installation was used to validate this adapter's implementation.
