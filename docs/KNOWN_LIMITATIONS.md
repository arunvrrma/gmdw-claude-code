# GMDW — Known Limitations & Risk Register

Honest accounting of what GMDW does **not** guarantee, the risks the design
knowingly accepts, and the questions only a live run can answer. None of these are
defects against the architecture; they are the boundary of the current
implementation.

Current as of **v0.3.0** (the deterministic-orchestrator rework — see
[MIGRATION.md](../MIGRATION.md)). Where v0.3.0 closed or changed a v0.2 item, that
is stated rather than quietly dropped: a risk register that only ever grows
shorter is not being kept honestly.

## A. Runtime-integration risks (confirm on a live run)

| ID | Risk | Impact if the assumption is wrong | Mitigation in place | Detection |
|---|---|---|---|---|
| ~~R1~~ | *(open until 2026-08-28)* The `claude` CLI's `--output-format json` envelope does not carry `usage` | **Answered: it does.** The first live run (`wf_20260828_001`) recorded `estimated: false` on all three invocations, with `usage` and `total_cost_usd` both present. The `len/4` fallback and its `estimated: true` marker remain for the case where a future runtime omits them | resolved |
| R2 | A headless `claude -p` session cannot answer a tool-permission prompt, so an agent's writes are denied by the *runtime* rather than by GMDW | The stage produces nothing and is retried once, then escalates — noisy and expensive, but fail-safe | `GMDW_CLAUDE_ARGS` is the operator's seam for `--permission-mode`; GMDW deliberately does not pick a posture, since the right one is environmental | SMOKE, before S1 |
| R3 | Hook `cwd` / project-root resolution differs in the child session, so the hooks do not fire there | An agent session runs ungoverned | **Closed by refusal, not by hope:** the orchestrator requires a hook-sourced ledger event from every invocation and *refuses the artifact* if there is none, escalating instead of accepting (`test_O17`) | SMOKE S1 (`hook_liveness` present) |
| R4 | `--allowed-tools` is advisory, or the child session ignores it | An agent attempts a tool its role does not need | Not a security boundary and never was — the PreToolUse gate is, and it does not consult that list | SMOKE S2 |
| ~~R5~~ | *(v0.2)* The Orchestrator cannot inject a `GMDW-INVOCATION` header | **Obsolete.** There is no header protocol: the orchestrator starts agents directly and the gate denies subagent invocation outright | — |
| ~~R6~~ | *(v0.2)* The runtime does not surface `model` to the PreToolUse hook | **Obsolete.** The model comes from the pinned profile and is passed on the child's argv; nothing asks for it, so nothing can ask wrongly | — |

## B. Enforcement scope the design deliberately accepts

1. **Caller attribution on writes is state-based, not identity-based.** Hooks
   cannot cryptographically attribute a `Write` to a particular agent. Scope is
   derived from the current state, so in `IMPLEMENTING` any actor writing a planned
   file is allowed. v0.3.0 tightened this with a second, independent signal:
   `GMDW_ROLE` is set on the child process, and a session whose role does not match
   the state's agent is denied (`test_stale_role_session_is_denied`). That catches a
   stale session; it is not identity.
2. **`trivial_max_files` is defined in profiles but not hook-enforced.** The route's
   *write* scope is now the plan's `## Files In Scope` like every other route
   (v0.2 permitted any non-protected path there), so a ballooning trivial task is
   confined — but the file *count* itself is not checked against the profile.
3. **The Bash gate is pattern-based, not a parser.** It denies mutating operations
   against protected substrings and gates push/commit/install by state. Determined
   obfuscation (unusual quoting, env indirection) could evade a pattern; the
   protected *files* remain safe because they are also enforced at the Write layer
   and by file permissions.
4. **Test re-verification runs the declared command in-repo with a 300s timeout.** A
   test command with side effects, or one exceeding 300s, yields `observed=timeout`
   and is treated as non-matching only when the declared result was pass/fail. Long
   suites need the profile's `rerun_tests: false` or a fast subset command.
5. **The token budget counts cached reads at full weight, so it is a volume
   metric and not a spend one.** `runner._extract_usage` sums
   `cache_read_input_tokens` alongside fresh input and output. The first live run
   made the gap concrete: **1,110,759 tokens for $1.84**. That is not a bug —
   those tokens really were processed — but it means the token budget bounds how
   much context the run chewed through, while **`cost_ceiling_usd` is the
   meaningful spend control**. Read the two numbers as answering different
   questions. (The orchestrator itself now costs *zero* tokens, being Python
   rather than an LLM holding a growing context, so there is no longer an
   unmetered orchestrator component.)
