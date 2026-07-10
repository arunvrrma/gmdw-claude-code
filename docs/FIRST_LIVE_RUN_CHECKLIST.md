# GMDW — First Live Run Checklist

A step-by-step procedure for executing the first real workflow inside Claude Code.
Assumes the implementation is complete (23/23 offline). Follow in order; do not skip
the pre-flight. Keep a run log capturing Q1–Q5 answers and any accepted-mode deviations.

---

## 0. Repository preparation

- [ ] Open Claude Code with the working directory at the repo root
      (`gmdw-claude-code/`) so `$CLAUDE_PROJECT_DIR` resolves.
- [ ] `python3 --version` ≥ 3.9; `git --version` present.
- [ ] `chmod +x .workflow/bin/wf` (once).
- [ ] Confirm no workflow is active: `ls .workflow/ACTIVE` → not found; `cat
      .workflow/index.json` → `{}` (or only closed workflows).
- [ ] Create a small **target sandbox** to act on: `sandbox/` with the files the smoke
      prompts reference; `git add -A && git commit -m "seed sandbox"` so HEAD is clean
      (the Implementor's `base_commit` ancestry check needs this).
- [ ] Confirm the active profile: `cat .workflow/config.json` → `standard` (use
      `expedited` only for the V6 budget drill).

## 1. Hook verification (pre-flight — do NOT skip)

- [ ] Hooks are reachable and stdlib-clean:
      `echo '{}' | python3 .claude/hooks/pretooluse_gate.py; echo "exit=$?"`
      → prints nothing, `exit=0` (no active workflow ⇒ allow).
- [ ] Same for `posttooluse_ledger.py` and `subagentstop_validate.py` → `exit=0`.
- [ ] Confirm `.claude/settings.json` references the three hooks under
      PreToolUse / PostToolUse / SubagentStop.
- [ ] Offline suite still green: `python3 tests/test_governance.py` → `OK` (23 tests).
- [ ] **Gate is live** smoke: with a scratch workflow active (`wf init`), attempt a
      protected write and confirm it is denied (see step 3). If a protected write
      **succeeds**, STOP — hooks are not firing (fix wiring before anything else).

## 2. Workflow initialization

- [ ] Start with **Smoke Test A** (`SMOKE_TESTS.md`): type the prompt
      `/gmdw Rename the constant DEFAULT_TIMEOUT to DEFAULT_TIMEOUT_SECONDS …`.
- [ ] Confirm `wf status` shows a new `wf_<date>_NNN`, `state: RECEIVED→CLASSIFYING`,
      `policy_profile: standard`, `.workflow/ACTIVE` now exists.
- [ ] Confirm `request.md` captured the prompt verbatim.

## 3. Validation checkpoints (during the run)

Watch these as the Orchestrator drives the workflow:

- [ ] **V1 order:** after the first subagent, `decision_log.jsonl` shows
      `invocation_requested` → `invocation_completed` → `artifact_accepted`.
- [ ] **V2 tokens:** the `invocation_completed` entry has `tokens` + `estimated`; record
      whether exact or estimated.
- [ ] **V3 header/binding:** `open_invocation.agent` matches the expected agent; a
      deliberately mismatched header is denied.
- [ ] **V4 artifacts:** each accepted artifact is frozen (`ls -l` shows no write bit;
      present in `artifacts_index.json`); versions increment, never overwrite.
- [ ] **Gate-live confirmation:** attempt `Write` to `.workflow/runs/<id>/state.json`
      or `.claude/hooks/pretooluse_gate.py` → **Deny** ("governance-protected").
- [ ] **V5 approvals:** at each gate the workflow blocks until `wf approve …`; the
      approval artifact appears under `approvals/` and is frozen.
- [ ] **V7 state path:** `state.json.history` matches Smoke Test A's documented path.
- [ ] Repeat for **Smoke Test B** (medium) and **Smoke Test C** (security/full),
      checking the conditional states appear (C) and are unskippable (Adversarial §5).

## 4. Expected outputs (end of Smoke Test A)

- [ ] `state: COMPLETED`; `.workflow/ACTIVE` removed after `wf close`.
- [ ] Artifacts present: `classification_v1`, `implementation_v1/`, `code_review_v1`,
      `approvals/deployment_approval.json`.
- [ ] `index.json` shows the workflow `COMPLETED` with `closed_at`, `tokens`, cost.
- [ ] `decision_log.jsonl` contains the audit set listed in `SMOKE_TESTS.md` (A).
- [ ] The sandbox file actually changed as requested (open it and confirm).

## 5. Adversarial spot-checks (at least 3 of 10, live)

Pick #1 (planner write), #5 (security bypass), #10 (budget) at minimum — one per
enforcement family. For each, drive to the setup state via the Orchestrator, attempt
the violation through the real tool, and confirm the deny/block + the log line
(`ADVERSARIAL_VALIDATION.md`). Any *allowed* violation is a release blocker.

## 6. Success criteria

- [ ] Smoke Tests A, B, C each reach `COMPLETED` on their documented paths.
- [ ] V1–V8 pass, or deviations are accepted-mode (R2 estimated tokens / R3 advisory
      model) and recorded in the run log.
- [ ] The 3+ adversarial spot-checks reproduce their denials live.
- [ ] Every run leaves a complete, frozen, self-consistent evidence bundle under
      `.workflow/runs/<id>/`.
- [ ] Q1–Q5 (`KNOWN_LIMITATIONS.md` §D) answered.

## 7. Rollback / recovery procedure

If a run misbehaves or must be abandoned:

- **Abandon cleanly:** `wf transition ESCALATED --reason other --detail "operator
  abort"` → `wf approve escalation --by <you> --resolution abandon` → `wf transition
  REJECTED --trigger x` → `wf close`. Work-in-progress in the sandbox stays on disk;
  nothing was pushed (deployment gate).
- **Orphaned in-flight invocation** (session died with `open_invocation` set): reopen CC
  in the repo; `wf status` to inspect. If the expected artifact landed and was accepted
  (`current.<type>` advanced), continue. If not, clear the orphan:
  `wf transition ESCALATED --reason agent_blocked --detail "orphaned invocation after
  interruption"`, then resolve (redirect to re-invoke, or abandon).
- **Integrity hold set** (tamper / false test pass): the workflow is frozen to
  `ESCALATED`/`FAILED`. Investigate the `integrity_violation` log entry; resolve via
  `wf approve escalation` (force_approve only with recorded `--overridden-findings`, or
  abandon). Never hand-edit `state.json` to clear the hold.
- **Nuclear reset (scratch runs only):** with no ACTIVE workflow you may `rm -rf
  .workflow/runs/<id>` and reset `index.json`; NEVER do this to a workflow that touched
  real (non-sandbox) code without archiving its evidence bundle first.
- **Git is the outer safety net:** the deployment gate guarantees nothing reached a
  remote without explicit approval; local sandbox changes are reversible with
  `git checkout -- sandbox/` or `git reset --hard <seed-commit>`.

## 8. After the run

- [ ] Archive each `.workflow/runs/<id>/` evidence bundle (commit it, or copy it out).
- [ ] Record Q1–Q5 answers and any accepted-mode deviations in the run log.
- [ ] Re-score `PRODUCTION_READINESS.md` against the release gate.
- [ ] File any runtime-wiring surprises as issues (not architecture changes) for the
      integration layer.
