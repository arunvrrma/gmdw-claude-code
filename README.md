# GMDW - Governed Multi-Agent Delivery Workflow for Claude Code

A controlled software-delivery workflow: a **deterministic Python orchestrator**
drives a fixed state machine, calls Claude Code agents for the parts that are
actually cognitive, and stops for a human at the decisions that matter. Isolation,
loop prevention, budget limits, scope enforcement and artifact integrity are
enforced by code - not by asking a model to behave.

> **New here?** [**docs/GETTING_STARTED.md**](docs/GETTING_STARTED.md) is a
> step-by-step walkthrough from the user's seat - start there. The rest of this
> README is the architecture.
>
> **Coming from v0.2.x?** [**MIGRATION.md**](MIGRATION.md) explains what changed
> and why, and what you have to do about it.

## Why it exists

Free-form agent swarms are unpredictable and unauditable. GMDW replaces the swarm
with a Python loop over a transition table, three single-purpose agents that
communicate only through versioned artifacts, three hooks that make the rules
non-negotiable, and a person at both gates that matter.

## The shape of it

```
                 ┌──────────────────────────────────────────┐
   request.md ──▶│  gmdw/orchestrator.py   (deterministic)   │
                 │  read state → next stage → validate → …   │
                 └───────┬─────────────────────────┬─────────┘
                         │ one agent at a time     │ two human gates
                         ▼                         ▼
             claude -p  (a real Claude Code      you, at a terminal,
             session, hooks firing inside)       reading a plan / a diff
```

**No model decides what happens next.** Routing, retries, artifact validation and
state transitions are application code. An LLM is a subroutine that turns a scoped
prompt into a file on disk.

## The agents

| Agent | Role | Model | Writes |
|---|---|---|---|
| `analyst-planner` | Repository analysis, classification, and the plan - in one context | sonnet | `repo_analysis_vN.md`, `classification_vN.md`, `plan_vN.md` |
| `implementor` | Code, tests, docs - confined to the plan's file allowlist | **opus** | `implementation_vN/` |
| `reviewer` | Security **and** code-quality review in a single pass - **advisory** | sonnet | `review_vN.md` |

Agents have no `Agent` tool and the gate denies subagent invocation outright, so
peer-to-peer delegation is structurally impossible. The Implementor gets the most
capable model because that is the stage where cognitive quality turns into working
software.

## The state machine

```
RECEIVED → ANALYSIS_PLANNING → PLAN_APPROVAL → IMPLEMENTING
         → REVIEW → FINAL_APPROVAL → APPROVED → COMPLETED
                          ▲                          │
                          └── human ──────────────────┘
```

Any active state can divert to `ESCALATED` (human decision) or `FAILED`;
`RECEIVED`, `PLAN_APPROVAL` and `FINAL_APPROVAL` can reach `REJECTED`.

The two human gates are **structural**. There is no edge from `ANALYSIS_PLANNING`
to `IMPLEMENTING`, and none from `REVIEW` to `APPROVED`. Skipping a person is not
a setting that could be misconfigured - it is an edge that does not exist.

## Human-in-the-loop

The Reviewer is an **advisory tool**, not a gatekeeper. It surfaces findings; a
person decides. `APPROVED` requires a frozen approval artifact carrying
`human_approved: true` that names the exact review version it signs - so an
approval from before a rework does not vouch for the work after it, and a
hand-edited approval fails its hash check.

At each gate the orchestrator prints what you need: the plan's objective, file
allowlist, milestones, acceptance criteria and risks; then the review findings
**and the actual diff**. With nobody at the keyboard it prints the briefing and
the exact `wf approve` command and exits `2` rather than hanging.

`--auto-approve` waives the plan and final gates, but only on a `trivial`-route
workflow whose profile permits it, never the deployment gate, and always recorded
in `decision_log.jsonl`.

## Quick start

Open this repo in Claude Code **as the project root**, then describe the change:

```
/gmdw Add a rate limiter to the public API
```

or drive it directly:

```bash
python3 -m gmdw.orchestrator run request.md --by "$USER"
python3 -m gmdw.orchestrator resume      # after recording a gate decision
python3 -m gmdw.orchestrator status      # what is it waiting for?
```

