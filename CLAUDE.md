# gmdw-claude-code — Claude Code Project Guide

This repository IS the Governed Multi-Agent Delivery Workflow (GMDW). It runs a
controlled, state-machine-driven software-delivery workflow using specialized
subagents under strict, hook-enforced governance.

## The one rule everything derives from

**Governance is enforced by code, never by prompts.** Counters, budgets, write
scopes, transition legality, and artifact acceptance are enforced by three Python
hooks and the `wf` CLI reading `state.json`. A misbehaving model can, at worst, get
stuck asking — it cannot invoke an agent illegally, exceed a limit, write out of
scope, skip a state, or advance on an invalid artifact.

## How to run a workflow

Type `/gmdw <your change request>`. The main session becomes the **Orchestrator**
(the only component with the `Agent` tool) and drives the state machine. It invokes
six subagents — repo-analyst, planner, plan-reviewer, implementor, security-reviewer,
code-reviewer — **only** through artifacts. Agents never invoke each other (they have
no `Agent` tool — structural isolation).

## Architecture invariants (do not violate)

1. **Orchestrator-centric.** Only the Orchestrator invokes agents. Enforced by
   subagent tool lists excluding `Agent`.
2. **Artifact-only communication.** Agents read/write versioned JSON artifacts under
   `.workflow/runs/<id>/artifacts/`. No peer channels.
3. **`state.json` is mutated ONLY by `.workflow/bin/wf` and the hooks** (both call
   `.claude/hooks/gmdw/state.py`). Writing it via any tool is denied by the gate.
4. **Artifacts are immutable once accepted.** The SubagentStop validator freezes them
   (chmod a-w + sha256 in `artifacts_index.json`). Corrections are new versions.
5. **Policy is pinned at init.** The active profile is copied into `state.json`;
   editing a profile never changes a running workflow.
6. **Hooks fail closed.** Any hook error while a workflow is ACTIVE denies the action
   (PreToolUse) or sets an integrity hold (SubagentStop).

## Layout

- `.claude/agents/` — the six subagent definitions (tool lists = capability boundary).
- `.claude/commands/gmdw.md` — the Orchestrator procedure.
- `.claude/hooks/` — `pretooluse_gate.py`, `posttooluse_ledger.py`,
  `subagentstop_validate.py`, and the `gmdw/` runtime library.
- `.claude/settings.json` — wires the hooks to PreToolUse/PostToolUse/SubagentStop.
- `.workflow/schemas/` — the 9 artifact contracts (JSON Schema Draft 2020-12).
- `.workflow/policies/` — `standard` / `strict` / `expedited` profiles.
- `.workflow/bin/wf` — the CLI (init/status/expected/transition/approve/validate/close).
- `.workflow/runs/<id>/` — per-workflow state, artifacts, decision log, registry.
- `tests/` — the governance verification suite (proves every guarantee with a
  misbehaving model).

## Validation before committing changes to the governance code

```bash
python3 -m pytest tests/ -q          # or: python3 tests/test_governance.py
for f in .claude/hooks/*.py .claude/hooks/gmdw/*.py .workflow/bin/wf; do \
  python3 -c "import ast,sys; ast.parse(open('$f').read())" || echo "SYNTAX: $f"; done
```

## Working on this repo (meta note)

While no workflow is ACTIVE, the repo behaves like a normal Claude Code project and
you can edit freely. The governance gate only engages once `.workflow/ACTIVE` exists.
Do not hand-edit files under `.workflow/runs/` — treat them as machine-owned.