6. **The budget is checked before each invocation, never during one, so a run can
   overshoot it by one full stage.** `_limits_preflight` refuses to *start* a
   stage it cannot afford; nothing interrupts one already running. In the first
   live run the total stood at 851k after the Implementor — under the then-900k
   budget, so the Reviewer was permitted to start — and finished at 1.11M. This is
   deliberate (killing an agent mid-flight strands a half-written artifact), but
   the configured budget is a floor on the overshoot, not a cap. Size profiles
   accordingly: the shipped ones now bind a stage or two before
   `max_agent_invocations` does.
7. **`cost_ceiling_usd` is enforced against a reported or estimated cost.** v0.2
   recorded it and never acted on it; v0.3.0 stops the run on it. The CLI does
   report `total_cost_usd` (confirmed live), so the estimate path
   (`ledger.MODEL_RATES`, a July 2026 snapshot, advisory) is a fallback only. It
   shares the one-stage overshoot described above.
8. **Single-flight, single-active-workflow.** One workflow ACTIVE at a time, one
   invocation open at a time. Concurrent workflows remain out of scope.
9. **Structured-Markdown parsing is tolerant by design.** The parser accepts bold
   markers, inline code, `::` in place of an em dash, and a table introduced by a
   sentence, because a rejection for cosmetics is a wasted invocation. The cost is
   that a sufficiently creative layout could parse into something the author did
   not intend. The mitigations are that every parsed field is enum- or
   pattern-checked, and that a human reads the plan and the review at the gates.

## C. Recovery boundaries

1. **Crash recovery is state-file based.** The orchestrator holds no state the run
   directory does not, so `python3 -m gmdw.orchestrator resume` picks up exactly
   where it stopped, from a fresh process (`test_O9`). An invocation that was **in
   flight** at the crash leaves `open_invocation` set, and single-flight then gates
   every next action; recovery is `wf clear-invocation --by <human>`, which releases
   the lock and nothing else. There is no automatic orphan reaper.
2. **The integrity hold is terminal-ish.** Once set (tamper, or a false test pass),
   only `ESCALATED` / `FAILED` are reachable. Resolution is a human decision; there
   is no automated un-hold.
3. **Stale workspace.** The acceptance pipeline checks the Implementor's
   `base_commit` is an ancestor of HEAD. If a human commits under a running
   workflow, the implementation is rejected and must be escalated — never
   auto-rebased.
4. **A v0.2 workflow cannot be resumed under v0.3.** The state machine, the artifact
   format and the profile shape all changed. Finish an in-flight run on the old tag
   or abandon it before upgrading (see the MIGRATION.md checklist).
5. **No in-governance `wf extend-deadline`.** A run that outlives its wall-clock
   deadline, `max_agent_invocations` or `max_review_cycles` still needs a raw
   `state.json` edit. A human-attributed, audited recovery-command family for
   budgets remains a known gap — the same gap v0.2 had, unchanged.

## D. Open questions for the live runs

- ~~**Q1:** Does `claude -p --output-format json` report `usage` and
  `total_cost_usd` on every invocation?~~ **Answered 2026-08-28: yes**, on all
  three invocations of the first live run. What it revealed instead is §B5 —
  cache reads dominate the total.
- ~~**Q2:** Do the project's hooks reliably load in a `claude -p` child started
  from the repo root by a non-Claude parent process?~~ **Answered 2026-08-28:
  yes.** All three stages of the first live run left `hook_liveness` /
  `artifact_written` events, so every artifact was accepted under the dynamic
  liveness check rather than refused. One run on one machine is not a guarantee,
  but the refusal path makes a future negative safe.
- **Q3:** What permission posture does a headless agent session actually need to
  write its artifacts and run tests? (Drives R2, and the answer belongs in
  `docs/GETTING_STARTED.md` once known.)
- **Q4:** Does a stage's rejection-and-retry actually converge? The retry carries
  the exact defect, which is the design's central bet about token cost — a live run
  where the second attempt fails the same way is the falsifying case. *Still open:
  the first live run produced no rejections, so the retry path has not been
  exercised against a real model.*
- **Q5:** Are the gate briefings enough to decide on without opening the artifacts?
  The final gate truncates the diff at 200 lines; a change routinely larger than
  that needs a better presentation, not a bigger truncation limit.

## E. Explicitly out of scope (non-goals, not limitations)

Parallel implementor fan-out; a workflow dashboard or UI; a Postgres backend;
CI-gated finalization beyond `require_ci_green` as a flag; automated threshold
re-tuning from escalation history; multi-user approver routing beyond the config
stub.

## F. The v0.2 findings, and where they stand

Two limitations were confirmed in live operation under the v0.2 architecture. Both
are now closed, and it is worth saying precisely how — one by the ADR-001 work that
preceded this rework, one by the rework itself.