Exit codes: `0` terminal state reached, `2` waiting for a human decision,
`1` halted (the reason is in the output).

Verify the toolchain any time (standard library only - no `pip install`):

```bash
.workflow/bin/wf doctor              # is the enforcement plane wired up and live?
python3 tests/test_governance.py     # the governance suite must pass
python3 tests/test_orchestrator.py   # the loop, driven with no model in it
python3 tests/test_parsers.py        # the artifact format contract
```

## Artifacts: structured Markdown

Agents write a small `---` front-matter envelope and then `## ` sections whose
bodies are bullets, Markdown tables and fenced blocks. `gmdw/parsers.py` extracts
the machine-readable parts.

```markdown
---
artifact_type: plan
workflow_id: wf_20260828_001
version: 1
produced_by: analyst-planner
status: complete
---

## Files In Scope
- src/app.py — modify — add the limiter middleware
- src/limiter.py — create — the token bucket itself

## Acceptance Criteria
| id | criterion | verification method |
|---|---|---|
| AC1 | requests beyond the limit get a 429 | unit_test |
```

This is not about Markdown being smaller than JSON. It is that **a validation
failure is local**: the rejection says `milestone M2 has no verification
sub-bullet`, and the orchestrator feeds that back as the whole correction, so a
retry rewrites one section instead of regenerating the artifact.

JSON stays where it belongs: fenced ` ```json ` blocks for small closed data, and
the **approval record**, which a machine writes and the APPROVED guard reads.

## Configuring the agent sessions (`GMDW_CLAUDE_ARGS`)

**You must set this before your first run. GMDW does not pick a default, on
purpose.**

The orchestrator starts each agent with `claude -p` — a *headless* session. A
headless session has no one to answer a tool-permission prompt, so whatever the
`claude` CLI would normally ask about, it cannot ask. Left unconfigured, an agent
can end up unable to write its own artifact, and the stage fails for a reason that
has nothing to do with the work.

`GMDW_CLAUDE_ARGS` is passed through verbatim to that invocation
([`gmdw/runner.py`](gmdw/runner.py) `ClaudeRunner.build_argv`), so it is where you
declare the permission posture your environment calls for:

```bash
# a workstation, where you want the CLI's normal prompting behavior
export GMDW_CLAUDE_ARGS="--permission-mode default"

