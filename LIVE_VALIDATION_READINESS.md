# GMDW — Live Validation Readiness

**Branch:** `live-validation` (isolated from the frozen `v0.1.0-mvp` baseline).
**Purpose:** the single readiness artifact that gates the first live Claude Code run.
It synthesizes `docs/LIVE_VALIDATION_PLAN.md` (V1–V8), `docs/FIRST_LIVE_RUN_CHECKLIST.md`,
`docs/SMOKE_TESTS.md`, and `docs/ADVERSARIAL_VALIDATION.md` into objectives, pass/fail
criteria, the test-repo requirements, the execution sequence, rollback, and evidence
collection — plus the concrete Phase-5 plan for the first run (Workflow A).

Runtime is frozen: this phase produces **evidence and documentation only**. No changes
to code, schemas, hooks, agents, policies, or the CLI.

---

## 1. Validation objectives

The offline suite (23/23) proved the governance *logic*. Live validation proves the
logic is **wired into the runtime** — that hooks fire, metadata arrives in the shape the
code parses, and the state machine advances under real subagents. Concretely:

- **O1 — Hook activation.** All three hooks (PreToolUse, PostToolUse, SubagentStop)
  actually fire during a real subagent turn. *(LIVE_VALIDATION_PLAN V1)*
- **O2 — Metadata shapes.** Token usage (`tool_response`) and invocation metadata
  (`subagent_type`, `model`) arrive as the gate/ledger expect, or the flagged fallbacks
  engage. *(V2, V3)*
- **O3 — Artifact lifecycle.** Write-scope, versioning, freeze, and immutability behave
  live. *(V4)*
- **O4 — Approval gates.** Plan and deployment gates block and record correctly. *(V5)*
- **O5 — Budget/limits.** Soft-warn and hard-deny fire on real accumulation. *(V6)*
- **O6 — State machine.** The three routes reproduce their documented state paths;
  illegal transitions are refused. *(V7)*
- **O7 — Recovery.** A workflow resumes cleanly after interruption; an orphaned
  in-flight invocation is handled safely. *(V8)*
- **O8 — Adversarial parity.** Violations denied offline are denied live. *(ADVERSARIAL_VALIDATION)*
- **O9 — Audit fidelity.** Every action leaves the expected `decision_log.jsonl` entry
  and the evidence bundle is self-consistent.

Also resolve the open questions Q1–Q5 (`docs/KNOWN_LIMITATIONS.md §D`).

## 2. Success criteria

Live validation **passes** when all hold:

- V1–V8 pass, **or** any deviation is one of the accepted coarser modes (R2 estimated
  tokens, R3 advisory model policy) and is recorded with rationale.
- Smoke Workflows A, B, C each reach `COMPLETED` on their documented state paths.
- At least three adversarial cases (one per enforcement family — write-scope, security
  bypass, budget) reproduce their denials live; the false-test-pass integrity trap fires
  if exercised.
- No governance guarantee from `docs/HANDOFF.md §3` is observed to fail.
- Every run leaves a complete, frozen, self-consistent evidence bundle under
  `.workflow/runs/<id>/`.
- Q1–Q5 answered; model-policy and token-accounting verdicts recorded.

## 3. Failure criteria (release blockers)

Any one of these **stops** the phase — it is a wiring failure, not a logic failure, and
must be raised as an integration issue (not fixed under the freeze):

- **A hook does not fire** — a violation is *allowed*, or `invocation_requested` never
  logs. This is the single most important thing to detect (§ the pre-flight gate-live
  smoke check exists for exactly this).
- **A guarded transition is wrongly allowed** (e.g. security-review bypass succeeds,
  deployment without approval reaches `COMPLETED`, a revision/invocation/budget limit is
  exceeded).
- **An artifact advances a version pointer without validation**, or a frozen artifact is
  successfully mutated through a tool.
- **State corruption** — `wf status` errors, or a version pointer double-advances.

