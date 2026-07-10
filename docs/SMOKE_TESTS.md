# GMDW — Smoke Test Workflows

Three end-to-end workflows for the live run, one per route. Each is designed to be run
against a small throwaway target repo (or a `sandbox/` subtree of this repo) so the
Implementor has real files to touch. Use the `standard` profile unless noted.

For each: the prompt to type, the expected route, the artifacts that must appear
(under `.workflow/runs/<id>/artifacts/`), the human approvals required, and the audit
entries that must be present in `decision_log.jsonl`.

> Note on model policy: the standard profile assigns `opus` to planner/reviewers and
> `sonnet` to repo-analyst/implementor. If V3 shows the runtime does not honor
> per-invocation model selection, treat the model column as advisory (see R3).

---

## Workflow A — Simple (trivial route)

- **Prompt:** `/gmdw Rename the constant DEFAULT_TIMEOUT to DEFAULT_TIMEOUT_SECONDS in sandbox/config.py and update its references`
- **Expected route:** `trivial` (score ≤ 3, no flags).
- **Expected state path:**
  `RECEIVED → CLASSIFYING → IMPLEMENTING → CODE_REVIEW → APPROVED → COMPLETED`
- **Expected artifacts:**
  - `classification_v1.json` (route `trivial`, `override_flags: []`)
  - `implementation_v1/manifest.json` (+ `diff.patch`, `test_output.txt`)
  - `code_review_v1.json` (`mode: code`, `verdict: approve`)
  - `approvals/deployment_approval.json`
- **Expected approvals:** deployment only (plan gate is `off` on trivial/standard).
- **Expected audit entries:** `workflow_created`; `transition` ×5 (to CLASSIFYING,
  IMPLEMENTING, CODE_REVIEW, APPROVED, COMPLETED); `invocation_requested` +
  `invocation_completed` ×2 (implementor, code-reviewer); `artifact_written` /
  `artifact_accepted` for the implementation and code_review; `verification`
  (tests_rerun, if the manifest declares a runnable command); `human_approval`
  (deployment); `workflow_closed`.
- **What it proves:** the shortest legal path; classification→route sync;
  code-review-before-approval even with no plan; deployment gate on the simplest task.

---

## Workflow B — Medium

- **Prompt:** `/gmdw Add structured request/response logging to sandbox/api/handlers.py using the existing logger, without changing handler behavior`
- **Expected route:** `medium` (score 4–8, no override flags).
- **Expected state path:**
  `RECEIVED → CLASSIFYING → REPOSITORY_ANALYSIS → PLANNING → IMPLEMENTING → CODE_REVIEW → COMPLETED`
  (On standard/medium the plan gate is `notify` — non-blocking — so `PLAN_REVIEW` is
  taken per the guard's medium-route allowance only if the Orchestrator routes to it;
  the documented medium path skips agent plan review and the plan gate is a
  notification. `APPROVED` is still traversed before `COMPLETED`.)
- **Expected artifacts:**
  - `classification_v1.json` (route `medium`)
  - `repo_analysis_v1.json` (test_command, conventions, relevant_modules with evidence)
  - `classification_v2.json` (re-scored post-analysis; route may confirm/adjust)
  - `plan_v1.json` (milestones, acceptance_criteria, files[])
  - `implementation_v1/manifest.json` (+ diff, test output)
  - `code_review_v1.json` (`verdict: approve`, all acceptance_criteria `pass`)
  - `approvals/deployment_approval.json`
- **Expected approvals:** deployment (blocking, always). Plan gate = notify → a
  one-paragraph summary surfaced to the human, **no** approval artifact.
- **Expected audit entries:** `workflow_created`; transitions for the full medium path;
  `invocation_requested/_completed` ×3 (repo-analyst, planner, implementor,
  code-reviewer — i.e. ×4 if plan review is skipped, agent count = analyst+planner+
  implementor+code-reviewer); `artifact_accepted` for each; `verification`;
  `human_approval` (deployment); `workflow_closed`.
- **What it proves:** repository analysis feeding a plan; the plan-as-contract scoping
  the Implementor's writes; medium-route gate policy (notify, not block); re-scoring.

---

## Workflow C — Security-sensitive (full route)

- **Prompt:** `/gmdw Add authentication middleware to sandbox/api that validates a bearer token on every /api route and rejects unauthenticated requests with 401`
- **Expected route:** `full` with `override_flags: ["security"]` (security dimension ≥ 2
  forces full route + the security review states).
- **Expected state path:**
  `RECEIVED → CLASSIFYING → REPOSITORY_ANALYSIS → PLANNING → PLAN_REVIEW →
  PLAN_SECURITY_REVIEW → PLAN_APPROVAL → IMPLEMENTING → SECURITY_REVIEW →
  CODE_REVIEW → APPROVED → COMPLETED`
- **Expected artifacts:**
  - `classification_v1.json` then `classification_v2.json` (flags include `security`)
  - `repo_analysis_v1.json`
  - `plan_v1.json` (possibly `plan_v2.json` if plan review returns `revise`)
  - `plan_review_v1.json` (`mode: plan`)
  - `security_review_plan_v1.json` (`mode: plan`, non-empty `checked[]`)
  - `approvals/plan_approval.json`
  - `implementation_v1/manifest.json` (+ diff, test output)
  - `security_review_code_v1.json` (`mode: code`, non-empty `checked[]`, `verdict: approve`)
  - `code_review_v1.json` (`verdict: approve`, all acceptance_criteria `pass`)
  - `approvals/deployment_approval.json`
- **Expected approvals:** **plan approval** (blocking on full route) AND **deployment
  approval**. If the classification's architectural dimension ≥ 2, an
  **architecture approval** may also be requested (config-routed).
- **Expected audit entries:** `workflow_created`; the full transition chain;
  `invocation_requested/_completed` for analyst, planner, plan-reviewer,
  security-reviewer (plan), implementor, security-reviewer (code), code-reviewer;
  `artifact_accepted` for each; `verification`; `human_approval` ×2 (plan, deployment);
  `workflow_closed`.
- **What it proves:** the flagged full route cannot skip `PLAN_SECURITY_REVIEW` or
  `SECURITY_REVIEW` (guard-enforced); plan-approval gate blocks before implementation;
  the code path cannot reach `APPROVED` without a matching-version security approval;
  two independent human gates.

---

## Running notes

- Prepare `sandbox/` with the referenced files before each run, and commit it, so the
  Implementor's `base_commit` ancestry check has a clean HEAD.
- After each run, archive the run directory (`.workflow/runs/<id>/`) as the evidence
  bundle; it is self-contained and diff-able.
- If any smoke test diverges from its documented state path, stop and consult
  `LIVE_VALIDATION_PLAN.md` V7 before proceeding — a route/guard deviation is
  higher-priority than completing the run.
