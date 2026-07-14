# ADR-001: Artifact acceptance authority and governance-hook liveness

- **Status:** Proposed
- **Date:** 2026-07-13
- **Deciders:** (pending — repo owner)
- **Incident reference:** `wf_20260713_003` (REJECTED 2026-07-13T21:00:46Z, resolution `abandon`)
- **Audience:** implementation-planning agent. This document is the source of truth
  for the fix; it deliberately specifies *what and why*, not line-level diffs.

---

## 1. Problem statement

A GMDW workflow launched from a Claude Code session rooted **outside**
`gmdw-claude-code` (an external-workspace session with this repo reachable on disk)
advanced `RECEIVED → CLASSIFYING → REPOSITORY_ANALYSIS`, produced a **schema-valid**
`repo_analysis_v1.json`, and then deadlocked: `current.repo_analysis` stayed `0`,
the guard rejected `REPOSITORY_ANALYSIS → PLANNING` ("repo_analysis artifact
required"), and the run had to be escalated and abandoned.

More generally: **a valid agent-produced artifact can exist on disk while workflow
state is permanently unable to acknowledge it**, and — worse — **the entire
enforcement plane (invocation gating, write scoping, budget, push protection) can be
silently absent** while the workflow appears to be running normally.

## 2. Incident evidence (verified 2026-07-13)

From `.workflow/runs/wf_20260713_003/` (`state.json`, `decision_log.jsonl`):

| Observation | Meaning |
|---|---|
| No `invocation_requested` event; `counters.agent_invocations == 0` despite the repo-analyst having run | PreToolUse gate never fired |
| No `invocation_completed`/`invocation_failed` events; `open_invocation`/`last_invocation` never set | PostToolUse ledger never fired |
| No `artifact_accepted` for `repo_analysis`; `current.repo_analysis == 0` while `artifacts/repo_analysis_v1.json` exists and passes `schemas.validate_artifact` today (re-verified) | SubagentStop validator never fired |
| `classification` v1 **and** v2 *were* accepted (`actor: orchestrator` ledger events) | The CLI-side acceptance path (`state.transition → _accept_orchestrator_artifacts`, `.claude/hooks/gmdw/state.py:465`) works without hooks — this is why the run could advance to REPOSITORY_ANALYSIS at all |
| `transition_denied` at 20:50:30: "guard rejected REPOSITORY_ANALYSIS -> PLANNING: repo_analysis artifact required" | The guard (`state.py:277`) correctly failed closed |
| `ESCALATED → REJECTED → workflow_closed`, ACTIVE cleared | The escalation/abandon recovery path worked; the run's work was lost |

**Why the hooks were absent.** Claude Code loads hooks from the settings of the
project the session was started in. GMDW's three hooks are registered only in this
repo's `.claude/settings.json`, via commands that expand
`$CLAUDE_PROJECT_DIR/.claude/hooks/…`. In a session rooted at external-workspace:
(a) this repo's settings are never loaded (additional working directories grant
file access, not hook registration), and (b) even a globally-registered copy of the
hook entries would resolve `$CLAUDE_PROJECT_DIR` to the wrong root. Meanwhile the
`wf` CLI and `state.py` locate the repo by walking up from the Bash `cwd`
(`find_root`, `state.py:105`), so they work from anywhere. The two planes divorced
silently.

## 3. Root-cause analysis

GMDW has two planes that are supposed to be inseparable:

1. **State plane** — `state.py` + `wf` CLI: state machine, transition table, guards,
   ledger. **Repository-scoped**: found via `find_root()` from any cwd; works in any
   session, any environment.
2. **Enforcement plane** — the three hooks: invocation legality, write scoping, Bash
   protections, usage accounting, and **artifact acceptance**. **Session-scoped**:
   exists only when Claude Code loaded this project's `.claude/settings.json`.

Two defects combine to produce the incident:

- **RC-1 (primary): acceptance authority is misplaced.** Acceptance of subagent
  artifacts — validate → semantic checks → counters → `current` pointer → freeze —
  lives *exclusively* in `subagentstop_validate.py`. It is pure, deterministic,
  repository-scoped logic (everything it needs is on disk), yet it is owned by the
  session-scoped plane. The one asymmetric exception proves the point:
  orchestrator-written `classification` artifacts are accepted by the state plane
  (`_accept_orchestrator_artifacts`) and therefore worked in the incident.
- **RC-2 (enabling): no liveness detection.** Nothing verifies the enforcement
  plane is actually present. `wf init` happily creates a workflow in a session
  where no hook can ever fire. The system cannot distinguish "governed session"
  from "ungoverned session"; the difference only surfaces later as a confusing
  guard rejection.