Accepted **non-failures** (record, don't block): estimated token counts (R2); advisory
model policy if the runtime does not surface `model` (R3); a long test suite yielding
`observed=timeout` on re-verification (use `rerun_tests:false` or a fast subset).

## 4. Required test repositories

Live workflows need real files to change. Use an **isolated `sandbox/` subtree** inside
this repo so runs never touch framework code, committed so the Implementor's
`base_commit` ancestry check has a clean HEAD.

```
sandbox/
  config.py              # DEFAULT_TIMEOUT constant + a reference (Workflow A target)
  api/
    handlers.py          # a handler using the existing logger (Workflow B target)
tests/
  test_sandbox.py        # a trivial passing test over the sandbox (re-verification target)
```

Seed content and commands are in `LIVE_VALIDATION_EXECUTION.md §2`. Record the seed
commit SHA — it is the per-run sandbox rollback point. Workflow C (auth middleware)
targets `sandbox/api/` and does not need new seed files beyond a route to protect.

## 5. Execution sequence

1. **Pre-flight** (`docs/FIRST_LIVE_RUN_CHECKLIST.md §0–§1`): confirm Python/Git, `wf`
   executable, hooks reachable (`echo '{}' | python3 .claude/hooks/pretooluse_gate.py`
   → exit 0), `$CLAUDE_PROJECT_DIR` resolves, offline suite green, and the **gate-live
   smoke check** (attempt a protected write under a scratch active workflow → must Deny).
2. **Seed the sandbox** (§4 / `LIVE_VALIDATION_EXECUTION.md §2`); commit it.
3. **Workflow A** (trivial) — the first run; detailed plan in §8 below.
4. **Workflow B** (medium) — analysis → plan → implement; verifies plan-scoped writes
   and the notify gate.
5. **Workflow C** (full/security) — the strongest test; verifies unskippable security
   states and the two human gates.
6. **Adversarial spot-checks** — #1, #5, #10 minimum (per family), + integrity bonus.
7. **Record results** in `docs/LIVE_VALIDATION_RESULTS.md` (new evidence doc on this
   branch); re-score `docs/PRODUCTION_READINESS.md`.
8. **Merge** `live-validation → main` (`--no-ff`) once complete.

Run V1–V8 opportunistically *within* steps 3–5 (they observe the same runs), per
`docs/LIVE_VALIDATION_PLAN.md`.

## 6. Rollback strategy

- **Abandon a run cleanly:** `wf transition ESCALATED --reason other --detail "abort"`
  → `wf approve escalation --resolution abandon` → `wf transition REJECTED` → `wf close`.
  Nothing was pushed (deployment gate); WIP stays on disk.
- **Orphaned in-flight invocation** (session died with `open_invocation` set): reopen CC,
  `wf status`; if the artifact landed and was accepted, continue; else
  `wf transition ESCALATED --reason agent_blocked` then resolve. Never hand-edit
  `state.json`.
- **Integrity hold** (tamper / false pass): workflow frozen to `ESCALATED`/`FAILED`;
  inspect the `integrity_violation` log entry; resolve via `wf approve escalation`
  (force_approve only with recorded `--overridden-findings`, or abandon).
- **Sandbox corruption:** `git checkout <seed-sha> -- sandbox/` or `git reset --hard
  <seed-sha>` on this branch.
- **Framework rollback:** `git checkout v0.1.0-mvp` restores the pristine baseline. The
  tag + `mvp-complete` are the immovable anchors; live-validation commits never touch
  them.

## 7. Evidence collection strategy

- **Per run, the evidence bundle is the run directory** `.workflow/runs/<id>/`:
  `state.json` (+ `history`), `decision_log.jsonl` (append-only), `artifacts_index.json`
  (sha256 of every artifact), the immutable versioned artifacts, and `approvals/`. It is
  self-contained and diff-able.
- **Commit each bundle** to `live-validation` immediately after the run
  (`git add .workflow/runs/ && git commit -m "live-validation: Workflow <X> evidence"`).
- **Aggregate findings** in `docs/LIVE_VALIDATION_RESULTS.md`: V1–V8 verdicts with the
  observed evidence per check; Q1–Q5 answers; model-policy verdict (enforcing/advisory);
  token verdict (exact/estimated); per-workflow state-path diff (observed vs
  `docs/SMOKE_TESTS.md`); links to the committed bundles.
- **Adversarial evidence:** for each attempted violation, capture the verbatim
  deny/block reason string and the corresponding `decision_log.jsonl` line.
- **Chain of custody:** git history on `live-validation` is the audit trail of the
  validation phase itself; the `--no-ff` merge to `main` preserves it as a distinct
  merge commit.

---

## 8. First validation run plan — Workflow A (trivial route)

**Goal:** validate hook activation, artifact creation, state transitions, audit logging,
and deployment-approval enforcement on the shortest legal path. **Do not run yet** — this
is the plan; execution is authorized separately.

### 8.1 Exact test-repository structure (seed before the run, commit)

```
sandbox/
  config.py
```
```python
# sandbox/config.py
DEFAULT_TIMEOUT = 30

def get_timeout():
    return DEFAULT_TIMEOUT
```
Optional (recommended) so re-verification has something to run:
```
tests/test_sandbox.py
```
```python
# tests/test_sandbox.py
from sandbox.config import get_timeout
def test_timeout():
    assert get_timeout() == 30
```
Commit: `git add -A && git commit -m "live-validation: seed sandbox for Workflow A"`.
Record the seed SHA.

### 8.2 Exact user request

```
/gmdw Rename the constant DEFAULT_TIMEOUT to DEFAULT_TIMEOUT_SECONDS in sandbox/config.py and update its references
```

### 8.3 Expected route

`trivial` — classification total ≤ 3 (files_impacted 1, all other dimensions 0), no
security/data_model override flags. Skips REPOSITORY_ANALYSIS and PLANNING.

### 8.4 Expected state path

```
RECEIVED → CLASSIFYING → IMPLEMENTING → CODE_REVIEW → APPROVED → COMPLETED
```
(Verify against `state.json.history` after the run.)

### 8.5 Expected artifacts (under `.workflow/runs/<id>/artifacts/`)

| Artifact | Producer | Key content to confirm |
|---|---|---|
| `classification_v1.json` | orchestrator | `route:"trivial"`, `override_flags:[]`, `total ≤ 3` |
| `implementation_v1/manifest.json` (+ `diff.patch`, `test_output.txt`) | implementor | `changes` lists `sandbox/config.py`; `tests.result` honest; `base_commit` = seed SHA (ancestor of HEAD) |
| `code_review_v1.json` | code-reviewer | `mode:"code"`, `verdict:"approve"` |
| `approvals/deployment_approval.json` | human (via `wf approve`) | `gate:"deployment"`, `decision:"approved"`, frozen |

Each accepted artifact must be **frozen**: no write bit (`ls -l`), and a sha256 entry in
`artifacts_index.json`.

### 8.6 Expected audit events (`decision_log.jsonl`)

In order (approx.): `workflow_created` → `transition`(→CLASSIFYING) →
`artifact_written`+`artifact_accepted`(classification, via orchestrator write) →
`transition`(→IMPLEMENTING) → `invocation_requested`+`invocation_completed`(implementor)
→ `artifact_accepted`(implementation) → `verification`(tests_rerun; if a runnable
command was declared) → `transition`(→CODE_REVIEW) →
`invocation_requested`+`invocation_completed`(code-reviewer) →
`artifact_accepted`(code_review) → `transition`(→APPROVED) → `human_approval`(deployment)
→ `transition`(→COMPLETED) → `workflow_closed`.

### 8.7 Expected approvals

- **Deployment approval only** (plan gate is `off` on trivial/standard). The
  `COMPLETED` transition is **blocked** until `wf approve deployment` is recorded, and
  any `git push` is denied until then — this is the deployment-enforcement check.

### 8.8 Expected final state

`state: COMPLETED`; `.workflow/ACTIVE` removed after `wf close`; `index.json` shows the
workflow `COMPLETED` with `closed_at`, token estimate, and cost; the sandbox constant is
actually renamed on disk.

### 8.9 What a pass proves

Hooks fired (invocation + artifact events present); artifacts were created, validated,
and frozen; the state machine advanced along the trivial path; the audit log is complete
and ordered; and **deployment approval was enforced** (no `COMPLETED`, no push, without
the recorded approval). Capture the run directory as the first evidence bundle.

### 8.10 Failure handling for this run

If `invocation_requested` never appears → PreToolUse not firing (settings/`$CLAUDE_
PROJECT_DIR`); STOP (release blocker). If the protected-write pre-flight smoke check was
skipped and a protected write succeeds → STOP. If `COMPLETED` is reachable without the
deployment approval → release blocker. Otherwise follow §6 rollback and record in the
results doc.
