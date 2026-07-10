# GMDW — Pre-Flight Validation Report

**Phase:** Live validation, pre-flight only (per `docs/FIRST_LIVE_RUN_CHECKLIST.md §1`).
**Date (UTC):** 2026-07-10
**Scope:** governance-control verification against the shipped hook scripts and `wf`
CLI. **Workflow A was NOT executed.** No implementation code modified. No remediation
performed.
**Recommendation: ✅ PASS — cleared to proceed to Workflow A when authorized.**

## Method

Two complementary, non-destructive techniques:

1. **Direct executable-contract checks** against the real repo (read-only): pipe the
   stdin JSON Claude Code sends into each hook and observe stdout/exit code.
2. **Active-workflow behavioral checks** against a **verbatim throwaway copy** of the
   published repo (the same isolation the governance suite uses), driving the shipped
   hooks and CLI with an active scratch workflow. The real working tree was never
   mutated; the tempdir was removed on completion.

Post-run confirmation: real repo working tree clean, no `.workflow/ACTIVE`, `runs/`
empty, `index.json` intact, no stray temp dirs. **Non-destructive: confirmed.**

> Scope note: pre-flight validates the hook/CLI **executable contract and governance
> logic**. Whether the Claude Code *runtime* actually intercepts tool calls and
> populates invocation/token metadata is validated during Workflow A (V1–V3); those
> items are listed under "Runtime metadata" below as deferred-by-design.

## Checks performed & evidence

### Objective 1 — Hooks are firing

**1a (contract).** Each hook given `{}` (no active workflow):
```
hook=pretooluse_gate       exit=0 stdout=''
hook=posttooluse_ledger    exit=0 stdout=''
hook=subagentstop_validate exit=0 stdout=''
```
All three load, import the `gmdw` runtime library, resolve the repo root, honor the
"no active workflow ⇒ allow/no-op" contract, and exit cleanly.

**1b (processing a governed input).** With an active scratch workflow in
`REPOSITORY_ANALYSIS`, a **legal** agent invocation (correct header, agent, model):
```
decision=None (allow)   open_invocation set=True   agent_invocations=1
```
The gate processed the input, mutated `state.json` (opened the invocation, incremented
the counter), and **allowed a legal action** — proving it is actively evaluating, not
blindly denying.

**Corroboration:** offline governance suite `python3 tests/test_governance.py` → **OK
(23/23)**, including positive controls (legal invocation allowed, planned write allowed,
valid artifact accepted).

### Objective 2 — Hook denials are visible

Illegal agent invocations each returned a `deny` decision with a human-readable reason
(the `permissionDecisionReason` surfaced to the model):
```
2a wrong-agent -> deny | state REPOSITORY_ANALYSIS requires agent 'repo-analyst', not 'planner'
2b no-header   -> deny | invocation prompt lacks a valid GMDW-INVOCATION header (...)
2c wrong-model -> deny | policy assigns model 'sonnet' to repo-analyst; requested 'haiku'
```
Denials are structured (`hookSpecificOutput.permissionDecision = "deny"`) and carry an
actionable reason.

### Objective 3 — Protected paths are protected

Every governance-critical and out-of-scope write was denied under an active workflow:
```
write state.json    -> DENY | '.workflow/runs/<id>/state.json' is governance-protected ...
write hook script   -> DENY | '.claude/hooks/pretooluse_gate.py' is governance-protected ...
write policy profile-> DENY | '.workflow/policies/standard.json' is governance-protected ...
write settings      -> DENY | '.claude/settings.json' is governance-protected ...
write source (wrong state) -> DENY | source writes are not permitted in state REPOSITORY_ANALYSIS ...
```
Zero writes were allowed. `state.json`, hooks, policies, and settings are unwritable
while a workflow is active; source writes are confined to the Implementor in
`IMPLEMENTING`.

### Objective 4 — Audit logging occurs

