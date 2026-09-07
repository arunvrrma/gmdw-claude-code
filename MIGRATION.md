# Migrating from GMDW v0.2.x to v0.3.0

v0.3.0 rebuilds GMDW's **routing layer** and leaves its **enforcement layer**
standing. That split is the whole design: the governance code was the strongest
part of the system and none of it was the problem. What was wrong was what the
governance code was serving — an LLM pretending to be a state machine.

Four changes, in the order they were made. Each section says what changed, why,
and what you have to do about it.

---

## 1. The Orchestrator is a Python program, not a prompt

**Before.** `/gmdw` turned the main Claude Code session into "the Orchestrator".
It read `.claude/commands/gmdw.md` — 192 lines of procedure — and *interpreted* a
15-state machine: call `wf expected`, construct a `GMDW-INVOCATION` header,
invoke the right subagent, read the verdict, decide the next transition, repeat.

Three costs, all structural:

- **The context window grew monotonically.** The Orchestrator held every
  artifact, every hook message, and every intermediate decision for the whole
  run, and paid for all of it on every turn.
- **It stalled.** A misparsed transition produced a `wf transition` the guard
  refused, and the Orchestrator's only recourse was to try to reason its way out
  — inside the same context that had just got it wrong.
- **It failed closed against itself.** The hooks could not tell "the Orchestrator
  is confused" from "a model is misbehaving", so a routing slip landed as a
  governance denial, which is the most expensive possible way to say "try again".

**After.** `gmdw/orchestrator.py` is a deterministic program:

```
read state → look up the successor in TRANSITIONS → start one agent
  → parse its Markdown → validate → advance → repeat
```

No model is consulted about what happens next. The LLM is called in exactly one
place — `gmdw/runner.py`, which shells out to `claude -p` for the cognitive work
(analysis, planning, implementation, review) and reads a file off disk.

The existing building blocks did not move; they were promoted. `state.py`,
`policy.py`, `accept.py`, `ledger.py` and `heartbeat.py` were hook-level
enforcers being driven by a prompt. They are now the router itself.

### What you have to do

**Set `GMDW_CLAUDE_ARGS` before your first run.** This is new and it is required:
v0.2 ran agents as subagents *inside* your interactive session, which could prompt
you for tool permissions. v0.3 runs each agent as a headless `claude -p` session,
which cannot. Whatever the CLI would have asked about, it now has to be told in
advance — and GMDW deliberately does not choose for you, because the right answer
is a property of where you are running:

```bash
export GMDW_CLAUDE_ARGS="--permission-mode default"          # a workstation
export GMDW_CLAUDE_ARGS="--dangerously-skip-permissions"     # CI / unattended
```

The argument is passed through verbatim to the `claude -p` invocation in
`gmdw/runner.py`. It cannot widen what an agent is allowed to do — the PreToolUse
gate runs inside the child session either way — only what it may attempt. Leaving
it unset tends to surface as an agent that "did nothing": the stage is retried once
and then escalates, which is safe but uninformative. `GMDW_CLAUDE_BIN` points at a
different binary.

```bash
# before
/gmdw Add a rate limiter to the public API

# after — same thing, and /gmdw still works (it is now a thin launcher)
python3 -m gmdw.orchestrator run request.md --by "$USER"
python3 -m gmdw.orchestrator resume     # after recording a gate decision
python3 -m gmdw.orchestrator status
```

The package moved from `.claude/hooks/gmdw/` to a top-level `gmdw/`, because it
is now an entry point and not just a hook library. If you import it, update the
path; the hooks and `wf` already do.

### The governance question this raises

The orchestrator is the host process now, so it sits *outside* the hooks rather
than under them. Two mechanisms keep that honest, and they replace the single
heartbeat check v0.2 relied on:

1. **Static, before any spend.** `heartbeat.enforcement_installed()` verifies all
   three hooks are registered in `.claude/settings.json` and present on disk.
   `preflight()` refuses to start otherwise.
