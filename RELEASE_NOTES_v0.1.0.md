# GMDW v0.1.0-mvp — Release Notes

**Release:** `v0.1.0-mvp` (MVP baseline)
**Status:** Governance logic complete and verified offline (23/23). Not yet exercised
in a live Claude Code session — that is the next phase, on the `live-validation` branch.
**Scope of this release:** a frozen, source-controlled baseline of the Governed
Multi-Agent Delivery Workflow, ready for live validation.

## Overview

GMDW is a controlled software-delivery workflow for Claude Code. It replaces free-form
agent swarms with a single Orchestrator driving a fixed state machine, six
single-purpose subagents that communicate only through versioned artifacts, and three
hooks that make the governance rules non-negotiable. The design goal — met and tested —
is that **every guardrail survives a misbehaving model**: isolation, loop prevention,
budget limits, scope enforcement, and artifact integrity are enforced by deterministic
Python, not by asking the model to comply.

## Architecture summary

- **Orchestrator-centric.** The `/gmdw` main session is the only component with the
  `Agent` tool. The six subagents lack it, so peer-to-peer invocation is structurally
  impossible.
- **Artifact-only communication.** Agents read and write versioned JSON artifacts under
  `.workflow/runs/<id>/artifacts/`. No hidden channels.
- **State machine.** 15 states, a fixed transition table, and per-edge guard functions
  that read artifacts from disk (never conversation memory). Three routes: `trivial`,
  `medium`, `full`.
- **Single mutator.** `state.json` changes only through the `wf` CLI and the runtime
  library; direct writes are denied by the gate.
- **Three hooks.** PreToolUse (the enforcement boundary), PostToolUse (accounting +
  registration), SubagentStop (artifact acceptance). Hooks fail closed.
- **Policy pinned at init.** The active profile is copied into each run; editing a
  profile never changes a running workflow.

## Implemented features

- Runtime library: transition table + guards, profile validation with non-overridable
  floors, a stdlib-only JSON-Schema (Draft 2020-12 subset) validator, audit log + hash
  registry + budget tracking.
- Workflow CLI (`wf`): `init / status / expected / transition / approve / validate /
  close`, with atomic writes and audit logging of denials.
- Six agent definitions with tool-list capability boundaries.
- Orchestrator command with full routing, classification, escalation, and approval logic.
- Nine JSON schemas (envelope + eight artifact types + state).
- Three policy profiles: `standard` (default), `strict` (regulated/production),
  `expedited` (prototypes).
- Governance verification suite: 23 tests driving the real hooks and CLI, covering the
  12 required adversarial scenarios plus the false-test-pass integrity trap and positive
  controls.
- Validation documentation set (7 docs) and release documentation set (5 docs).

## Governance guarantees

1. Only the Orchestrator invokes agents (structural — subagents have no `Agent` tool),
   and each invocation is bound to state / expected agent / output path / model via a
   mandatory `GMDW-INVOCATION` header.
2. Agents communicate only through artifacts; writes are confined to per-state scope.
3. `state.json` is mutated only through the CLI / runtime library.
4. Loop prevention: `max_plan_revisions=2`, `max_impl_revisions=3`,
   `max_review_cycles=6`, `max_agent_invocations=14` (standard), enforced at both the
   invocation gate and the transition guards.
5. Token budget (2,000,000 standard) and a wall-clock deadline (12h), checked per
   invocation; 80% warns, 100% denies.
6. Artifacts are immutable once accepted (chmod a-w + sha256 registry); corrections are
   new versions.
7. Test-result integrity: the Implementor's declared test command is re-run and compared
   to the reported result; a mismatch freezes the workflow (integrity hold).
8. Approval gates: per-route/profile plan approval; deployment approval is a
   non-overridable floor gating push/PR/merge.
9. Fail-closed hooks.
10. Complete, append-only, hash-anchored audit trail.

## Repository structure

```
.claude/agents/            six subagent definitions (capability boundaries)
.claude/commands/gmdw.md    the Orchestrator procedure
.claude/hooks/              three hooks + the gmdw/ runtime library
.claude/settings.json       hook wiring
.workflow/schemas/          nine artifact contracts
.workflow/policies/         standard / strict / expedited profiles
.workflow/bin/wf            the CLI (sole legal state mutator)
.workflow/runs/<id>/        per-workflow state, artifacts, decision log, registry
tests/                      governance verification suite (23 tests)
docs/                       validation documentation (7 docs)
RELEASE_*, CONTRIBUTING, GIT_STRATEGY, LIVE_VALIDATION_EXECUTION   release docs
CLAUDE.md, README.md        project docs
```

## Known limitations (summary; full detail in `docs/KNOWN_LIMITATIONS.md`)

- Not yet run inside a live Claude Code session; hook firing order and `tool_response`
  metadata shape are unconfirmed (validation targets V1–V3).
- Per-role model policy enforcement depends on the runtime surfacing `model` to the
  hook; until confirmed live, treat it as advisory.
- Token budget is enforced on delegated (subagent) work; if exact usage fields are
  absent it falls back to a flagged estimate.
- `trivial_max_files` is defined in profiles but not hook-enforced.
- Caller attribution on writes is state-based, not identity-based (by design).
- The Bash gate is pattern-based, not a parser.
- Orphaned in-flight invocation cleanup after a crash is a manual escalate-to-clear step.
- Single active workflow, single in-flight invocation (concurrency is out of scope).

## Future roadmap (not in this release)

- **v0.2 (post-live-validation):** fold live-run findings (metadata shapes, hook order)
  into the docs; confirm or downgrade model-policy enforcement; optional
  `trivial_max_files` enforcement.
- Parallel implementor fan-out for disjoint milestones (worktree-isolated).
- Workflow dashboard / status report over `index.json`.
- Postgres-backed state + artifact store for multi-user, org-level budget reporting.
- CI-gated finalization and multi-approver routing.
- Escalation/misclassification mining to auto-tune thresholds.

## Upgrade / compatibility

First release — no upgrade path. Requires Python ≥ 3.9 and Git; no third-party
dependencies.
