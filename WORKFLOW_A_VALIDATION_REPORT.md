# GMDW — Workflow A Validation Report

**Phase:** Live validation, Workflow A (trivial route).
**Date (UTC):** 2026-07-10
**Recommendation: ⛔ BLOCKED — NOT EXECUTED IN THIS SESSION (INCONCLUSIVE for Q1–Q3).**
**No governance violation occurred** (so no `WORKFLOW_A_FAILURE_REPORT.md`): the run
could not *begin* under the live runtime, which is an environment/authorization blocker,
not a control failure.

This report exists to record the blocker honestly and to hand over the exact operator
runbook that will resolve Q1–Q3. No runtime observations are fabricated: the items
Workflow A is meant to answer are, by definition, only observable when the real Claude
Code runtime intercepts real tool calls — which did not occur here.

## Why Workflow A could not be executed here

Workflow A's objective is to observe the **live Claude Code runtime**: whether it fires
the GMDW hooks and what token/model metadata it surfaces during **real Agent execution**.
Three conditions required for that were not met in the validating session:

1. **Project mismatch.** Claude Code loads hooks from the *project's*
   `.claude/settings.json` at session start. The validating session's project was not
   `gmdw-claude-code`, so the GMDW PreToolUse/PostToolUse/SubagentStop hooks were not
   registered and did not intercept tool calls. (Corroborating observation: many files
   were written into the repository via the Write tool during release/validation prep,
   and none were intercepted by the GMDW gate — consistent with the hooks not being
   loaded in that session.)
2. **Subagents not registered.** The six GMDW agents exist only as `.claude/agents/*.md`
   files; they were not invokable `subagent_type`s in the session. With no real
   `Agent`/`Task` invocation of `repo-analyst` / `implementor` / `code-reviewer`, there
   is no real `tool_response` (Q2) and no real invocation `model` field (Q3) to observe.
3. **`/gmdw` unavailable.** The Orchestrator command lives in the repo's
   `.claude/commands/gmdw.md` and was not a registered command in the session, so the
   documented entry point could not be started.

Driving the hooks via `subprocess` (the pre-flight technique) would re-prove the
governance *logic* but cannot answer Q1–Q3 — those are properties of the live runtime,
not of the hook scripts in isolation. Producing such output as "Workflow A results"
would misrepresent the evidence, so it was deliberately not done.

## 1. Runtime observations

None from a live GMDW run (see blocker above). The only runtime facts established remain
those from pre-flight: the hook **executable contract** (stdin fields consumed; deny /
block / allow output shapes; exit codes). The runtime **interception** behavior — the
actual subject of Q1–Q3 — is unobserved and remains open.

## 2. Hook firing sequence