2. **Dynamic, after every invocation.** The child session must have left a
   hook-sourced event in the ledger for this leg
   (`ledger.hook_event_since_last_transition`). A child that ran without hooks
   gets its artifact **refused**, not accepted, and the workflow escalates.

The PreToolUse gate now writes one `hook_liveness` event per workflow leg
specifically to be that signal. `HOOK_SOURCED_EVENTS` changed accordingly: the
old `invocation_requested` / `invocation_completed` / `invocation_failed` events
no longer prove anything about the enforcement plane, because the orchestrator
writes them itself now.

`wf doctor` reports both readings separately — *registered* and *live* — because
they answer different questions.

---

## 2. Artifacts are structured Markdown, not JSON

**Before.** Six JSON Schema contracts. A plan was a nested object with
`milestones[].files[]`, `acceptance_criteria[].verification_method`,
`risks[].likelihood`, and so on. Producing one cost output tokens on syntax —
quoting, escaping, brace bookkeeping — before any content. And validation was
all-or-nothing: one malformed character meant "regenerate the whole artifact",
which on a large plan doubled or tripled the cost of a single bad line.

**After.** A small `---` front-matter envelope, then `## ` sections whose bodies
are bulleted lists, Markdown tables, and fenced blocks. `gmdw/parsers.py` extracts
the machine-readable parts by heading and simple regex.

| v0.2 | v0.3 | sections |
|---|---|---|
| `repo_analysis_vN.json` | `repo_analysis_vN.md` | Summary, File Tree, Dependencies, Architecture Notes |
| `classification_vN.json` | `classification_vN.md` | Complexity, Risk Level, Governance Profile |
| `plan_vN.json` | `plan_vN.md` | Objective, Files In Scope, Milestones, Acceptance Criteria, Risk Mitigations |
| `plan_review_vN.json`, `code_review_vN.json`, `security_review_*_vN.json` | `review_vN.md` | Verdict, Findings, Summary |
| `implementation_vN/manifest.json` | `implementation_vN/manifest.md` | Base Commit, Changes, Tests, Milestone Status, Deviations |
| `approvals/*.json` | **unchanged** | still JSON — see below |

JSON did not disappear; it retreated to where it is actually the right format.
Fenced ` ```json ` blocks carry small closed data (a dependency list), and the
**approval record stays JSON entirely** because a machine writes it, it is small
and fixed, and it is the artifact the APPROVED and COMPLETED guards read.

`.workflow/schemas/` therefore lost six schema files and kept two
(`approval`, `state`). `gmdw/schemas.py` still exists — the stdlib JSON-Schema
subset validator now validates approvals only.

### Why this actually saves money

The saving is not "Markdown is smaller than JSON" — it is that **validation
failures became local**. A rejection now reads:

```
missing required section '## Risk Mitigations'
milestone M2 has no `- verification:` sub-bullet
'## Files In Scope' entry 1 (src/app.py): action 'refactor' is not one of ['create', 'modify', 'delete']
```

The orchestrator feeds that text back as the entire correction (see
`_retry_or_escalate`), so a retry rewrites one section instead of regenerating a
54KB blob. `tests/test_parsers.py` asserts this property directly: every
rejection test checks that the message names the specific defect.

### What you have to do

Old runs under `.workflow/runs/` are **not migrated** and do not need to be —
they are frozen, hash-registered evidence of what happened under v0.2, and they
stay readable as they are. A workflow started under v0.2 cannot be resumed under
v0.3; finish it on the old tag or abandon it (`wf transition REJECTED`).

`wf validate` now takes both formats:

```bash
.workflow/bin/wf validate .workflow/runs/<id>/artifacts/plan_v1.md --type plan
.workflow/bin/wf validate .workflow/runs/<id>/artifacts/approvals/plan_approval_1.json --type approval
```

---

## 3. Three agents, not six

**Before.** `repo-analyst`, `planner`, `plan-reviewer`, `implementor`,
`security-reviewer`, `code-reviewer`. Every one started with a blank context
window and had to re-ingest the artifact chain behind it. On the full route with
a security flag that was **seven invocations** (the security reviewer runs twice)
and seven cold starts.

**After.**

| Agent | Absorbs | Model tier | Writes |
|---|---|---|---|
| `analyst-planner` | repo-analyst + planner + plan-reviewer | sonnet | `repo_analysis_vN.md`, `classification_vN.md`, `plan_vN.md` |
| `implementor` | implementor | **opus** | `implementation_vN/` |
| `reviewer` | security-reviewer + code-reviewer | sonnet | `review_vN.md` |

**Analyst-Planner.** A repository analysis is only ever useful as input to a
plan, so splitting them meant one agent wrote a document whose sole reader was
another agent starting cold. That is the same tokens twice for no extra
judgement. Self-review of the plan is acceptable *because it is not what the
system relies on*: the orchestrator validates the plan's structure
deterministically, and a human approves its substance at a blocking gate. The
self-review buys quality; it does not buy authority.

**Implementor** moved from sonnet to **opus**. This is the stage where cognitive
quality converts into working software, and it is the one place the rework spends
*more* rather than less.

**Reviewer.** Both old reviewers read the same diff against the same plan, and
nothing reconciled their two verdicts. One pass covering both concerns halves the
input cost and produces a single coherent verdict a person can act on. The
security checklist did not go away — it is a required `## Security Checklist`
section on any security-flagged workflow, and an empty one is still not an
approval (`accept._check_review`).

