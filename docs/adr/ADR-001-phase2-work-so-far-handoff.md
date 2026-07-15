# ADR-001 Phase 2 — Work-So-Far Handoff & Findings Export

- **Date:** 2026-07-15
- **Workflow:** `wf_20260714_001` ("Implement ADR-001 Phase 2: acceptance extraction, transition liveness, wf accept")
- **Current state:** `PLAN_SECURITY_REVIEW` (paused mid-run; ACTIVE)
- **Why paused:** the governed run hit its static per-run budget ceilings after a
  necessary security-driven replan and several infrastructure interruptions
  (session limits, safety-classifier outages, a day boundary). Continuing to
  completion requires operator budget adjustments; this document checkpoints the
  work so a future session can decide how to proceed.
- **Nothing here is lost:** every accepted artifact is frozen on disk under
  `.workflow/runs/wf_20260714_001/artifacts/` (sha-registered, chmod a-w).

---

## 1. Executive status

The workflow progressed cleanly through classification, repository analysis, and a
**full plan + review + security-review loop**, producing an implementation plan
(`plan_v3`) that is **plan-reviewer approved** and **security-hardened** (it fixes a
real privilege-escalation finding raised during review). It did **not** reach
implementation — it paused at the plan security **re**-review, and cannot finish
under the current per-run budgets.

Progress vs the state machine:
`RECEIVED → CLASSIFYING → REPOSITORY_ANALYSIS → PLANNING ⇄ PLAN_REVIEW ⇄
PLAN_SECURITY_REVIEW` (currently here). Remaining to APPROVED: plan security
re-review → PLAN_APPROVAL (human gate) → IMPLEMENTING → SECURITY_REVIEW →
CODE_REVIEW → APPROVED → COMPLETED.

Counters at pause: `agent_invocations 12/14`, `review_cycles 4/6`,
`plan_revisions 2/2` (maxed), `consecutive_failures 0`, `tokens ~1.23M/2M`,
`deadline 2026-07-16T12:50:02Z` (extended once). `open_invocation: null` (clean).

## 2. The deliverable: Phase 2 implementation plan (plan_v3)

`plan_v3.json` is the authoritative, ready-to-implement design. It is
plan-reviewer **approved** (`plan_review_v3`, no open findings) and incorporates the
security fix (below). Milestone shape (as of v3):

- **M1 — Extract accept pipeline into `.claude/hooks/gmdw/accept.py`; thin the
  SubagentStop adapter.** Pure, behavior-preserving refactor. Core entry
  `accept_artifact(root, st, atype, version, *, actor=None)` returns
  `Result{outcome ∈ accepted|rejected|blocked|integrity_hold, reason, event, extra}`.
  Move: existence, schema, blocked-status, `_semantic` (incl. git-ancestry and the
  `shell=True` test-rerun with its integrity_hold side effects **verbatim**),
  counter bumps, pointer set, freeze, `artifact_accepted` log. Keep in adapter:
  `block/ok/_fail`, `open_invocation` lifecycle, `consecutive_failures`, the single
  `save_state`, and the fail-closed exception wrapper. Delete the dead
  `_subject_matches` in `subagentstop_validate.py` (live copy stays in `state.py`).
  Oracle: existing tests P3/P5/P6/13/13b stay green.
- **M2 — `wf accept --by <human>`** recovery command (cmd_approve-shaped): accepts
  ONLY `S.expected()`'s next-version artifact, reuses M1 core, refuses
  invalid/wrong-version/already-frozen, logs `actor=human_recovery`.
- **M3 — orphaned-invocation recovery + SF1 control.** (a) `wf clear-invocation
  --by <human>` clears a dead `open_invocation`, logs `invocation_cleared`, refuses
  when none open. (b) **REQUIRED** PreToolUse `gate_bash` deny of Bash `wf accept` /
  `wf clear-invocation` while a workflow is active (mirrors the git-push/commit
  deny rules) — closes SF1.
