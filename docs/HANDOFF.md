# GMDW — Implementation Handoff

**Audience:** the engineer running the first live validation in Claude Code.
**Status at handoff:** MVP complete; 23/23 governance tests + a full trivial-route
CLI lifecycle walk passing offline. Not yet exercised inside a live Claude Code
session (real subagents, real hook firing).

This document states what exists, what it guarantees, and what it assumes. It does
not propose changes. Companion docs: `KNOWN_LIMITATIONS.md`, `LIVE_VALIDATION_PLAN.md`,
`SMOKE_TESTS.md`, `ADVERSARIAL_VALIDATION.md`, `PRODUCTION_READINESS.md`,
`FIRST_LIVE_RUN_CHECKLIST.md`.

## 1. Implemented capabilities

| Component | Location | What it does |
|---|---|---|
| Runtime library | `.claude/hooks/gmdw/{state,policy,schemas,ledger}.py` | Transition table + guards, profile validation, stdlib JSON-Schema subset validator, audit log + hash registry + budget. Sole mutator of `state.json`. |
| Workflow CLI | `.workflow/bin/wf` | `init / status / expected / transition / approve / validate / close`. The only sanctioned entry point that changes workflow state. |
| PreToolUse gate | `.claude/hooks/pretooluse_gate.py` | Governs `Agent/Task`, `Write/Edit/MultiEdit`, `Bash`. Fails closed. |
| PostToolUse ledger | `.claude/hooks/posttooluse_ledger.py` | Token accounting per role, budget, consecutive-failure tracking, artifact hashing. Records only. |
| SubagentStop validator | `.claude/hooks/subagentstop_validate.py` | Artifact acceptance: existence → schema → semantics → test re-verify → freeze. Fails closed. |
| Six agents | `.claude/agents/*.md` | repo-analyst, planner, plan-reviewer, implementor, security-reviewer, code-reviewer. None has the `Agent` tool. |
| Orchestrator | `.claude/commands/gmdw.md` | `/gmdw <request>` — drives the state machine; the only holder of `Agent`. |
| Schemas | `.workflow/schemas/*.schema.json` | 9 contracts (envelope + 8 artifact types + state), Draft 2020-12. |
| Policy profiles | `.workflow/policies/{standard,strict,expedited}.json` | Pinned into each run at init. |
| Governance suite | `tests/test_governance.py` | 23 tests driving real hooks + CLI. |

## 2. State machine (as implemented)

15 states: `RECEIVED, CLASSIFYING, REPOSITORY_ANALYSIS, PLANNING, PLAN_REVIEW,
PLAN_SECURITY_REVIEW, PLAN_APPROVAL, IMPLEMENTING, SECURITY_REVIEW, CODE_REVIEW,
APPROVED, ESCALATED, COMPLETED, FAILED, REJECTED`. Terminal: `COMPLETED / FAILED /
REJECTED`. Every edge is in a fixed table AND behind a guard function that reads
artifacts from disk. Routes: `trivial` (skips analysis + planning), `medium`,
`full`. Bracketed states are conditional on security/data-model flags and gate policy.

## 3. Governance guarantees (enforced by code, not prompts)

1. **Orchestrator-centric invocation.** Subagents lack the `Agent` tool → peer-to-peer
   invocation is structurally impossible. The gate additionally binds each invocation
   to the current state, the expected agent, the expected output path, and the
   policy-assigned model, via a mandatory `GMDW-INVOCATION` header.
2. **Artifact-only communication.** Agents exchange only versioned JSON files; the
   gate confines every write to the state's allowed scope.
3. **Single mutator.** `state.json` changes only through `wf` / `gmdw.state`. Direct
   writes are denied by the gate (protected paths).
4. **Loop prevention.** `max_plan_revisions=2`, `max_impl_revisions=3`,
   `max_review_cycles=6`, `max_agent_invocations=14` (standard). Counters live in
   `state.json`, are bumped by hooks, and are checked by both the invocation gate and
   the transition guards — defense in depth.
5. **Budget + deadline.** Token budget (2,000,000 standard) and a wall-clock deadline
   (12h) are checked at every invocation; 80% crosses a soft warning, 100% denies.
6. **Immutable, versioned artifacts.** On acceptance each artifact is frozen
   (`chmod a-w` + sha256 in `artifacts_index.json`). Corrections are new versions.
7. **Integrity verification.** The validator re-runs the Implementor's declared test
   command (profile-gated) and compares to the reported result; a mismatch sets an
   integrity hold and freezes the workflow.
8. **Approval gates.** Plan-approval mode is per-route/profile; deployment approval is
   a non-overridable floor (guards + the Bash gate block push/PR without it).
9. **Fail-closed.** PreToolUse denies on internal error while a workflow is active;
   SubagentStop sets an integrity hold.
10. **Complete audit trail.** `decision_log.jsonl` (append-only), `state.json.history`,
    `artifacts_index.json`, immutable artifacts, and `approvals/` reconstruct any
    outcome end to end.

## 4. Audit event vocabulary (`decision_log.jsonl`)

`workflow_created, transition, transition_denied, invocation_requested,
invocation_completed, invocation_failed, artifact_written, artifact_accepted,
artifact_missing, artifact_invalid, artifact_rejected, artifact_blocked,
budget_warning, verification, integrity_violation, human_approval, validator_error,
workflow_closed`. (`artifact_missing/invalid/rejected` are emitted by the validator's
`_fail` path.)

## 5. Architectural assumptions (locked; carried forward for live validation)

1. Orchestrator = the main `/gmdw` session, never a subagent.
2. Hook matchers use `Agent|Task` to survive the subagent tool-name across CC versions.
3. Stdlib-only → a **subset** JSON-Schema validator; schemas avoid `$ref/$defs/anyOf`.
4. Immutability is applied on **acceptance** (SubagentStop), not first write — an agent
   may rewrite its own draft within one invocation.
5. `route`/`flags` are synced **from the latest classification artifact** at transition
   time; the artifact is the source of truth.
6. PostToolUse moves `open_invocation → last_invocation` so SubagentStop can validate
   **regardless of hook firing order** (order-independent by design).
7. Escalation resolutions each unlock exactly one target
   (redirect→PLANNING, override→IMPLEMENTING, force_approve→APPROVED, abandon→REJECTED).
8. Approval artifacts are written only by `wf approve`; `artifacts/approvals/**` is
   Write-denied.
9. Token usage is read from the Agent/Task `tool_response`; if the expected fields are
   absent, a `len/4` estimate is used and flagged `estimated`.

## 6. What live validation must establish (the open items)

These are precisely the assumptions that cannot be proven offline:

- **A1 — Hook firing order** (PostToolUse vs SubagentStop for a subagent). Designed to
  work either way; must be observed.
- **A2 — Token metadata shape** in the real `tool_response`. Determines whether budget
  enforcement uses exact counts or the estimate fallback.
- **A3 — Agent invocation metadata**: does the runtime pass `subagent_type` and a
  `model` field to the PreToolUse hook as the gate expects?
- **A4 — `$CLAUDE_PROJECT_DIR`** resolves for the hook commands in `settings.json`.
- **A5 — Subagent prompt plumbing**: the Orchestrator can place the `GMDW-INVOCATION`
  header verbatim into the subagent prompt the gate reads.

`LIVE_VALIDATION_PLAN.md` turns each of these into an executable check.