Not observable in this session (hooks not registered for the session's project). Expected
sequence to confirm on a real run (per `docs/LIVE_VALIDATION_PLAN.md` V1): per agent
invocation — `invocation_requested` (PreToolUse) → `invocation_completed` (PostToolUse)
→ `artifact_accepted` (SubagentStop), with `invocation_requested` first and an artifact
terminal event last.

## 3. Metadata shapes observed

None (no real Agent execution occurred). Open items unchanged from
`docs/KNOWN_LIMITATIONS.md §D`:
- **Q2** — `tool_response` token-usage field shape (exact vs. estimated budget).
- **Q3** — presence of `subagent_type` and `model` in `tool_input` for real Agent calls
  (enforcing vs. advisory model policy).

## 4. Audit events observed

None from a live run. `decision_log.jsonl` for a real Workflow A is expected to contain
(per `docs/SMOKE_TESTS.md` A): `workflow_created`; `transition` ×5; `invocation_requested`
+ `invocation_completed` ×2 (implementor, code-reviewer); `artifact_accepted` for the
implementation and code_review; `verification` (tests_rerun); `human_approval`
(deployment); `workflow_closed`.

## 5. Governance checks exercised

None *newly* in this session. Governance controls remain PASS from the pre-flight
(`PRE_FLIGHT_VALIDATION_REPORT.md`, 5/5) and the offline suite (23/23). This report adds
no new governance evidence — it records that the live run did not start.

## 6. Deviations from expected behavior

One, and it is environmental, not a control defect: **Workflow A did not enter
`CLASSIFYING`** because it could not be started (no `/gmdw`, no registered agents, hooks
not loaded for the session's project). The documented state path
`RECEIVED → CLASSIFYING → IMPLEMENTING → CODE_REVIEW → APPROVED → COMPLETED` was not
entered. No state transition, artifact, or approval was produced.

## 7. PASS / FAIL recommendation

**INCONCLUSIVE / BLOCKED.** Not a PASS (Q1–Q3 unresolved; no live evidence) and not a
FAIL (no governance control was observed to fail — nothing ran). Workflow A must be
executed in an **interactive Claude Code session whose project is the GMDW repository**,
per the runbook below.

---

## Operator runbook — execute Workflow A to resolve Q1–Q3

Run these steps in a terminal/IDE Claude Code session **opened on the GMDW repo** so its
`.claude/settings.json` hooks and `.claude/agents/*` subagents load.

### A. Open the session correctly
```bash
cd /path/to/gmdw-claude-code
claude            # start Claude Code with THIS repo as the project (cwd)
```
Confirm the runtime picked up the GMDW config:
- `/agents` (or the session's agent list) shows `repo-analyst`, `implementor`,
  `code-reviewer`, etc.
- `/gmdw` is an available command.
- Pre-flight gate-live smoke (`docs/FIRST_LIVE_RUN_CHECKLIST.md §1`): with a scratch
  workflow active, attempt a protected write → must be denied by the live gate.

### B. Seed the Workflow A target (not yet present in the repo)
```bash
mkdir -p sandbox
cat > sandbox/config.py <<'PY'
DEFAULT_TIMEOUT = 30

def get_timeout():
    return DEFAULT_TIMEOUT
PY
mkdir -p tests
cat > tests/test_sandbox.py <<'PY'
from sandbox.config import get_timeout
def test_timeout():
    assert get_timeout() == 30
PY
git add -A && git commit -m "live-validation: seed sandbox for Workflow A"   # clean HEAD for base_commit
```

### C. Run Workflow A exactly as documented
Type into Claude Code:
```
/gmdw Rename the constant DEFAULT_TIMEOUT to DEFAULT_TIMEOUT_SECONDS in sandbox/config.py and update its references
```
Approve the **deployment** gate when prompted (`wf approve deployment`). Do not run
Workflow B or C.

### D. Capture the Q1–Q3 evidence from the run directory
```bash
RUN=.workflow/runs/$(cat .workflow/ACTIVE 2>/dev/null || ls -t .workflow/runs | head -1)
# Q1 — hook firing order:
grep -E '"event":"(invocation_requested|invocation_completed|artifact_accepted)"' "$RUN/decision_log.jsonl"
# Q2 — token metadata (exact vs estimated):
grep '"event":"invocation_completed"' "$RUN/decision_log.jsonl"   # inspect tokens + "estimated"
python3 -c "import json;print(json.load(open('$RUN/state.json'))['budget'])"
# Q3 — model metadata: whether the gate's model-policy check fired (a wrong-model
#      invocation is denied only if the runtime passes 'model'); inspect for
#      'policy assigns model' denials, and whether open_invocation recorded the agent.
# Full state path (expect the documented A route):
python3 -c "import json;print([h['to'] for h in json.load(open('$RUN/state.json'))['history']])"
```

### E. Record the findings
Populate a real `WORKFLOW_A_VALIDATION_REPORT.md` (replacing this blocked one) with the
observed hook sequence, the token/model metadata shapes, the audit events, the state
path, and the deployment-approval enforcement result — then give a PASS/FAIL.

## Blocker summary (for immediate hand-off)

- **Blocker:** Workflow A requires the GMDW hooks + subagents + `/gmdw` command to be
  live in the session; the validating session's project was not the GMDW repo, so none
  were active.
- **Exact action required:** run the runbook above in an interactive Claude Code session
  whose working directory is the GMDW repository.
- **Nothing was faked, nothing was modified:** no implementation code changed; no
  Workflow B/C; no fabricated runtime data.