**Classification of the bug** (question 2): a combination of *missing hook
detection* (RC-2) and *incorrect ownership of artifact acceptance* (RC-1). It is
**not** a state-modeling bug — the guard's refusal to advance without an accepted
artifact was correct fail-closed behavior, and the escalation exit worked. The
deadlock was the symptom, not the defect.

**Aggravating finding (security-relevant, worse than the deadlock):** in a hookless
session the deadlock is not guaranteed. On the trivial route,
`CLASSIFYING → IMPLEMENTING` requires only the orchestrator-written classification,
which the CLI itself accepts. A hookless session can therefore legally reach
IMPLEMENTING with **zero enforcement**: no GMDW-INVOCATION header checks, no model
pinning, no budget/counters, no write-scope gate, no protected-path denial, no
`git push` gating. The stall at the next hook-owned acceptance happens only *after*
the ungoverned source writes. This makes "refuse to run without hooks" a
governance requirement, not merely a UX improvement.

## 4. Verification of prior findings

All six prior claims were re-verified rather than assumed:

| Claim | Verdict |
|---|---|
| 38 files across 4 tracked run folders (not 7) | **Confirmed.** `git ls-files .workflow/runs/`: wf_20260711_001 (5) + wf_20260711_002 (7) + wf_20260711_003 (9) + wf_20260712_001 (17) = 38. Four newer run dirs are untracked. |
| Runtime is independent of tracked run folders | **Confirmed.** All runtime reads/writes go through the filesystem (`load_state`, `active_id`, artifact loads); git tracking is invisible to it. |
| `state.py` enumerates runs from disk | **Confirmed.** `create_workflow` derives the next `wf_<date>_<seq>` by counting same-day directories in `.workflow/runs/` (`state.py:545`). Note: deleting/pruning run dirs can therefore recycle IDs — relevant to any "gitignore live runs" cleanup (which is exactly what wf_20260713_003 was attempting). |
| ACTIVE state maintained outside `.workflow/runs/` | **Confirmed.** `.workflow/ACTIVE` at the `.workflow/` top level (`state.py:117`, cleared by `wf close`). Currently absent (no active workflow). |
| Doc drift between GIT_STRATEGY.md / LIVE_VALIDATION_EXECUTION.md / LIVE_VALIDATION_READINESS.md | **Confirmed, with a correction:** all three live at the **repo root**, not `docs/`. GIT_STRATEGY.md says to confirm "no runs/" is committed (line 83); the two live-validation docs instruct `git add .workflow/runs/` as the evidence-bundle procedure; `.gitignore` excludes only `*.tmp` under run artifacts; 38 run files are in fact tracked. The contradiction is real but is a documentation/policy issue **orthogonal to this ADR's defect** — track it separately. |
| tests/test_governance.py passes 26/26 | **Confirmed** (`python3 tests/test_governance.py`: "Ran 26 tests … OK"; pytest is not installed in this environment). Note: all 26 tests exercise hooks by invoking them directly — none simulates a hookless session, which is why this gap was never caught. |

## 5. Target responsibility allocation (question 3)

| Responsibility | Owner (target) | Rationale |
|---|---|---|
| State, transition table, guards | **Workflow core** (`state.py` / `wf`) | Unchanged — already correct and environment-independent |
| Artifact validation (schema + semantic cross-checks) | **Workflow core** (pure library, e.g. `gmdw/accept.py`) | Deterministic disk-only logic; must be callable from both hook and CLI |
| Artifact **acceptance** (validate → counters → pointer → freeze → ledger) | **Workflow core**, with the SubagentStop hook as its *normal trigger* | Fixes RC-1: authority in the repository-scoped plane, timing in the session-scoped plane |
| Interception/denial of illegal tool use; invocation lifecycle; usage accounting | **Hooks only** | Enforcement against a misbehaving model can *only* come from hooks — this responsibility is not relocatable, which is precisely why hook absence must abort the workflow rather than be worked around |
| Transition guards | Unchanged: precondition checks over *accepted* state | The incident proved they work |
| Hook-liveness attestation | **Hooks write it, workflow core checks it** | New responsibility introduced by this ADR (RC-2) |

Design rule that falls out: **a hook may be the sole *trigger* of a state-advancing
operation, but never the sole *implementation* of one.** `_accept_orchestrator_artifacts`
already follows this rule; SubagentStop acceptance violates it.

## 6. Alternatives considered

