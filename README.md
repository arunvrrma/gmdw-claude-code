# GMDW — Governed Multi-Agent Delivery Workflow for Claude Code

A controlled software-delivery workflow that runs specialized Claude Code subagents
under a **state machine** with **hook-enforced governance**. Built so that every
guardrail survives a misbehaving model: isolation, loop prevention, budget limits,
scope enforcement, and artifact integrity are enforced by deterministic Python — not
by asking the model to behave.

> **New here?** [**docs/GETTING_STARTED.md**](docs/GETTING_STARTED.md) is a
> step-by-step walkthrough from the user's seat — start there. The rest of this
> README is the architecture.

## Why it exists

Free-form agent swarms are unpredictable and unauditable. GMDW replaces the swarm
with one Orchestrator driving a fixed state machine, six single-purpose agents that
communicate only through versioned artifacts, and three hooks that make the rules
non-negotiable.

## The agents

| Agent | Role | Writes |
|---|---|---|
| repo-analyst | Read-only codebase reconnaissance | `repo_analysis_vN.json` |
| planner | Implementation plan (a contract) | `plan_vN.json` |
| plan-reviewer | Adversarial plan review | `plan_review_vN.json` |
| implementor | Code + tests + docs | `implementation_vN/` |
| security-reviewer | Defensive security review (plan & code) | `security_review_{plan,code}_vN.json` |
| code-reviewer | Implementation review vs. plan | `code_review_vN.json` |

Only the **Orchestrator** (`/gmdw`) invokes agents. Agents have no `Agent` tool, so
peer-to-peer invocation is structurally impossible.

## The state machine

```
RECEIVED → CLASSIFYING → REPOSITORY_ANALYSIS → PLANNING → PLAN_REVIEW
         → [PLAN_SECURITY_REVIEW] → [PLAN_APPROVAL] → IMPLEMENTING
         → [SECURITY_REVIEW] → CODE_REVIEW → APPROVED → COMPLETED
```
Bracketed states are conditional (security flags / gate policy). Any active state can
divert to `ESCALATED` (human decision) or `FAILED`; `CLASSIFYING` can `REJECTED`.
Trivial-route requests skip analysis and planning.

## Quick start

Open this repo in Claude Code **as the project root**, then just describe the change:

```
/gmdw Add a rate limiter to the public API
```

GMDW sizes the change, plans it, writes and reviews the code, and stops to ask your
approval at the plan gate (on larger changes) and always before anything ships.
That's the whole user experience — [**docs/GETTING_STARTED.md**](docs/GETTING_STARTED.md)
walks one run end to end.

> If `/gmdw` reports *"governance hooks are not active"*, you launched Claude Code
> from a different project root — relaunch it rooted in this repo. That refusal is
> the liveness guard doing its job (no hooks → no enforcement → no run).

Verify the toolchain any time (standard library only — no `pip install`):
```bash
python3 tests/test_governance.py     # the governance suite must pass
.workflow/bin/wf --help              # the CLI (you rarely touch it directly)
```

## How governance is enforced (the three hooks)

- **`pretooluse_gate.py`** (PreToolUse on Agent/Task, Write/Edit, Bash) — the boundary.
  Validates agent invocations against the state machine and the invocation header,
  enforces invocation/revision/review/budget/deadline limits and the per-role model
  policy, and confines every write to the state's allowed scope. Denies push/PR
  without deployment approval. **Fails closed.**
- **`posttooluse_ledger.py`** (PostToolUse) — accounts token usage per role, tracks
  budget and consecutive failures, registers artifact hashes. Records only.
- **`subagentstop_validate.py`** (SubagentStop) — the artifact acceptance gate. The
  decision itself (existence → schema → semantics: path existence, subject match,
  verdict consistency, deviation scope, **test re-verification** → counter bumps →
  version-pointer update → freeze) lives in `gmdw/accept.py`, shared verbatim by the
  human recovery path (`wf accept`); the hook is the thin session-plane adapter
  around it. **Fails closed** (integrity hold).

State, counters, budgets, and versioning live in `state.json`, mutated only through
`.workflow/bin/wf` and `.claude/hooks/gmdw/state.py`.

All three hooks attest liveness on every firing: `wf init` refuses to start a
workflow — and `wf transition` refuses to advance one — if that enforcement plane
isn't actually present in the session. Two human-attributed recovery commands
(`wf accept`, `wf clear-invocation`) exist only for genuinely hooks-dark sessions
and are denied while the hooks are live. (See ADR-001 for the design.)

## Policy profiles

`standard` (default), `strict` (regulated/production — blocking plan gate on every
route, security review always on, fewer auto-retries, higher budget), `expedited`
(prototypes — larger trivial threshold, lighter gates, cheaper models). Security
triggers and the deployment gate are **floors**: a profile can tighten them, never
loosen them — enforced by `policy.validate_profile`. Set the active profile in
`.workflow/config.json`.

## Auditability

Every workflow leaves a complete trail in `.workflow/runs/<id>/`:
`state.json` (+ `history[]`), `decision_log.jsonl` (append-only), `artifacts_index.json`
(sha256 of every artifact), immutable versioned artifacts, and `approvals/` (every
human decision). The provenance of any shipped line is reconstructable end to end.

## Requirements

Python 3.9+ (standard library only). Git (for the implementation base-commit and
scope checks). No third-party dependencies.

## Layout

See `CLAUDE.md` for the annotated directory map and the architecture invariants.
`tests/README.md` documents the governance scenarios and what each proves.
