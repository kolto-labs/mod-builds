# Manual Reference CI — Execution Contract

> **Status: infrastructure in development.** The `manual.py` executor and its
> reviewed-plan infrastructure are being written. This is an execution contract,
> not evidence of a successful live manual CI run. Commands and serialized schemas
> must be checked against the reviewed implementation before being advertised.

## Purpose and inputs

The manual lane follows the full K1 or K2 guide literally, using each mod's
original patcher where required, to produce a reference install for comparison
with the automated ModSync lane.

| Build | Installation authority |
|-------|------------------------|
| K1 | `content/k1/full.md` |
| K2 | `content/k2/full.md` |

Pin the guide repository revision and guide content hash at run start. Record
game, edition, language, platform, selected guide options, and archive identities.
Derive any entry counts or method totals from that pinned source. This document
asserts no current repository SHA, guide count, or installed binary digest.

## Reviewed execution plan

The intended `manual.py` flow executes an explicitly reviewed plan derived from
the pinned guide. Each operation must identify its guide section, inputs, target,
choices, original patcher where applicable, and expected verification. Unresolved
instructions block execution. Generated TOML or inferred operations do not replace
the guide as installation authority.

Bind review to the exact plan contents and pinned inputs. Changes to the guide,
plan, archives, original patcher, or baseline require renewed review before
dependent operations execute. These are requirements for the infrastructure being
written, not claims about its currently implemented interfaces or passing runs.

## Original patcher provenance

Use each mod's original installer from its verified release archive, or the
original patcher explicitly required by the pinned guide. Preserve the associated
installer data, working directory, arguments, and selected options.

Do not substitute ModSync's `vendor/bin` patchers, OdyPatcher, a newer generic
HoloPatcher, or another compatible implementation. Vendor binary hashes do not
establish the identity of a mod's original patcher. If an original installer cannot
run in the available environment, block that step until an appropriate environment
is available.

For each patcher invocation, record and verify:

- Source archive identity and content hash.
- Original installer path within that archive and actual executed binary hash.
- Patcher data identity, working directory, arguments, and option choices.
- Operating system, any compatibility layer or runtime, and resolved game target.

Record runtime identity separately from patcher identity. A single shared vendor
digest table cannot identify the original patchers of different mod releases.

## Official baseline

Start from a clean official installation of the selected game, edition, language,
and platform, including the applicable official updates. Record acquisition
provenance and a complete relative-path, size, and content-hash manifest before
modification. `chitin.key` must exist, but its presence or hash alone does not
establish cleanliness.

There is no universal directory-size threshold or single operating-system
baseline. Official installations can contain files in `Override/`; preserve
those files and account for them in the baseline manifest. Zero files in
`Override/` is not an acceptance requirement.

Verify cleanliness against official inputs and their inventory. Isolate the
target and check installer auto-detection, registry entries, and compatibility
layer paths so an original patcher cannot silently select another installation.
Keep baseline copies and snapshots outside installer discovery paths. Do not
require deletion of other installations as a substitute for target verification.

## Guide order and ledger

Execute every selected instruction in guide order, including manual file changes,
cleanup, and option choices. Never skip a failed step, continue past it, or append
its fix later. Loose files are last-writer-wins and patchers accumulate shared
state that later steps depend on.

The append-only ledger must retain:

- Run identity, pinned guide and reviewed-plan identities, and baseline manifest.
- Step identity, exact guide section, reviewed operation, and selected choices.
- Archive and original patcher provenance, execution environment, and target.
- Verified pre-step snapshot and before/after content manifests.
- Start and outcome events, exit status, logs, and verification results.
- Verdict and reason: in progress, passed, blocked, rolled back, or corrupt.

Append a start event before mutation and a separate outcome afterward; do not
rewrite earlier records. A crash leaves an incomplete step requiring recovery.
Guide-authorized exclusions must be explicit in the reviewed selection. Missing
archives are blockers, not exclusions; a reduced selection is not a completed
full build.

Accept a step only after checking its expected effects and original patcher logs.
A zero exit code alone is insufficient. Investigate errors, unexpected warnings,
and unexplained empty diffs. A guide-authorized deletion of an already absent
file can be a valid recorded no-op.

## Snapshots, rollback, and replay

Before every mutation, retain a restorable snapshot with a complete content
manifest. Choose copies or verified filesystem snapshot/reflink facilities based
on actual filesystem support and capacity. Halt if no valid rollback point can
be created.

Hardlinks alone do not isolate in-place writes. Dereferencing symbolic links does
not break hardlinks. Protect every file an installer may modify, rather than an
assumed list of mutable folders. Size and modification time alone cannot prove
unchanged contents.

On failure:

1. Halt at the failed step.
2. Restore its pre-step snapshot, including removal of newly added files.
3. Verify the entire restored tree against the pre-step content manifest.
4. Record corruption and abort if restoration does not match.
5. Fix the cause and review any changed plan or inputs.
6. Rerun the failed step, verify it, then continue in guide order.

If an installer writes outside the isolated target, halt and assess all affected
locations. Restoring the game directory alone does not establish recovery.

## Blockers

Halt on missing or invalid archives, unreviewed operations, original patcher
identity mismatches, unsupported execution environments, unverified baselines or
snapshots, patcher failures, unexplained effects, or writes to an unexpected
target. Preserve evidence and the last verified state.

Reuse existing verified archives and check archive integrity and contents.
Extension and size alone are insufficient. Record login walls, payment
requirements, rate limits, and CAPTCHAs as distinct blockers. Never solve or
interact with a CAPTCHA; record it as pending user action.

## Comparison and completion evidence

Compare runs with matching pinned guides, reviewed selections, and compatible
baseline inputs. Record platform or edition differences and establish their
effect before claiming parity.

Compare loose-file identities and content hashes. Compare containers by resource
identity and complete logical contents. TLK comparison must include flags, sound
metadata, and other meaningful fields as well as text and string indices. Accept
packing-only differences only after proving semantic equality. Verify executable
patches against the expected inputs and outputs for that edition and platform;
do not assume all patchers produce deterministic bytes.

Each finding must identify the resource, presence in each lane, content
difference, and responsible guide step where known. Retain raw comparison evidence
and the reason for any accepted normalization.

A reviewed plan, implemented executor, or green infrastructure check does not
prove a successful manual install. Live completion requires an actual end-to-end
run with all selected operations verified, original patcher provenance, complete
ledgers and manifests, and comparison receipts. Until those exist, report
implementation progress and outstanding blockers without claiming manual CI
success.
