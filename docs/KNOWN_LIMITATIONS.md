# GMDW — Known Limitations & Risk Register

Honest accounting of what the MVP does **not** guarantee, the risks the design
knowingly accepts, and the questions only a live run can answer. None of these are
defects against the approved architecture; they are the boundary of the current
implementation. Nothing here should be "fixed" before the first live run — the run
exists to resolve the open items.

## A. Runtime-integration risks (must be confirmed live)

| ID | Risk | Impact if the assumption is wrong | Mitigation already in place | Detection |
|---|---|---|---|---|
| R1 | Hook firing order (PostToolUse vs SubagentStop) differs from expectation | Counters/validation could act on a stale `open_invocation` | `open_invocation → last_invocation` handoff makes validation order-independent | LIVE V1 |
| R2 | `tool_response` token fields differ from the parsed keys (`usage.input_tokens`, …) | Budget tracked by `len/4` estimate, not exact tokens | Estimate fallback flagged `estimated:true`; budget still enforced, just coarser | LIVE V2 |
| R3 | Runtime does not pass `model` (or `subagent_type`) to the PreToolUse hook | Model-policy check silently no-ops; agent/state binding weakens | Gate treats missing `model` as "unspecified" (skips that one check); state/agent/header checks still fire | LIVE V3 |
| R4 | `$CLAUDE_PROJECT_DIR` unset/incorrect for hook commands (or session project ≠ GMDW repo) | Hooks don't run → **no governance** | **Closed at entry (ADR-001 Phase 1):** the hooks write a liveness heartbeat on every firing and `wf init` refuses to start a workflow unless a fresh heartbeat proves the enforcement plane is live in this session (override: `GMDW_ALLOW_UNGOVERNED_INIT`, ledger-recorded). Mid-run hook loss is not yet caught — see Phase 2. | LIVE V1 (pre-flight); **confirmed live in `wf_20260713_001`**, now guarded — see §F (F1) |
| R5 | Orchestrator cannot inject the `GMDW-INVOCATION` header into the subagent prompt | Every invocation denied (header missing) | Deny is safe (blocks work, never allows illegally) | LIVE V3 |

## B. Enforcement scope the design deliberately accepts

1. **Caller attribution on writes is state-based, not identity-based.** Hooks cannot
   see *which* subagent issued a `Write`. Scope is derived from the current state, so
   e.g. in `IMPLEMENTING` any actor writing a planned file is allowed. This is the
   approved stance; it holds because only the Implementor runs in `IMPLEMENTING`.
2. **`trivial_max_files` is defined in profiles but not hook-enforced.** On the trivial
   route the write gate permits any non-protected path. A trivial task that balloons
   past the intended file count is not auto-stopped (code review still catches it).
3. **Bash gate is pattern-based, not a parser.** It denies mutating operations against
   protected substrings and gates push/commit/install by state. Determined obfuscation
   (unusual quoting, env indirection) could evade a pattern; the protected *files*
   remain safe because `state.json` etc. are also enforced at the Write layer and by
   file permissions. Tune patterns after live experience.
4. **Test re-verification runs the declared command in-repo with a 300s timeout.** A
   test command with side effects, or one exceeding 300s, yields `observed=timeout`
   and is treated as non-matching only when the declared result was pass/fail. Long
   suites need the profile's `rerun_tests:false` or a fast subset command.
5. **Token budget is an estimate, not billing truth.** Even with exact `tool_response`
   counts, orchestrator-side tokens are not metered — only subagent invocations are.
   The budget bounds delegated work, which is where the volume is.
6. **Single-flight, single-active-workflow.** One workflow is ACTIVE at a time and one
   invocation open at a time. Concurrent workflows are out of scope for the MVP.

## C. Recovery boundaries

1. **Crash recovery is state-file based.** If the session dies, `state.json` +
   artifacts fully describe position; re-running `/gmdw` (resume) reads them. But an
   invocation that was **in flight** at crash leaves `open_invocation` set — the next
   action is gated (single-flight) until the operator clears it via a `wf` transition
   to `ESCALATED` or confirms the artifact landed. There is no automatic
   orphan-invocation reaper in the MVP; this is a manual step (see LIVE V8).
