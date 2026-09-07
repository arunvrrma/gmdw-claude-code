# Changelog

All notable changes to GMDW are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-09-07

The architecture rework. GMDW's **routing layer** was rebuilt; its **enforcement
layer** was preserved. The governance code was never the problem — what was wrong
was what it was serving: an LLM pretending to be a state machine.

Full rationale, per-change detail and an upgrade checklist: [MIGRATION.md](MIGRATION.md).

### Added
- **`gmdw/orchestrator.py`** — a deterministic Python orchestrator. Reads state,
  looks up the successor in the transition table, starts one agent, parses and
  validates its output, advances. No model is consulted about routing.
  `python3 -m gmdw.orchestrator run|resume|status`.
- **`gmdw/runner.py`** — the `claude` CLI adapter, the single place a model is
  called. Prompt goes on stdin (a plan-sized prompt would outgrow ARG_MAX);
  usage is read from the `--output-format json` envelope. `ScriptedRunner` makes
  the whole loop testable with no model in it.
- **`gmdw/parsers.py`** — structured-Markdown artifact parsing and validation.
- **`gmdw/prompts/`** — one prompt template per agent role.
- **Two mandatory human gates**, `PLAN_APPROVAL` and `FINAL_APPROVAL`, and the
  `final` approval gate in `wf approve`. `accept.human_approval_satisfied()` is
  the single home of the decision: it requires a frozen approval artifact carrying
  `human_approved: true` that names the current version of what it signs.
- **`--auto-approve`** — waives the plan and final gates on a `trivial`-route
  workflow whose profile permits it, never the deployment gate, always recorded
  (`auto_approve_enabled` / `auto_approve_used` / `auto_approve_refused`).
- **`heartbeat.enforcement_installed()`** — a static check that all three hooks are
  registered and present, for the orchestrator, which starts before any session
  exists. `wf doctor` now reports *registered* and *live* separately.
- `hook_liveness`, `gate_denied` and `awaiting_human` ledger events.
- `tests/test_parsers.py` (34) and `tests/test_orchestrator.py` (29); shared
  `tests/fixtures.py`. The suites total **146 tests**.

### Changed
- **Six agents became three.** `analyst-planner` (repo-analyst + planner +
  plan-reviewer), `implementor`, `reviewer` (security-reviewer + code-reviewer).
  The Implementor moved to **opus** — the one place the rework spends more.
- **Artifacts are structured Markdown**, not JSON: a `---` front-matter envelope
  and `## ` sections. The point is that validation failures became *local*, so a
  retry rewrites one section instead of regenerating the artifact. JSON remains
  for fenced small data and for the approval record, which a machine writes.
- **The state machine**: `RECEIVED → ANALYSIS_PLANNING → PLAN_APPROVAL →
  IMPLEMENTING → REVIEW → FINAL_APPROVAL → APPROVED → COMPLETED`.
- The `gmdw` package moved from `.claude/hooks/gmdw/` to a top-level `gmdw/`, and
  is now governance-protected — a run cannot rewrite its own router.
- Invocation, revision, review-cycle, budget and deadline limits are checked in
  `Orchestrator._limits_preflight`, before the spend rather than at the moment of
  it. **`cost_ceiling_usd` is now enforced**; it was advisory.
- Policy profiles: three model roles, a new `gates.final_approval`, and lower
  budgets to match the smaller pipeline (standard: 14 → 8 invocations,
  2,000,000 → 900,000 tokens, $25 → $12). A v0.2 profile fails validation.
- `policy.validate_profile` gained a floor: a human gate may be `off` only on the
  `trivial` route.
- The trivial route is now plan-scoped like every other route (v0.2 permitted any
  non-protected write there, because it had no plan).
- `.claude/commands/gmdw.md` is a thin launcher; the main session is no longer
  the Orchestrator.
- `pretooluse_gate.py` denies subagent invocation outright, denies `wf approve`
  from inside an agent session, and audits every denial.
- `posttooluse_ledger.py` no longer accounts tokens or closes invocations — the
  orchestrator owns that lifecycle, and doing it in both would double-count.

### Removed
- The LLM-driven Orchestrator role, and the `GMDW-INVOCATION` header protocol it
  existed to satisfy.
- The `repo-analyst`, `planner`, `plan-reviewer`, `security-reviewer` and
  `code-reviewer` agent definitions.
- Six JSON artifact schemas (`classification`, `repo_analysis`, `plan`, `review`,
  `security_review`, `implementation_manifest`, `envelope`); `approval` and
  `state` remain.

### Fixed (found by the first live run, `wf_20260828_001`)
- `parsers.nested_bullets` kept only a bullet's first line, silently truncating a
  wrapped rationale in the plan briefing a human reads at the gate. Indented
  continuation lines now belong to the bullet they continue.
- `parsers._clean` stripped a trailing backtick unconditionally, mangling prose
  that ends in inline code (`` wire it into `main()` ``). It now unwraps only a
  value that is *entirely* one span.
- The plan gate now states that no independent model review was performed, since
  the human's approval is the sole quality check on the plan.
- `token_budget` raised (standard 900,000 → 2,500,000; strict → 5,000,000;
  expedited → 1,200,000). The first run consumed 1.11M tokens on a *trivial*
  three-file change, because cached reads count at full weight — see
  `docs/KNOWN_LIMITATIONS.md` §B5-B6.

### Preserved
Single-flight locking, the governance heartbeat, cost ceilings and token budgets,
`decision_log.jsonl` and `artifacts_index.json` formats, artifact SHA-256 hashing
and freezing, write-scoping, test re-verification and the integrity hold,
fail-closed hooks, and the human recovery commands — all unchanged in behavior.


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