# CI, or any unattended run, where nothing can be prompted for
export GMDW_CLAUDE_ARGS="--dangerously-skip-permissions"
```

The right choice is a property of where you are running, not of GMDW, which is why
GMDW refuses to guess. Pick deliberately: `--dangerously-skip-permissions` removes
the CLI's own confirmation layer, and it is named the way it is for a reason.

**What this cannot do:** widen what an agent is actually allowed to do. The
PreToolUse gate runs inside every child session regardless of this setting, so
write-scoping, protected paths, the push gate and the rest are unaffected. This
governs what the agent may *attempt* — GMDW still governs what happens next.

Anything the CLI accepts can go here (`--add-dir`, `--settings`, …). To point at a
different binary entirely, use `GMDW_CLAUDE_BIN`.

## How governance is enforced (the three hooks)

The orchestrator starts each agent as its own Claude Code session, so this
project's `.claude/settings.json` loads and all three hooks fire inside it.

- **`pretooluse_gate.py`** (Write/Edit, Bash, Agent/Task) - the boundary. Confines
  every write to the state's allowed scope, protects `gmdw/`, `.claude/hooks/`,
  schemas, policies and the run's own bookkeeping, denies subagent spawning,
  denies `wf approve` / `wf accept` from inside an agent session, and blocks
  push/PR without deployment approval. **Fails closed.**
- **`posttooluse_ledger.py`** - registers (hashes) every artifact written under
  the run directory. Records only.
- **`subagentstop_validate.py`** - the acceptance gate for anything still run as a
  subagent, sharing `gmdw/accept.py` verbatim with the orchestrator and with
  `wf accept`. **Fails closed** (integrity hold).

Because the orchestrator is the host process rather than a subagent, liveness is
checked twice: **statically** before any spend (all three hooks registered and
present), and **dynamically** after every invocation (the child session must have
left a hook-sourced event in the ledger). A child that ran ungoverned gets its
artifact refused, not accepted.

State, counters, budgets and versioning live in `state.json`, mutated only through
`gmdw/state.py` - via the orchestrator, `.workflow/bin/wf`, and the hooks.

## What a run costs

The first v0.3.0 run (`wf_20260828_001`, standard profile, trivial route — a
`--dry-run` flag across three files) completed both human gates in **3 agent
invocations, 1,110,759 tokens, $1.84**. Usage is exact, read from
`claude -p --output-format json`.

Two things to know about those numbers, both detailed in
[MIGRATION.md](MIGRATION.md#first-v030-run--2026-08-28): the token count includes
cached reads at full weight, so it measures context processed rather than spend
(hence 1.11M tokens for under two dollars) — **`cost_ceiling_usd` is the spend
control**. And the budget is checked before each stage, never during one, so a run
can overshoot it by one invocation.

## Policy profiles

`standard` (default), `strict` (regulated/production - every gate blocking on
every route, so `--auto-approve` cannot apply; security review always on; higher
budget), `expedited` (prototypes - larger trivial threshold, cheaper models).

Three things are **floors** a profile may tighten but never loosen, enforced by
`policy.validate_profile`: the deployment gate stays blocking, security triggers
cannot be raised past the standard baseline, and a human gate may only be set
`off` on the `trivial` route. Set the active profile in `.workflow/config.json`.

## Auditability

Every workflow leaves a complete trail in `.workflow/runs/<id>/`: `state.json`
(+ `history[]`), `decision_log.jsonl` (append-only), `artifacts_index.json`
(sha256 of every artifact), immutable versioned artifacts, and `approvals/` -
every human decision, frozen, naming what it signed and whether a person actually
looked at it.

One curated example run (`wf_20260714_001`) is committed for reference; new runs
stay local by default (see `.gitignore`). Other run IDs cited in ADRs and
historical docs were local runs and are intentionally not in the tree.

## Requirements

Python 3.9+ (standard library only). The `claude` CLI on `PATH` for agent
invocation (override with `GMDW_CLAUDE_BIN`), configured with
[`GMDW_CLAUDE_ARGS`](#configuring-the-agent-sessions-gmdw_claude_args). Git, for
the implementation base-commit and scope checks. No third-party dependencies.

## Layout

```
gmdw/                     the orchestrator + runtime library
  orchestrator.py         the deterministic router (python3 -m gmdw.orchestrator)
  runner.py               the claude CLI adapter — the only place a model is called
  parsers.py              structured-Markdown artifact parsing and validation
  state.py                the state machine, transition table and guards
  policy.py               profiles: limits, budgets, gates, model tiers
  accept.py               artifact acceptance + the human-approval gate
  ledger.py               audit log, hash registry, cost estimates
  heartbeat.py            governance-liveness attestation
  prompts/                one prompt template per agent role
.claude/agents/           the three agent definitions (tool lists = capability)
.claude/hooks/            the three governance hooks
.workflow/bin/wf          the human's CLI (status, approve, doctor, recovery)
.workflow/policies/       standard / strict / expedited
.workflow/runs/<id>/      per-workflow state, artifacts, decision log, registry
tests/                    parsers, governance, orchestrator
```

See `CLAUDE.md` for the architecture invariants. `tests/README.md` documents what
each governance scenario proves.

## Contributing & security

Contributions are welcome under the project's Apache-2.0 license - read
[CONTRIBUTING.md](CONTRIBUTING.md) first, since changes to the enforcement surface
carry extra requirements (the golden rule is *governance is enforced by code,
never by prompts*). Please **do not open a public issue** for a vulnerability or a
way to bypass a governance guarantee - report it privately per
[SECURITY.md](SECURITY.md).

## License

Licensed under the Apache License, Version 2.0 - see [LICENSE](LICENSE) and
[NOTICE](NOTICE). Copyright 2026 Arun Verma.

GMDW is an independent project and is not affiliated with, sponsored by, or
endorsed by Anthropic; "Claude" and "Claude Code" are trademarks of Anthropic,
referenced only to describe the environment GMDW runs in.