### A. Fail-fast only: hook-liveness preflight, no recovery path
Hooks touch a heartbeat file (e.g. `.workflow/.hooks_heartbeat`, timestamp + hook
event) on every firing, unconditionally, before any decision logic. `wf init`
refuses to create a workflow unless the heartbeat is fresh (minutes-scale window),
with an error naming the remedy ("relaunch Claude Code from the gmdw-claude-code
root"). Because PreToolUse fires on the very Bash call that runs `wf init`, a
governed session self-attests atomically; a hookless session cannot forge the
attestation *by accident* (a determined human can, which is acceptable — the threat
model is a misbehaving model plus operator error, not a malicious operator).

- ✅ Smallest change; preserves single acceptance authority; closes RC-2 and the
  trivial-route enforcement bypass at the entry point.
- ❌ No remedy for an artifact stranded mid-run (environment switch after init, or
  future hook regressions): the only exits remain escalate/abandon, losing the run.
- ❌ Alone, it leaves RC-1 in place — acceptance logic still lives only in a hook.

### B. Lazy acceptance at transition time (extend `_accept_orchestrator_artifacts` to all artifact types)
`wf transition` would accept any on-disk next-version artifact of any type, running
the same checks SubagentStop runs today, making the workflow fully hook-independent
for liveness.

- ✅ Deadlock becomes impossible; conceptually uniform with classification handling.
- ❌ **Rejected.** It makes the entire workflow *operational* without the
  enforcement plane, converting §3's aggravating finding from a partial bypass into
  the sanctioned mode of operation: ungoverned invocations, unscoped writes, no
  budgets — with state advancing normally and nothing recording that governance was
  absent. It also erodes invocation-coupled semantics (open_invocation lifecycle,
  consecutive_failures, retry-once) that give acceptance its meaning. This inverts
  the repo's founding rule ("governance is enforced by code, never by prompts"):
  outside the project, the orchestrator would be enforced by nothing at all.

### C. Unrestricted `wf accept` CLI command
A CLI command that accepts a named artifact on demand, callable by the orchestrator.

- ✅ Unblocks stranded artifacts.
- ❌ **Rejected as unconditional.** It creates a second, orchestrator-invocable
  acceptance authority: in a governed session the model could race/duplicate
  SubagentStop; in an ungoverned session it re-creates alternative B one Bash call
  at a time. Acceptable only in the constrained form of alternative E.

### D. Operational fixes only (register hooks in `~/.claude/settings.json` with absolute paths; "always launch from the repo")
- ✅ Zero code change.
- ❌ **Rejected as primary fix.** Environment configuration is exactly the class of
  control GMDW exists to avoid: not code-enforced, not versioned with the repo, not
  testable by the governance suite, silently wrong on the next machine. Worth
  *documenting* as guidance; never load-bearing.

### E. **Recommended:** re-layer acceptance into core + liveness attestation + human-gated recovery
Combines A with the RC-1 fix and a constrained recovery path:

1. **Extract acceptance into workflow core.** Move the accept pipeline
   (existence → schema → semantic checks → counters → pointer → freeze → ledger)
   from `subagentstop_validate.py` into a core module (e.g. `gmdw/accept.py`).
   SubagentStop becomes a thin trigger: parse the event, resolve the expected
   invocation, call core, translate the result to block/ok. Behavior in governed
   sessions is unchanged by construction.
2. **Liveness attestation + fail-fast** (alternative A's mechanism), enforced at
   `wf init` **and** re-checked by `wf transition` while a workflow is ACTIVE, so a
   mid-run environment switch fails at the CLI with a diagnostic naming the real
   problem ("governance hooks are not firing in this session") instead of a
   misleading guard message.
3. **`wf accept` as a human recovery gate, not an orchestrator tool.** Modeled on
   `wf approve`: requires `--by <human>`, accepts only the exact artifact
   `wf expected` names for the current state, runs the *identical* core acceptance
   pipeline (including semantic checks and, where applicable, test re-run), and
   writes a distinct ledger event (`artifact_accepted`, `actor: human_recovery`) so
   audits can always distinguish hook-accepted from human-recovered artifacts. The
   human attestation is the compensating control for the checks that genuinely
   cannot be reconstructed after the fact (that the producing invocation was
   header-checked, model-pinned, and budgeted).

## 7. Decision (recommended approach)

**Adopt E.** Sequence it as two phases so the smallest safe fix ships first:

- **Phase 1 — smallest safe fix (question 5):** liveness heartbeat written by all
  three hooks + fail-fast check in `wf init`. This alone would have prevented the
  incident (init would have refused at 20:41 with an actionable message) and closes
  the trivial-route enforcement bypass at the entry point. No behavior change in
  governed sessions.
- **Phase 2 — long-term fix (question 6):** acceptance extraction into core,
  liveness re-check in `wf transition`, and the human-gated `wf accept` recovery
  command. Add governance tests for the hookless scenario (init refusal; transition
  refusal mid-run; `wf accept` accepting a valid stranded artifact and rejecting an
  invalid/wrong-version one; `wf accept` requiring human attribution).

### Rationale

- The deadlock was **correct** fail-closed behavior; the fix must therefore target
  the causes (misplaced authority, missing detection), not soften the guard.
- Hooks provide two separable things: *acceptance mechanics* (relocatable pure
  logic) and *enforcement against a misbehaving model* (not relocatable, ever). E
  relocates the first and treats absence of the second as a fatal precondition —
  the only allocation consistent with "governance enforced by code, never by
  prompts."
- The one acceptance path that already lived in core (`classification`) was the
  one that survived the incident. E generalizes the pattern that demonstrably
  worked.
- The recovery path is shaped like the system's existing human gates (`wf approve`),
  so it adds no new trust model — humans are already the escalation authority.

## 8. Expected implementation impact

| Area | Change | Size |
|---|---|---|
| `.claude/hooks/gmdw/` (new `accept.py` or equivalent) | Extract accept pipeline + semantic checks from `subagentstop_validate.py`; add heartbeat write/read helpers | Moderate (mostly code motion; `_semantic`, `_subject_matches` move to core) |
| `.claude/hooks/subagentstop_validate.py` | Becomes a thin adapter over core accept | Shrinks substantially |
| `.claude/hooks/pretooluse_gate.py`, `posttooluse_ledger.py` | Add unconditional heartbeat touch at entry (before any decision, even when no workflow is ACTIVE) | Small |
| `.workflow/bin/wf` | `init`/`transition` liveness check; new `accept` subcommand (mirrors `approve`'s argument/audit shape) | Small–moderate |
| `tests/test_governance.py` | New hookless-session tests; existing 26 tests must keep passing — direct-invocation tests will need either a seeded heartbeat fixture or an explicit test-only override (e.g. env var); the override must itself be ledger-logged if used while a workflow is ACTIVE | Moderate |
| `.claude/commands/gmdw.md` | §0: note that `wf init` verifies governance liveness and what the refusal means | Trivial |
| `docs/KNOWN_LIMITATIONS.md` | Remove/annotate the out-of-project limitation once fixed | Trivial |

No schema changes. No changes to the transition table or guards. No migration of
existing run directories (closed runs are immutable history).

## 9. Risks and migration considerations

- **Heartbeat freshness window.** Too short → false refusals in slow sessions; too
  long → a stale heartbeat from an earlier governed session could vouch for a later
  hookless one on the same machine. Minutes-scale (5–10 min) is appropriate; the
  init-time case is self-refreshing (PreToolUse fires on the `wf init` Bash call
  itself). The `transition`-time re-check should tolerate the longest plausible
  subagent runtime or key off "any hook event since this workflow's last
  transition" (ledger-derived) rather than wall-clock alone — leave the exact
  predicate to implementation planning, but it must be deterministic and disk-only.
- **Non-Claude operation** (a human running `wf` from a plain terminal, CI running
  tests): legitimate, hookless by nature. The escape hatch must be explicit
  (flag/env), attributed, and ledger-visible when a workflow is ACTIVE — never the
  default.
- **`wf accept` abuse in governed sessions.** A misbehaving orchestrator could call
  it via Bash. Mitigations: identical validation to the hook path (an invalid
  artifact is rejected either way), distinct audit actor, and optionally a
  PreToolUse Bash rule denying `wf accept` while the heartbeat shows hooks live.
  Residual risk is a *valid* artifact accepted without invocation-lifecycle checks
  — bounded, auditable, and gated on human attribution.
- **Code-motion regression risk in Phase 2.** The accept pipeline is the most
  safety-critical code in the repo; extraction must be behavior-preserving and is
  the reason Phase 2 should land as pure refactor + new tests before `wf accept`
  is added on top.
- **Heartbeat file location.** It is runtime state, not history: it must be
  gitignored, excluded from the tamper registry, and added to the PreToolUse
  protected-path list only if writes to it by tools should be denied (recommended:
  yes — only hooks write it; note `_PROTECTED_EXACT` in `pretooluse_gate.py:50`).
- **Out of scope but adjacent** (file separately, do not bundle):
  (a) GIT_STRATEGY vs LIVE_VALIDATION_* drift on committing run folders;
  (b) run-ID recycling if run directories are pruned (`create_workflow` counts
  disk entries); (c) the trivial-route human-gate question independent of hook
  liveness.

---

*Appendix — verification commands used (2026-07-13): `git ls-files .workflow/runs/`
(38 files, 4 run dirs); `python3 tests/test_governance.py` (26/26 OK);
`schemas.validate_artifact` on the stranded `repo_analysis_v1.json` (no errors);
full read of `decision_log.jsonl` and `state.json` for `wf_20260713_003`;
code reads of `pretooluse_gate.py`, `posttooluse_ledger.py`,
`subagentstop_validate.py`, `gmdw/state.py`, `.workflow/bin/wf`,
`.claude/settings.json`, `.claude/commands/gmdw.md`.*