| ID | v0.2 finding | Status in v0.3.0 |
|---|---|---|
| F1 | **Cross-repo runs were governed by operator discipline, not runtime interception.** `$CLAUDE_PROJECT_DIR` did not resolve to the GMDW repo, so the hooks did not auto-fire; governance held only because the operator manually drove the same validators at each gate. Failure mode: **fail-open**. | **Closed, and the failure mode inverted.** The orchestrator refuses to start unless all three hooks are registered and present, and refuses to *accept* any artifact from an invocation that left no hook-sourced ledger event. An ungoverned agent session no longer produces silently-accepted work; it produces an escalation. The residual below still applies. |
| F3 | **The plan-approval artifact recorded a single, immutable decision.** `approvals/plan_approval.json` had exactly one slot, frozen on write, so a `changes_requested → revise → approve` cycle could not be represented and was forced onto the escalation path. | **Closed.** Approval artifacts are now indexed per gate (`plan_approval_1.json`, `plan_approval_2.json`, …) by `accept.approval_relpath`, so the ordinary iteration pattern is representable and no longer dilutes the escalation signal. Exercised by `test_O11`. |

### Residual on F1/R3

None of these is claimed closed:

1. **`--by` is trusted, not verified.** The recovery commands and `wf approve` take
   the human's name as a string and record it; nothing authenticates it. This is
   bounded by the gate deny, which makes all of them unreachable from inside a live
   agent session — the control is a real human at a terminal.
2. **The gate deny is a syntactic rule.** Like every `gate_bash` rule it matches
   command text, so dynamic construction could evade it. It is defense in depth over
   the acceptance core's identical validation, not the sole barrier.
3. **`guard()`'s single-flight ordering is unchanged by choice.** It still checks
   `open_invocation` before allowing `ESCALATED`. Orphaned locks are cleared by human
   recovery rather than auto-bypassed, so a live in-flight invocation can never be
   shrugged off by a transition.
4. **The test re-run executes an artifact-declared command under a shell.** This is
   the same already-accepted trust boundary the SubagentStop hook always crossed; the
   orchestrator inherits it rather than widening it.
5. **Legacy `GMDW_ALLOW_UNGOVERNED_INIT` keeps its loose truthiness.** Only the
   canonical `GMDW_ALLOW_UNGOVERNED` is affirmative-only (`1/true/yes/on`).
   `GMDW_ALLOW_UNGOVERNED_INIT=0` still enables the init bypass; hardening it would
   change shipped behavior, so it is deferred.

## G. New in v0.3.0

Limitations the rework introduced, stated plainly:

1. **The orchestrator depends on the `claude` CLI being on `PATH`.** GMDW is no
   longer a thing that runs *inside* Claude Code; it is a program that *calls* it.
   `GMDW_CLAUDE_BIN` overrides the binary; `preflight()` refuses cleanly if it is
   missing, rather than failing at the first invocation.
2. **The orchestrator is trusted code outside the hooks.** It is the host process,
   so the hooks do not gate it — the same trust class as `gmdw/state.py` and the
   `wf` CLI have always had, but applied to a component that now makes routing
   decisions. The mitigations are that it is deterministic, fully tested
   (`test_orchestrator.py`, 29 tests, no model involved), and that `gmdw/` is itself
   governance-protected so a GMDW run cannot rewrite its own router.
3. **The Analyst-Planner reviews its own plan.** The dedicated adversarial
   plan-reviewer is gone. This is a deliberate trade: structural completeness is
   checked deterministically and semantic correctness is checked by a human at a
   blocking gate, so the self-review buys quality but never authority. It does mean
   there is no longer an independent *model* opinion on the plan before a human
   sees it, and a plan that is plausible-but-wrong now reaches the human unchallenged.
   The human gate is load-bearing here in a way it was not before.
4. **The final gate truncates the diff at 200 lines.** A large change is presented
   in summary with a path to the full patch. A human who approves without opening it
   is exactly the failure mode the gate exists to prevent, and the presentation does
   not currently make that hard. See Q5. (The first live run's diff was ~140 lines
   and fitted, so this remains untested at the boundary.)
5. **Two parser defects reached production and were found by the first live run,
   not by the suite.** `nested_bullets` kept only a bullet's first line, so a
   wrapped rationale in `## Scope Out` was silently truncated in the plan briefing
   a human reads; and `_clean` stripped a trailing backtick unconditionally,
   mangling prose ending in inline code. Both are fixed with regression tests. The
   lesson generalises: the suite writes artifacts in its own house style, so the
   ways a *real* model formats Markdown are exactly the cases it does not cover.