Prompt templates live in `gmdw/prompts/` (one Markdown file per role) and are
rendered by `runner.build_prompt`. `.claude/agents/` keeps three definitions,
which remain the capability boundary (no `Agent` tool).

### Policy profiles changed shape

`models` now keys on the three roles. `max_agent_invocations`, `token_budget` and
`cost_ceiling_usd` came down to match the smaller pipeline:

| standard | v0.2 | v0.3 |
|---|---|---|
| `max_agent_invocations` | 14 | 8 |
| `token_budget` | 2,000,000 | 900,000 |
| `cost_ceiling_usd` | 25 | 12 |

**A custom profile from v0.2 will fail validation** — it names six model roles and
has no `gates.final_approval`. Rewrite it against `.workflow/policies/standard.json`.

`cost_ceiling_usd` was advisory in v0.2 (recorded, never enforced). The
orchestrator now actually stops on it, alongside the token budget and the
wall-clock deadline. That is a behavior change, not a rename.

### Where the limits are enforced now

The PreToolUse gate no longer sees agent invocations at all — the orchestrator
starts agents as their own sessions, and the gate's `Agent`/`Task` branch now
simply **denies subagent spawning outright** (nothing inside a governed session
has any business doing it). So invocation counts, revision limits, review cycles,
budgets and the deadline are checked in `Orchestrator._limits_preflight`, which
runs *before* the spend rather than at the moment of it. Same decision point,
same deterministic Python, one process earlier.

---

## 4. Human approval is required, and an LLM verdict no longer is

This is the correction to the only guarantee v0.2 made that was not sound.

**Before.** `CODE_REVIEW → APPROVED` was gated on the code-reviewer's
`verdict: approve` plus passing acceptance criteria — all of it asserted by an
LLM about an implementation written by another LLM. If the reviewer rubber-
stamped a hallucinated implementation, every deterministic hook happily permitted
APPROVED. The enforcement was real; what it was enforcing was an unverified
semantic claim. That is a false sense of security, and it was the most dangerous
thing in the system precisely because everything around it was so carefully built.

**After.** Two human gates, and they are **structural, not configurable**:

```
ANALYSIS_PLANNING → PLAN_APPROVAL → IMPLEMENTING → REVIEW → FINAL_APPROVAL → APPROVED
```

There is no edge from `ANALYSIS_PLANNING` to `IMPLEMENTING`, and none from
`REVIEW` to `APPROVED`. Skipping a human is not a policy setting that could be
misconfigured — it is an edge that does not exist in `state.TRANSITIONS`.
`tests/test_governance.py::test_H1` asserts exactly that.

The Reviewer is now **advisory**. Its verdict can withhold approval; it cannot
grant it. `accept.human_approval_satisfied()` is the single home of the decision,
and `state.guard` calls it for the plan, final and deployment gates. It requires
an approval artifact that:

