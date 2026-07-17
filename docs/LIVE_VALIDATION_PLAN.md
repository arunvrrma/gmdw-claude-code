# GMDW — Live Validation Plan

Validates the framework against **real Claude Code execution**. The offline suite
proved the governance logic; this plan proves the logic is actually *wired into* the
runtime — that hooks fire, metadata arrives in the expected shape, and the state
machine advances under real subagents.

**Conventions.** Each validation is `V<n>`. Run them in order — later ones assume the
earlier plumbing is confirmed. "Observe" = read `decision_log.jsonl`, `state.json`,
and hook stderr (Claude Code surfaces hook output). Keep a scratch log of Q1–Q5
answers from `KNOWN_LIMITATIONS.md` as you go.

Pre-flight (once): from the repo root, confirm hooks are reachable and stdlib runs:
`echo '{}' | python3 .claude/hooks/pretooluse_gate.py; echo "exit=$?"` should print
nothing and `exit=0` (no active workflow → allow). Confirm `.claude/settings.json`
resolves `$CLAUDE_PROJECT_DIR` (start Claude Code in the repo root).

---

## V1 — Hook execution order & reachability

- **Purpose:** confirm all three hooks actually fire during a real subagent turn, and
  determine the PostToolUse↔SubagentStop order (Q1, R1, R4).
- **Steps:**
  1. Start a trivial workflow (Smoke Test A, `SMOKE_TESTS.md`) up to the first agent
     invocation.
  2. After the subagent returns, read `decision_log.jsonl` and note the event
     sequence around the invocation: `invocation_requested` (PreToolUse) →
     `invocation_completed` (PostToolUse) → `artifact_accepted`/`artifact_*`
     (SubagentStop).
- **Expected result:** all three events present; `invocation_requested` first;
  `artifact_accepted` last. Order of `invocation_completed` vs the SubagentStop event
  may vary — both must appear.
- **Failure modes:** missing `invocation_requested` → PreToolUse not firing (settings/
  path); missing `artifact_accepted` and no `artifact_*` failure → SubagentStop not
  firing; counters not advancing → order dependency broke (should not happen by design).
- **Success criteria:** every invocation produces exactly one requested event, one
  completion event, and one terminal artifact event (accepted or a `*_missing/invalid/
  rejected/blocked`).

## V2 — Token usage metadata

- **Purpose:** determine whether budget uses exact counts or the estimate fallback
  (Q2, R2).
- **Steps:** after V1's invocation, read the `invocation_completed` log entry; inspect
  its `tokens` and `estimated` fields. Cross-check `state.json.budget.usage_by_role`.
- **Expected result:** `estimated:false` with a plausible token count if the runtime
  supplies `usage`; `estimated:true` otherwise.
- **Failure modes:** `tokens` implausibly small (~len/4 of a short string) while
  `estimated:false` → the parser matched the wrong field; `usage_by_role` not growing
  → PostToolUse not accounting.
- **Success criteria:** budget increases monotonically per invocation; if `estimated:
  true`, record it as an accepted coarser-enforcement mode (not a failure).

## V3 — Agent invocation metadata

- **Purpose:** confirm the gate receives `subagent_type` and (if available) `model`,
  and that the `GMDW-INVOCATION` header round-trips (Q3, R3, R5).
- **Steps:**
  1. Attempt an invocation where the Orchestrator sets the header correctly → expect
     allow (`invocation_requested` logged, `open_invocation` set).
  2. Manually attempt one with a mismatched `agent:` line → expect deny with
     "requires agent '<expected>'".
- **Expected result:** correct header allowed; mismatch denied; `model` mismatch
  denied *if* the runtime passes `model`.
- **Failure modes:** correct header denied for "header missing" → prompt plumbing
  strips the header (R5); model check never triggers → runtime doesn't pass `model`
  (treat model-policy as advisory, note in run log).
- **Success criteria:** state/agent/output-path binding enforced; model binding
  enforced or explicitly recorded as advisory.

## V4 — Artifact lifecycle

- **Purpose:** confirm write-scope, versioning, freeze, and immutability behave live.
- **Steps:**
  1. During `PLANNING`, confirm the planner can write `plan_v1.json` and only that path
     (attempt a source write → deny).
  2. After acceptance, attempt to re-write `plan_v1.json` → deny (immutable); confirm
     `chmod a-w` and a registry hash in `artifacts_index.json`.
  3. On a revise cycle, confirm the next write is `plan_v2.json` (version = current+1).