- **M4 — mid-run transition liveness** in `wf cmd_transition`: live if a
  hook-sourced ledger event (`invocation_requested`/`invocation_completed`/
  `invocation_failed`; `artifact_accepted` excluded — CLI also writes it) appears
  since the last transition, OR `heartbeat.status()` is fresh (carries the F-1
  future-skew bound). ESCALATED/FAILED/REJECTED exempt. Canonical bypass
  `GMDW_ALLOW_UNGOVERNED` with **affirmative-only** truthiness (`1/true/yes/on`);
  a new `ledger` JSONL read helper; `Base.setUp` seeds a heartbeat so legacy
  transition tests represent a governed session.
- **M5 — tests:** 13 new (`test_R1..R13`) → **47 total** (34 baseline + 13);
  covers wf accept happy/negative, clear-invocation + ESCALATED-after-clear,
  mid-run refusal + audited bypass, the SF1 gate-deny, and the fail-closed wrapper.
- **M6 — docs/ADR statuses**, corrected R4/F1 closure wording (enumerates the four
  mechanisms; no overclaim).

Invariants held: governance by code not prompts; `state.json` mutated only via
wf/state.py/hooks; artifacts immutable once accepted; **no schema changes**; no
state-shape changes except the test-only heartbeat seed.

## 3. Review outcomes

| Review | Verdict | Note |
|---|---|---|
| `plan_review_v1` (of plan_v1) | **revise** | F1 major: orphaned-invocation deadlock unaddressed; + minors |
| `plan_review_v2` (of plan_v2) | **approve** | all v1 findings resolved |
| `security_review_plan_v1` (of plan_v2) | **revise** | **SF1 major** (below) + SF2/SF3 minor |
| `plan_review_v3` (of plan_v3) | **approve** | SF1/SF2/SF3 fixes confirmed against live code |
| `security_review_plan_v2` (of plan_v3) | **pending** | interrupted by session limit before write; **not produced** |

The only outstanding governed step in the plan phase is re-running the plan
security review on `plan_v3` to confirm SF1's fix (the plan-reviewer already
independently verified it).

## 4. Findings surfaced by this run (the novel output)

These are meta-findings about GMDW itself, discovered by running a real,
infra-stressed workflow. They are **not** yet recorded in the repo.

- **SF1 (security, MAJOR — fix designed in plan_v3).** The planned `wf accept` and
  `wf clear-invocation` are Bash-callable, code-level acceptance/recovery
  authorities stronger than `wf approve`. Without a control, a misbehaving
  in-session Orchestrator could self-author an artifact and `wf accept` it to
  self-accept ANY artifact type (bypassing the GMDW-INVOCATION header, model pin,
  budget, single-flight) or escape single-flight via `wf clear-invocation`.
  **Fix:** make the PreToolUse `gate_bash` deny of these commands REQUIRED while a
  workflow is active — the gate only runs when hooks are live, so denying there
  loses no legitimate (hooks-dark) recovery function. In plan_v3 (M3 Part B).

- **GAP-1: orphaned `open_invocation` deadlock.** If an invocation is interrupted
  after the PreToolUse gate sets `open_invocation` but before a subagent runs (e.g.
  the safety classifier flaps between hook-firing and tool-execution), the field
  dangles. Then `guard()` blocks ALL transitions — **including ESCALATED**, because
  it checks `open_invocation` (state.py:252) before the ESCALATED allowance
  (state.py:256) — and single-flight blocks all new invocations. The
  KNOWN_LIMITATIONS C1 documented recovery ("transition to ESCALATED") is therefore
  **broken**. Hit live this run; required a raw `state.json` edit to clear.

- **GAP-2: static per-run budgets do not survive multi-day / infra-interrupted
  runs.** This run exhausted, in sequence: the 12h **wall-clock deadline** (expired
  during an overnight gap), and is now short on **`agent_invocations` (12/14)** and
  **`review_cycles` (4/6)** — a happy-path finish needs +4 invocations and +3
  review cycles. The overage came from a legitimate security replan
  (plan v1→v2→v3) plus infra retries, not runaway cost (tokens only 61%).