- exists on disk and is **frozen** in the hash registry,
- still matches its recorded sha256 (a hand-edited approval is rejected),
- carries `human_approved: true` (or `auto_approved: true`), and
- **names the current version** of what it signs — an approval of `review_v1.md`
  does not vouch for `review_v2.md` after a rework.

The orchestrator presents the full briefing at each gate: at the plan gate, the
objective, file allowlist, milestones, acceptance criteria and risks; at the final
gate, the review findings **and the actual diff**. When there is nobody at the
keyboard it prints the briefing plus the exact `wf approve …` command and exits
`2` (awaiting human) rather than hanging on a closed stdin.

The git-push gate is unchanged and remains the backstop — but `APPROVED` now
means a person signed it.

### `--auto-approve`

The explicit opt-out, deliberately narrow. It waives the plan and final gates
only when **both** conditions hold:

- the workflow classified as `trivial` (`policy.AUTO_APPROVE_ELIGIBLE_ROUTES`), and
- the pinned profile marks that gate `off` for that route.

It **never** waives the deployment gate, which is a policy floor. A `strict`
profile marks every gate blocking on every route, so `--auto-approve` cannot
apply there at all. Every outcome is logged: `auto_approve_enabled` at launch,
then `auto_approve_used` or `auto_approve_refused` per gate. The approval artifact
records `auto_approved: true` and `human_approved: false` — separate fields, so an
auditor asking "did a person look at this?" gets a truthful answer rather than one
inferred from the decision word.

`policy.validate_profile` gained a floor to match: a profile may set a human gate
`off` only on `trivial`. Attempting it on `medium` or `full` fails validation and
the workflow cannot start.

### What you have to do

Two more gates per run. If you scripted GMDW, expect exit code `2` and handle it:

```bash
python3 -m gmdw.orchestrator run request.md --by "$USER"
# exit 2 → read the briefing, then:
.workflow/bin/wf approve plan --decision approved --by "$USER"
python3 -m gmdw.orchestrator resume
```

`wf approve` gained the `final` gate and defaults `--subject` to whatever the gate
actually signs, so the common case cannot accidentally record an approval that
names nothing.

---

## What deliberately did **not** change

The enforcement layer, which is the part that was working:

- **Single-flight locking** — one invocation open at a time; `open_invocation` is
  still the lock, and `wf clear-invocation --by <human>` is still the recovery.
- **The heartbeat** — the hooks still touch it on every firing; `wf doctor` and
  `wf transition`'s mid-run liveness check still read it.
- **Cost ceilings and token budgets** — preserved and now actually enforced.
- **`decision_log.jsonl`** — same append-only JSONL, same event shape. New event
  names were added (`hook_liveness`, `gate_denied`, `awaiting_human`,
  `auto_approve_*`); none were repurposed.
- **`artifacts_index.json`** — same sha256 registry, same `frozen` semantics,
  same tamper check at `APPROVED → COMPLETED` and at `wf close`. Markdown
  artifacts and approval JSON are hashed and frozen identically.
- **Artifact immutability** — accepted artifacts are `chmod a-w` plus a frozen
  registry entry; corrections are new versions.
- **Write-scoping** — the Implementor is confined to the plan's
  `## Files In Scope` plus `tests/`, `test/`, `docs/`.
- **Test re-verification** — the acceptance pipeline still re-runs the declared
  test command and compares it to the declared result. A false `pass` still sets
  an integrity hold and freezes the workflow.
- **Fail-closed hooks** — a PreToolUse error still denies; a SubagentStop error
  still sets an integrity hold.
- **Human recovery** — `wf accept` and `wf clear-invocation` still exist, still
  require `--by <human>`, and are still gate-denied while the hooks are live.
- **The three hooks themselves** — all preserved and still wired. They fire inside
  each agent session. `subagentstop_validate.py` is kept exact even though the
  normal path no longer produces subagents, so that a configuration permitting one
  cannot thereby escape acceptance.

### Two things got *stricter*