- **Expected result:** exactly one writable artifact per producing state; frozen
  versions immutable; version pointer advances only via SubagentStop.
- **Failure modes:** any successful write outside the expected path; a frozen file
  re-written; `current.plan` advancing without an `artifact_accepted` event.
- **Success criteria:** the on-disk `artifacts/` tree and `artifacts_index.json` match
  the state pointers exactly after each step.

## V5 — Approval gates

- **Purpose:** confirm human gates block and record correctly (plan + deployment).
- **Steps:** run Smoke Test C to a `PLAN_APPROVAL`; confirm the workflow cannot reach
  `IMPLEMENTING` until `wf approve plan` is recorded. At the end, confirm `COMPLETED`
  is unreachable and `git push` is denied until `wf approve deployment`.
- **Expected result:** transitions past a gate fail (CLI exit 1 / gate deny) with a
  reason naming the missing approval; after `wf approve`, they succeed; an
  `approvals/*.json` file exists and is frozen; a `human_approval` log entry is written.
- **Failure modes:** a gated transition succeeds without an approval record; approval
  artifact writable by the Write tool (should be denied — only `wf approve` writes it).
- **Success criteria:** no gated state is crossed without a matching, immutable
  approval artifact and log entry.

## V6 — Budget enforcement

- **Purpose:** confirm soft-warning and hard-deny fire on real token accumulation.
- **Steps:** temporarily set a tiny budget via the `expedited` profile (or a scratch
  profile) so a couple of invocations cross 80% then 100%; run a workflow and observe.
- **Expected result:** at ≥80% a `budget_warning` event and `flags.budget_warning=true`
  (once); at ≥100% the next invocation is denied "token budget exhausted", and the only
  legal move is `ESCALATED`.
- **Failure modes:** invocations continue past 100%; no warning event; warning fires
  every invocation (should be once).
- **Success criteria:** enforcement matches the thresholds; deadline check likewise
  denies if the wall-clock deadline is set in the past for a scratch run.

## V7 — State-machine validation

- **Purpose:** confirm the live transition sequence matches the expected route for each
  smoke test, and that illegal transitions are refused.
- **Steps:** for each of Smoke Tests A/B/C, diff the observed `state.json.history`
  against the expected path in `SMOKE_TESTS.md`. Attempt one out-of-order transition
  (e.g. `CLASSIFYING → APPROVED`) via `wf` → expect exit 1.
- **Expected result:** history matches the documented route exactly (including
  conditional states appearing only when flagged); illegal transitions refused by the
  table, guard violations refused by the guard.
- **Failure modes:** a conditional state skipped when it should appear (security bypass
  — also covered adversarially); a route mis-synced from classification.
- **Success criteria:** all three routes reproduce their documented state sequences.

## V8 — Recovery after interruption

- **Purpose:** confirm a workflow resumes correctly from `state.json` after a session
  kill, and that an orphaned in-flight invocation is handled safely (R-C1).
- **Steps:**
  1. Start a workflow; interrupt (close the session / Ctrl-C) while an invocation is
     open (`open_invocation` set in `state.json`).
  2. Reopen Claude Code in the repo; run `wf status` — confirm state + `open_invocation`
     are intact.
  3. Case (a) the artifact did land: confirm SubagentStop already accepted it (pointer
     advanced) or re-run acceptance is idempotent. Case (b) it did not: the gate blocks
     a fresh invocation (single-flight); operator clears via `wf transition ESCALATED
     --reason agent_blocked` then resolves.
- **Expected result:** no state loss; no double-advance of a version pointer; a clean,
  logged path back to progress.
- **Failure modes:** `wf status` errors (corrupt/partial state — should be impossible
  with atomic writes); a second invocation allowed while one is open; a version pointer
  advanced twice for one artifact.
- **Success criteria:** the workflow either continues or lands in `ESCALATED` with a
  full audit trail; nothing silently duplicates or drops.

---

## Exit criteria for "live-validated"

All of V1–V8 pass, **or** any deviation is one of the accepted coarser modes named in
`KNOWN_LIMITATIONS.md` (estimated tokens R2, advisory model policy R3) and is recorded
in the run log with its rationale. Q1–Q5 are answered. No governance guarantee from
`docs/history/HANDOFF.md` §3 is observed to fail.