2. **Integrity hold is terminal-ish.** Once `integrity_hold` is set (tamper or false
   test pass), only `ESCALATED`/`FAILED` are reachable. Resolution is a human decision;
   there is no automated un-hold.
3. **Stale workspace.** The validator checks the Implementor's `base_commit` is an
   ancestor of HEAD. If a human commits under a running workflow, the implementation
   is rejected and must be escalated — never auto-rebased.

## D. Open questions for the validation run

- **Q1:** Does Claude Code invoke both PostToolUse and SubagentStop for a subagent
  completion, and in which order? (Drives R1/R2 confidence.)
- **Q2:** What is the exact JSON shape of `tool_response` for an `Agent`/`Task` call?
- **Q3:** Is `model` selectable per-invocation and surfaced to the hook, or is the
  subagent's own frontmatter `model` authoritative? (If the latter, the gate's
  model-policy check should be treated as advisory, not enforcing.)
- **Q4:** Does a subagent denial (PreToolUse deny on `Agent`) surface cleanly to the
  Orchestrator so it can escalate, rather than crashing the turn?
- **Q5:** Are hook stdout/exit-code conventions (deny JSON, `decision:block`) honored
  by the installed Claude Code version?

## E. Explicitly out of scope for the MVP (not limitations — non-goals)

Parallel implementor fan-out; a workflow dashboard/UI; Postgres backend; CI-gated
finalization beyond `require_ci_green` as a flag; automated threshold re-tuning from
escalation history; multi-user approver routing beyond the config stub.

## F. Confirmed in live operation (Workflow C — `wf_20260713_001`)

Workflow C ran the **full** governed path against a *different* repository (`../external-admin`)
than the GMDW project, from a session rooted in `external-workspace`. It converted two
previously-forward-looking items into **observed** limitations. Neither is a defect against the
approved architecture; both are boundaries of the current implementation, now witnessed live.
The review controls exercised in the same run are recorded in
[`VALIDATION_HISTORY.md`](VALIDATION_HISTORY.md).

| ID | Limitation | Impact | Detection / recovery |
|---|---|---|---|
| F1 | **Cross-repo runs are governed by operator discipline, not runtime interception** (confirms R4). Because `$CLAUDE_PROJECT_DIR` did not resolve to the GMDW repo, `PreToolUse`/`SubagentStop` did not auto-fire; governance held only because the operator manually drove the same validators (`.workflow/bin/wf` + the `gmdw` hook modules) at each gate. | Enforcement degrades from runtime-enforced to operator-advisory; failure mode is **fail-open** (an omitted manual step passes ungoverned work silently). Outcome parity was achieved in `wf_20260713_001`, but by discipline, not enforcement. | **Now code-enforced at init (ADR-001 Phase 1):** `wf init` fails fast when the governance-hook heartbeat is absent/stale, converting the silent fail-open start into an explicit refusal. (The prior pre-flight `$CLAUDE_PROJECT_DIR` checklist control is now redundant at init.) Tracked: [#2](https://github.com/arunvrrma/gmdw-claude-code/issues/2) ([`issues/cross-repo-governance-hook-limitation.md`](issues/cross-repo-governance-hook-limitation.md)). |
| F3 | **The plan-approval artifact records a single, immutable decision.** `artifacts/approvals/plan_approval.json` exposes exactly one slot, frozen on write. A `changes_requested → revise → approve` cycle cannot be represented: the first decision (defer M3) froze the slot, so the follow-up `approved` on the re-planned `plan_v2` could not be written and `PLAN_APPROVAL → IMPLEMENTING` was guard-blocked ("plan approval record required"). | A routine plan-iteration pattern is forced onto the **escalation** path (its sanctioned recovery), so legitimate re-approvals surface in `escalations[]`, diluting the audit signal of genuine escalations. All three reviewers and the human had approved `plan_v2` — a tooling limitation, not a governance disagreement. | Escalation override (as used in `wf_20260713_001`). Tracked: [#3](https://github.com/arunvrrma/gmdw-claude-code/issues/3) ([`issues/plan-approval-artifact-lifecycle-limitation.md`](issues/plan-approval-artifact-lifecycle-limitation.md)). |