Worth calling out, because neither is a rename:

1. **The trivial route is now plan-scoped.** v0.2 skipped analysis and planning on
   the trivial route, so the write gate permitted any non-protected path. The
   3-agent model always produces a plan, so the allowlist always applies
   (`test_03b_trivial_route_is_also_plan_scoped`).
2. **`gmdw/` is governance-protected.** The orchestrator and runtime library are
   now in `_PROTECTED_PREFIXES` alongside `.claude/hooks/`. A workflow running on
   this repo cannot rewrite its own router.

---

## Upgrade checklist

```bash
# 1. finish or abandon any in-flight v0.2 workflow BEFORE upgrading
.workflow/bin/wf status
.workflow/bin/wf transition REJECTED --trigger "pre-v0.3 upgrade"   # if abandoning
.workflow/bin/wf close

# 2. rewrite any custom policy profile against .workflow/policies/standard.json
#    (three model roles; gates.final_approval; 'off' only on trivial)
python3 -c "import sys; sys.path.insert(0,'.'); from gmdw import policy; \
  p,_ = policy.load_profile('.', 'myprofile'); print(policy.validate_profile(p) or 'OK')"

# 3. confirm the enforcement plane is wired up
.workflow/bin/wf doctor

# 4. run the suites
python3 tests/test_parsers.py
python3 tests/test_governance.py
python3 tests/test_orchestrator.py

# 5. drive a run
python3 -m gmdw.orchestrator run request.md --by "$USER"
```

## On the token claim

The rework is expected to cost less for equivalent work, and the reasons are
structural: **three cold starts instead of six or seven**, **no LLM Orchestrator
context accumulating across the whole run**, **no re-ingestion of the analysis by
a separate planner**, and **local corrections instead of whole-artifact
regeneration** on a validation failure.

### First v0.3.0 run — 2026-08-28

`wf_20260828_001`, standard profile, `trivial` route. Request: add a `--dry-run`
flag to a small Python CLI (three files: source, tests, README). Completed through
both human gates with no revisions and no retries. Usage figures are exact — read
from `claude -p --output-format json`, not estimated.

| | v0.2.0 baseline (`wf_20260714_001`) | v0.3.0 (`wf_20260828_001`) |
|---|---|---|
| tokens | 1,232,887 | 1,110,759 |
| cost | $10.4959 | $1.8384 |
| agent invocations | 12 | 3 |
| outcome | abandoned | completed |

Per stage in the v0.3.0 run:

| stage | agent | model | tokens | cost |
|---|---|---|---|---|
| ANALYSIS_PLANNING | analyst-planner | sonnet | 413,246 | $0.5728 |
| IMPLEMENTING | implementor | opus | 438,697 | $0.7499 |
| REVIEW | reviewer | sonnet | 258,816 | $0.5157 |

The two runs are not the same task on the same repository, so this is one data
point rather than a controlled comparison.

### What the run changed about the budgets

Two things the first run made plain, both now reflected in the profiles and in
`docs/KNOWN_LIMITATIONS.md`:

1. **`tokens_consumed_estimate` counts cached reads at full weight.**
   `runner._extract_usage` sums `cache_read_input_tokens` alongside fresh input
   and output, so the number tracks how much context was processed, not what was
   billed — which is why 1.11M tokens cost $1.84. The cost ceiling is the
   meaningful spend control; the token budget bounds volume.
2. **The budget is checked before each invocation, never during one.** This run
   stood at 851k after the Implementor — under the then-900k budget, so the
   Reviewer was allowed to start — and finished at 1.11M. The ceiling is soft by
   one full invocation, by design (`_limits_preflight` refuses to *start* a stage
   it cannot afford), but it means the configured number is a floor on the
   overshoot, not a cap.

The standard `token_budget` was therefore raised from 900,000 to **2,500,000**,
strict to 5,000,000 and expedited to 1,200,000 — sized so the budget binds a stage
or two before `max_agent_invocations` does, rather than after. The v0.2 numbers in
the table above (14 invocations, 2,000,000 tokens on standard) were never
calibrated against a completed run; these are.