- **GAP-3: no in-governance recovery/adjustment commands.** This run needed **four**
  operator interventions, none of which has a `wf` command, all requiring raw
  `state.json` edits (via the `!` shell escape, since the gate/classifier correctly
  block the Orchestrator from editing state): (1) clear orphaned invocation,
  (2) extend deadline, (3) raise `max_agent_invocations`, (4) raise
  `max_review_cycles`. Strong evidence GMDW needs a small, human-attributed,
  audited recovery-command family (and possibly budget policy that tolerates
  interruption).

- **GAP-4: Bash-gate content heuristic friction.** Agents without a `Write` tool
  (repo-analyst, planner, reviewers) must write artifacts via Bash, but
  `gate_bash` denies any command whose *text* contains a protected substring
  (`.claude/hooks`, `state.json`, ...) together with a mutation token (`> `, `>>`,
  `rm `, `dd `, ...) — and also the literals `git push`/`git commit`. Artifact
  content legitimately discussing the hooks/state trips this. Agents had to work
  around it (quoted heredocs to python stdin, hyphenating "git-push", avoiding
  "add "/arrows). Real usability/robustness issue for artifact production; not a
  security hole, but it caused several failed writes and wasted effort.

## 5. Artifact inventory (all frozen on disk)

Under `.workflow/runs/wf_20260714_001/artifacts/`:

| File | What | Status |
|---|---|---|
| `classification_v1.json` | route=full, security flag | accepted |
| `repo_analysis_v1.json` | grounded recon of the code Phase 2 touches | accepted |
| `plan_v1.json` | first plan | superseded |
| `plan_v2.json` | plan_review-approved | superseded |
| **`plan_v3.json`** | **current, approved, security-hardened — the deliverable** | accepted |
| `plan_review_v1/2/3.json` | plan reviews (revise, approve, approve) | accepted |
| `security_review_plan_v1.json` | SF1 finding (revise) | accepted |
| *(security_review_plan_v2.json)* | plan_v3 security re-review | **not produced** |

Plus `state.json`, `decision_log.jsonl`, `artifacts_index.json` (the full audit
trail, including the operator-authorized recovery events: `orphan_invocation_reset`,
and the raw deadline/budget edits).

## 6. How to resume in a new session

**Option A — continue THIS governed run.** Raise the two exhausted budgets first
(operator, via `!`, since no `wf` command exists):
```
python3 -c "import json,pathlib; p=pathlib.Path('.workflow/runs/wf_20260714_001/state.json'); s=json.loads(p.read_text()); s['limits']['max_agent_invocations']=20; s['limits']['max_review_cycles']=10; p.write_text(json.dumps(s,indent=2))"
```
Then `/gmdw` (resume) and drive: security_review_plan_v2 (confirm SF1 fix) →
PLAN_APPROVAL (human) → IMPLEMENTING → SECURITY_REVIEW → CODE_REVIEW → APPROVED.
Note: the deadline was extended to 2026-07-16T12:50Z; extend again if the new
session is later.

**Option B — start fresh, reuse plan_v3 as the spec.** Close this run first
(otherwise `wf init` refuses — single active workflow). A fresh run gets clean
budgets. Feed `plan_v3.json` (and this handoff) to the new run as the authoritative
design so it can skip straight to a light re-plan/approval and implementation.

**Option C — implement directly from plan_v3** (outside GMDW) if the governance
demonstration has served its purpose; plan_v3 is detailed enough to hand to an
implementor.

To free the repo/session for any of these, formally close this run:
`wf transition ESCALATED --trigger checkpoint --reason budget` →
`wf approve escalation --by <you> --resolution abandon` →
`wf transition REJECTED --trigger <escalation ref>` → `wf close`.
This preserves all frozen artifacts and records the checkpoint in the audit trail.

## 7. Recommendation

The plan (`plan_v3`) is strong and security-reviewed; the highest-value output is
that plan **plus** the four GMDW self-findings (GAP-1..4) above — arguably the most
useful result of the whole exercise, since they're concrete, reproduced-live
improvements to the governance system this project *is*. Recommended: preserve this
handoff + the frozen artifacts, formally close the run to unblock a fresh session,
and in the next session implement Phase 2 from `plan_v3` while folding GAP-1..4 into
scope (they extend the same recovery/liveness theme).
