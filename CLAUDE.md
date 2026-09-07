# gmdw-claude-code — Claude Code Project Guide

This repository IS the Governed Multi-Agent Delivery Workflow (GMDW): a
deterministic Python orchestrator that drives a state-machine software-delivery
workflow, calls Claude Code agents for the cognitive stages, and stops for a
human at the two decisions that matter.

## The two rules everything derives from

**1. Governance is enforced by code, never by prompts.** Counters, budgets, write
scopes, transition legality, and artifact acceptance are enforced by Python — the
orchestrator, the `wf` CLI, and three hooks, all reading `state.json`. A
misbehaving model can, at worst, get its artifact rejected; it cannot invoke an
agent illegally, exceed a limit, write out of scope, skip a state, or advance on
an invalid artifact.

**2. Routing is not a cognitive task.** The orchestrator is a loop over a
transition table, not an LLM reading a procedure. No model is consulted about what
happens next. If you find yourself writing "the Orchestrator should decide…" into
a prompt file, that logic belongs in `gmdw/orchestrator.py` instead.

## How to run a workflow

```bash
python3 -m gmdw.orchestrator run request.md --by "$USER"
python3 -m gmdw.orchestrator resume     # after a human records a gate decision
python3 -m gmdw.orchestrator status
```

`/gmdw <request>` does the same thing — it is a thin launcher that writes the
request to a file and shells out. The main session is **not** the Orchestrator and
must not act like one.

The loop runs three agents — `analyst-planner`, `implementor`, `reviewer` — each
as its own `claude -p` session, communicating only through versioned Markdown
artifacts under `.workflow/runs/<id>/artifacts/`.

## Architecture invariants (do not violate)

1. **Deterministic routing.** State transitions, retry logic, artifact routing and
   error handling are application code in `gmdw/`. Never a prompt.
2. **One agent at a time, started only by the orchestrator.** Agents have no
   `Agent` tool and the PreToolUse gate denies subagent invocation outright.
3. **Artifact-only communication.** Agents read and write versioned structured-
   Markdown artifacts. No peer channels.
4. **`state.json` is mutated ONLY through `gmdw/state.py`** — via the
   orchestrator, `.workflow/bin/wf`, and the hooks. Writing it via any tool is
   denied by the gate.
5. **Artifacts are immutable once accepted.** `gmdw/accept.py` freezes them
   (chmod a-w + sha256 in `artifacts_index.json`). Corrections are new versions.
6. **Policy is pinned at init.** The active profile is copied into `state.json`;
   editing a profile never changes a running workflow.
7. **An LLM cannot approve anything.** There is no edge from `REVIEW` to
   `APPROVED`, nor from `ANALYSIS_PLANNING` to `IMPLEMENTING`. Both go through a
   human gate that requires a frozen approval artifact naming the current artifact
   version. The Reviewer's verdict is advisory input to that decision.
8. **Hooks fail closed.** Any hook error while a workflow is ACTIVE denies the
   action (PreToolUse) or sets an integrity hold (SubagentStop).

## Layout

- `gmdw/` — the orchestrator and runtime library. **Governance-protected**: the
  gate denies writes here while a workflow is active.
  - `orchestrator.py` — the deterministic router and its CLI.
  - `runner.py` — the `claude` CLI adapter; the only place a model is called.
  - `parsers.py` — structured-Markdown parsing and validation.
  - `state.py` / `policy.py` / `accept.py` / `ledger.py` / `heartbeat.py`.
  - `prompts/` — one prompt template per agent role.
- `.claude/agents/` — the three agent definitions (tool lists = capability boundary).
- `.claude/commands/gmdw.md` — the thin launcher.
- `.claude/hooks/` — `pretooluse_gate.py`, `posttooluse_ledger.py`,
  `subagentstop_validate.py`.
- `.claude/settings.json` — wires the hooks to PreToolUse/PostToolUse/SubagentStop.
- `.workflow/schemas/` — `approval` (the one artifact still JSON) and `state`.
  The Markdown artifact contracts live in `gmdw/parsers.py`.
- `.workflow/policies/` — `standard` / `strict` / `expedited`.
- `.workflow/bin/wf` — the human's CLI (status, expected, doctor, approve,
  transition, accept, clear-invocation, validate, close).
- `.workflow/runs/<id>/` — per-workflow state, artifacts, decision log, registry.
- `tests/` — `test_parsers.py` (the artifact contract), `test_governance.py`
  (every guarantee, against a misbehaving model), `test_orchestrator.py` (the loop,
  driven with no model in it), `fixtures.py` (shared builders).

## Validation before committing changes to the governance code

```bash
python3 tests/test_parsers.py
python3 tests/test_governance.py
python3 tests/test_orchestrator.py
# or all three:
python3 -m unittest discover -s tests -t tests

for f in .claude/hooks/*.py gmdw/*.py .workflow/bin/wf; do \
  python3 -c "import ast,sys; ast.parse(open('$f').read())" || echo "SYNTAX: $f"; done
for f in .workflow/schemas/*.json .workflow/policies/*.json; do \
  python3 -c "import json,sys; json.load(open('$f'))" || echo "JSON: $f"; done
```

## Working on this repo (meta note)

While no workflow is ACTIVE, the repo behaves like a normal Claude Code project
and you can edit freely. The governance gate only engages once `.workflow/ACTIVE`
exists — and once it does, `gmdw/` and `.claude/hooks/` become unwritable, so a
GMDW run cannot rewrite its own router. Do not hand-edit files under
`.workflow/runs/`; treat them as machine-owned.