After the checks, the scratch `decision_log.jsonl` contained:
```
total audit lines: 5
event tally: {"workflow_created":1, "invocation_requested":1, "transition_denied":3}
```
Representative entries:
```
{"event":"workflow_created"}
{"event":"invocation_requested","agent":"repo-analyst"}
{"event":"transition_denied","from_state":"REPOSITORY_ANALYSIS","to_state":"APPROVED","reason":"illegal transition ... (not in transition table)"}
{"event":"transition_denied","from_state":"REPOSITORY_ANALYSIS","to_state":"IMPLEMENTING","reason":"guard rejected ... repo_analysis artifact required"}
{"event":"transition_denied","from_state":"REPOSITORY_ANALYSIS","to_state":"PLANNING","reason":"guard rejected ... repo_analysis artifact required"}
```
Workflow creation, an allowed invocation, and every denied transition are all recorded
append-only with actor/reason context.

### Objective 5 — State-machine enforcement occurs

```
5a table    REPOSITORY_ANALYSIS -> APPROVED       exit=1  illegal transition (not in transition table)
5b guard    REPOSITORY_ANALYSIS -> IMPLEMENTING   exit=1  guard rejected: repo_analysis artifact required
5c guard    REPOSITORY_ANALYSIS -> PLANNING       exit=1  guard rejected: repo_analysis artifact required
```
Two enforcement layers confirmed: the **transition table** rejects edges that don't
exist (5a), and **guard functions** reject edges whose preconditions aren't met (5b,
5c). 5c was set up expecting a legal advance but the guard correctly required the
`repo_analysis` artifact first — i.e. an additional, correct precondition enforcement,
not a failure. (Legal-advance ALLOW behavior is evidenced by Objective 1b and the
suite's positive controls.)

## Observed hook behavior (summary)

| Hook | Trigger | Behavior confirmed |
|---|---|---|
| PreToolUse (`pretooluse_gate.py`) | Agent/Write | Allows legal actions (empty stdout, exit 0); denies illegal ones with structured `permissionDecision:"deny"` + reason; mutates `state.json` (opens invocation, bumps counter) on allow |
| PostToolUse (`posttooluse_ledger.py`) | — | Loads and honors the no-op contract (exit 0); accounting path exercised by the offline suite |
| SubagentStop (`subagentstop_validate.py`) | — | Loads and honors the no-op contract; artifact-acceptance/`decision:"block"` path exercised by the offline suite (13-scenario coverage) |
| `wf` CLI | transition | Rejects illegal (table) and precondition-violating (guard) transitions with exit 1 + reason; logs `transition_denied` |

## Audit events observed

`workflow_created`, `invocation_requested`, `transition_denied` (×3) — all append-only
in `decision_log.jsonl` with reason/actor context. (Full event vocabulary — incl.
`invocation_completed`, `artifact_accepted`, `verification`, `human_approval`,
`integrity_violation` — is exercised by the offline suite and will be observed live in
Workflow A.)

## Runtime metadata discovered

**Confirmed (hook I/O contract):**
- PreToolUse consumes `tool_name`, `tool_input.{subagent_type, model, prompt,
  file_path, command}`, `cwd`.
- Deny shape: `{"hookSpecificOutput":{"hookEventName":"PreToolUse",
  "permissionDecision":"deny","permissionDecisionReason":"[GMDW] ..."}}`; allow = empty
  stdout + exit 0.
- SubagentStop block shape: `{"decision":"block","reason":"[GMDW] ..."}`; PostToolUse
  records silently (exit 0).

**Deferred to Workflow A by design (not observable via direct invocation):**
- **Q1/V1** — actual hook firing order (PostToolUse vs SubagentStop) under the live runtime.
- **Q2/V2** — the `tool_response` token-usage field shape (drives exact vs. estimated budget).
- **Q3/V3** — whether the runtime populates `subagent_type` and `model` in `tool_input`
  for real `Agent` calls (drives whether model-policy enforcement is active or advisory).

These are the first-run discovery items in `docs/LIVE_VALIDATION_PLAN.md`; pre-flight
cannot resolve them and does not claim to.

## Governance control failures

**None.** Every governance control behaved correctly. No control was observed to allow
a violation. (No `VALIDATION_FAILURE_REPORT.md` generated — not warranted.)

## Recommendation

**PASS.** All five pre-flight objectives are satisfied; the hook/CLI governance layer is
functioning as designed and the real repository is untouched. Cleared to proceed to
**Workflow A** (`LIVE_VALIDATION_READINESS.md §8`) when explicitly authorized — that run
will additionally resolve the deferred runtime-metadata items Q1–Q3.
