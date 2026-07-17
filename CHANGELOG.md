# Changelog

All notable changes to GMDW are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] — 2026-07-17

Open-source publication release: adds the licensing and community-health surface a
public repository needs, plus documentation hygiene. No governance behavior changes;
the enforcement plane and the 51-test suite are untouched.

### Added
- Apache-2.0 `LICENSE` and `NOTICE` (Copyright 2026 Arun Verma).
- Community health files: `SECURITY.md` (private vulnerability / governance-bypass
  reporting policy) and `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), each with a
  concrete maintainer-contact route.
- `.github/`: CI workflow running the governance suite plus syntax/JSON checks on
  push and PRs across Python 3.9/3.11/3.12; issue templates; a governance-aware PR
  template.
- README: License and Contributing & security sections, plus an Anthropic
  non-affiliation / trademark note, and a note that `wf_20260714_001` is the one
  committed example run.

### Changed
- `CONTRIBUTING.md`: fixed a stale test count and removed a reference to
  already-deleted branches.
- Moved superseded point-in-time docs (`HANDOFF`, `LIVE_VALIDATION_READINESS`,
  `LIVE_VALIDATION_EXECUTION`, `RELEASE_CHECKLIST`) into `docs/history/` with a
  historical-document banner, and fixed the inbound references.
- Date-stamped the advisory `MODEL_RATES` snapshot in `gmdw/ledger.py` (budgets
  remain enforced in tokens; the dollar estimate is advisory only).

## [0.2.0] — 2026-07-16

First general-use release. Hardens the MVP's one fail-open risk (a session where the
governance hooks silently don't fire), moves artifact-acceptance authority into the
repository-scoped core, and adds human-attributed recovery commands. All governance
guarantees remain enforced by code; the test suite grew from 26 to 50.

### Added
- **Governance-hook liveness attestation** (ADR-001 Phase 1). The three hooks write a
  heartbeat on every firing; `wf init` refuses to start a workflow when the
  enforcement plane is not live in the session (self-attesting on the `init` call).
- **Mid-run liveness re-check** (ADR-001 Phase 2). Every `wf transition` (except
  `ESCALATED`/`FAILED`/`REJECTED`) re-verifies the hooks are firing — via a
  hook-sourced ledger event since the last transition or a fresh heartbeat — and
  refuses to advance otherwise.
- **`wf accept --by <human>`** — human recovery for a validly-produced but unaccepted
  artifact, through the *same* acceptance core the hook uses (only the state's exact
  next version; refuses missing/frozen).
- **`wf clear-invocation --by <human>`** — human recovery for an orphaned
  `open_invocation` that would otherwise deadlock every transition.
- **`wf doctor`** — a read-only, side-effect-free preflight that reports whether the
  governance hooks are live in the current session (exit 0 live / 1 not; `--json`).
- **`wf --version`** — prints the CLI / package version.
- **`docs/GETTING_STARTED.md`** — a user-facing walkthrough, including a "Setting up
  in VS Code / other IDEs" guide (project-root / workspace-trust requirements).
- **`gmdw/accept.py`** — the extracted, single-home artifact-acceptance pipeline,
  shared by the SubagentStop hook and `wf accept`.

### Changed
- The SubagentStop validator is now a thin session-plane adapter over `gmdw/accept.py`
  (behavior-preserving; pinned by the existing acceptance tests).
- Bypass reconciliation: `GMDW_ALLOW_UNGOVERNED` (affirmative values only — `1/true/
  yes/on`) is honored at both `init` and `transition`; `GMDW_ALLOW_UNGOVERNED_INIT`
  still works at init.
- README on-ramp rewritten around actually using the tool; risk register and CLI docs
  updated for the above.

### Security
- **Recovery-command confinement.** `wf accept` / `wf clear-invocation` are denied by
  the PreToolUse gate while the hooks are live (the gate only runs when they are), so
  a misbehaving in-session Orchestrator cannot use them to self-accept an artifact or
  escape single-flight. Found and fixed during the plan security review (finding SF1).
- **Future-dated heartbeat bound.** Liveness rejects a heartbeat stamped beyond a small
  fixed skew, so a planted future timestamp cannot vouch for liveness indefinitely.

### Removed
- Run evidence bundles and a review deliverable that contained detail from a private
  third-party repository, plus a paused research note; run directories are now
  git-ignored by default (see `docs/GETTING_STARTED.md`). One curated GMDW-self run is
  retained as an example.

## [0.1.0-mvp] — 2026-07-12

Frozen MVP baseline: the state machine, six subagents, three governance hooks, the
`wf` CLI, nine artifact schemas, three policy profiles, and the governance test suite
(26 tests). See `RELEASE_NOTES_v0.1.0.md`.

[Unreleased]: https://github.com/arunvrrma/gmdw-claude-code/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/arunvrrma/gmdw-claude-code/releases/tag/v0.2.1
[0.2.0]: https://github.com/arunvrrma/gmdw-claude-code/releases/tag/v0.2.0
[0.1.0-mvp]: https://github.com/arunvrrma/gmdw-claude-code/releases/tag/v0.1.0-mvp
