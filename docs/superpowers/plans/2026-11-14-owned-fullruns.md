# Repository-owned full-run CI Implementation Plan

> **For agentic workers:** Implement in the current authorized session; use independent review for consequential workflow, credential and coverage boundaries.

**Goal:** Commit a complete locally owned CI variant to each requested repository branch.
**Architecture:** Local reusable game/archive/seed/install/P2P/tooling stages compose through a small coordinator. The owner role binds its own subject SHA and an exact counterpart pin; code/tools are not shared across repositories at runtime.
**Tech Stack:** GitHub Actions, existing Python integration tools, production-only Rust lib/bin commands.
**Spec:** User requirements and docs/ci-fullruns.md.

## Global constraints

- Each repository owns all its workflow/tool variants; no cross-repository workflow calls.
- Preserve existing source branch work and target metadata/release/paused launcher.
- No Rust test/custom Rust harness compilation or execution; no --all-targets.
- No destructive cache cleanup, repository settings/secret changes, releases or default-branch merges.

## Review focus

- Wrong subject/counterpart SHA under different owner roles.
- Missing/skipped/failed producer states and standalone install.
- Private credentials inherited by child applications.
- Corrupt or wrong-owner baseline recovery and premature publication.
- Partial/GUI-subset output reported as complete.

## Tasks

- [x] Audit the named source branch and the target default branch; pin both bases.
- [x] Add local tooling, fixtures, role contracts and typed coverage aggregation.
- [x] Split the local stage workflows and preserve existing native/production checks.
- [x] Test request, graph, failure, credential, selection and fixture boundaries.
- [ ] Complete isolated code review and resolve material findings.
- [ ] Commit both variants with expected-head leases and verify resulting branch files.
- [ ] Inspect automatic credential-free CI; report actual runtime limits separately.
