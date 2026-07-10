# GMDW — Live Validation Execution

The exact, operator-followable process for running live validation on the
`live-validation` branch. It ties together `docs/LIVE_VALIDATION_PLAN.md` (the V1–V8
checks), `docs/SMOKE_TESTS.md` (Workflows A/B/C), and `docs/ADVERSARIAL_VALIDATION.md`
(the 10 attacks). Runtime is frozen: this phase produces **evidence and doc updates
only** — no changes to code, schemas, hooks, or agents.

## Prerequisites

- `v0.1.0-mvp` tag exists and is pushed (`GIT_STRATEGY.md` complete).
- `docs/FIRST_LIVE_RUN_CHECKLIST.md` §0–§1 pre-flight passes (hooks reachable,
  `$CLAUDE_PROJECT_DIR` resolves, offline suite green).
- A run log file open to capture V1–V8 outcomes, Q1–Q5 answers, and deviations.

## 1. Create the validation branch

```bash
git checkout -b live-validation v0.1.0-mvp    # if not already created in GIT_STRATEGY §6
git status                                     # clean, on live-validation
```

All evidence for this phase is committed here, never to `mvp-complete`.

## 2. Prepare a test repository (the target the agents act on)

Live workflows need real files to change. Use an isolated `sandbox/` subtree so runs
never touch framework code, and commit it so the Implementor's `base_commit` ancestry
check has a clean HEAD.

```bash
mkdir -p sandbox/api
cat > sandbox/config.py <<'PY'
DEFAULT_TIMEOUT = 30
def timeout(): return DEFAULT_TIMEOUT
PY
cat > sandbox/api/handlers.py <<'PY'
import logging
log = logging.getLogger("api")
def handle(request):
    return {"status": 200, "body": "ok"}
PY
mkdir -p tests
cat > tests/test_sandbox.py <<'PY'
from sandbox.config import DEFAULT_TIMEOUT
def test_timeout(): assert DEFAULT_TIMEOUT == 30
PY
git add -A && git commit -m "live-validation: seed sandbox target for Workflows A/B/C"
```

Record the seed commit SHA — it is the rollback point for the sandbox between runs.

## 3. Execute Workflow A (simple / trivial route)

1. In Claude Code (cwd = repo root), run the Workflow A prompt from
   `docs/SMOKE_TESTS.md`.
2. During the run, tick the V1–V5/V7 checkpoints from
   `docs/FIRST_LIVE_RUN_CHECKLIST.md` §3 (hook order, tokens, header binding, artifact
   freeze, gate blocking, state path).
3. Approve the deployment gate when prompted (`wf approve deployment`).
4. Confirm terminal: `state: COMPLETED`, `.workflow/ACTIVE` gone, the sandbox constant
   actually renamed.
5. Archive the evidence bundle and commit it:
   ```bash
   git add .workflow/runs/ && git commit -m "live-validation: Workflow A evidence (trivial route → COMPLETED)"
   ```

## 4. Execute Workflow B (medium route)

1. Reset the sandbox to the seed commit if A modified shared files
   (`git checkout <seed-sha> -- sandbox/`), or leave A's change in place if independent.
2. Run the Workflow B prompt. Verify `REPOSITORY_ANALYSIS → PLANNING` appear and that
   the plan scopes the Implementor's writes (attempt an out-of-scope write → deny; that
   is Adversarial #3, captured here).
3. Plan gate is `notify` on medium/standard — confirm a summary surfaced, no plan
   approval artifact. Approve deployment.
4. Confirm the documented medium state path in `state.json.history`.
5. Commit the evidence bundle: `... -m "live-validation: Workflow B evidence (medium route)"`.

## 5. Execute Workflow C (security-sensitive / full route)

1. Run the Workflow C prompt. This is the strongest test: confirm
   `override_flags: ["security"]`, and that `PLAN_SECURITY_REVIEW` and `SECURITY_REVIEW`
   appear and **cannot be skipped** (attempt `wf transition CODE_REVIEW` from
   `IMPLEMENTING` on the flagged workflow → CLI-reject; that is Adversarial #5).
2. Handle **two** human gates: plan approval (blocking on full route) and deployment
   approval. Confirm each writes a frozen `approvals/*.json`.
3. Confirm the code path cannot reach `APPROVED` without a security approval for the
   current implementation version.
4. Confirm the full documented state path.
5. Commit the evidence bundle: `... -m "live-validation: Workflow C evidence (full/security route)"`.

## 6. Adversarial spot-checks (live)

Reproduce at least #1 (planner write), #5 (security bypass — already covered in
Workflow C), and #10 (budget) live, plus the integrity bonus if practical. For budget,
run one workflow under a scratch tiny-budget profile so an invocation crosses 100% →
deny. For each: capture the exact deny/block reason and the `decision_log.jsonl` line.
Commit as `live-validation: adversarial spot-check evidence`.

## 7. Recording results

Maintain a single `docs/LIVE_VALIDATION_RESULTS.md` on this branch (new evidence doc —
not a runtime change) containing:

- V1–V8: pass / accepted-deviation / fail, with the observed evidence per check.
- Q1–Q5 answers (`docs/KNOWN_LIMITATIONS.md` §D).
- Model-policy verdict: enforcing or advisory (from V3).
- Token accounting verdict: exact or estimated (from V2).
- A per-workflow state-path diff (observed vs `docs/SMOKE_TESTS.md`).
- Links to the committed evidence bundles.

Commit it. When complete and clean, re-score `docs/PRODUCTION_READINESS.md` against its
release gate (as a doc update on this branch).

## 8. Handling failures

- **A hook does not fire** (a violation is *allowed*, or `invocation_requested` never
  logs): STOP the phase. This is a wiring failure, not a logic failure — the release
  gate is not met. Diagnose against `LIVE_VALIDATION_PLAN.md` V1 (settings path,
  `$CLAUDE_PROJECT_DIR`, stdout conventions). Record it; do not work around it by
  editing runtime under the freeze — raise it as an integration issue for a follow-up
  release.
- **A guarded transition is wrongly allowed:** release blocker; same as above.
- **An accepted-mode deviation** (estimated tokens R2, advisory model R3): not a
  failure — record it in `LIVE_VALIDATION_RESULTS.md` with rationale and continue.
- **A workflow wedges** (integrity hold, orphaned invocation): follow the rollback
  procedure in `docs/FIRST_LIVE_RUN_CHECKLIST.md` §7. Never hand-edit `state.json`.
- **Sandbox corruption:** `git checkout <seed-sha> -- sandbox/` (or
  `git reset --hard <seed-sha>` on this branch if only the sandbox changed). The
  `v0.1.0-mvp` tag remains the pristine framework anchor.

## 9. Completion

Live validation is complete when V1–V8 pass (or deviations are recorded as
accepted-mode), the three smoke workflows reproduce their documented paths, the
adversarial spot-checks reproduce their denials, and `LIVE_VALIDATION_RESULTS.md` is
committed. Then open the `live-validation → main` merge (`--no-ff`, per
`GIT_STRATEGY.md`), summarizing the outcome in the merge commit.
