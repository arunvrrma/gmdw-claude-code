# Getting Started with GMDW

GMDW makes a code change for you with AI — but with the safety rails (scope,
budgets, reviews, approvals, audit trail) **enforced by code, not by trusting the
model**. This guide is written from your seat: what *you* actually do.

The machinery is elaborate; your job is small. In most runs you type one command
and answer one or two approval prompts.

---

## The 30-second version

1. Open this repository in Claude Code **as the project root** (see [Setup](#setup)).
2. Type your change request:
   ```
   /gmdw Add a --dry-run flag to the export CLI
   ```
3. Answer the approval prompts when they appear (approve the plan on larger
   changes, approve the deployment before anything ships). Done.

Everything in between — sizing the change, reading the repo, planning, writing the
code and tests, security and code review — happens automatically.

---

## Setup

- **Python 3.9+** and **git** on your PATH. No `pip install` — the tooling is
  standard-library only.
- Launch Claude Code with **`gmdw-claude-code` as the project root.** This is the
  one prerequisite people miss, and it matters: GMDW's guarantees come from hooks
  registered in this project's settings. If you start a workflow from a different
  project root, those hooks don't fire and there's no enforcement — so GMDW refuses
  to start:

  ```
  wf: error: governance hooks are not active in this session — refusing to
  start a workflow.
  ```

  If you see that, relaunch Claude Code rooted in this repo and retry.

### Setting up in VS Code / other IDEs

The hooks that enforce GMDW only load when **`gmdw-claude-code` is the launch /
workspace root** — the folder Claude Code treats as the project (it's identified by
its `.claude/` directory). How you set that depends on the surface:

- **Terminal (CLI):** `cd` into the repo and run `claude`. The nearest `.claude/`
  above your working directory becomes the project root.
- **VS Code / Cursor extension:** use **File → Open Folder** and open the
  `gmdw-claude-code` folder *itself* as the workspace root — not a parent folder,
  and avoid multi-root workspaces (in either case the repo's `.claude/settings.json`
  may not be the one that loads, so the hooks won't register). You must also
  **trust the workspace**: hooks execute code, and the extension won't run them in
  Restricted Mode.
- **Claude Code on the web:** start the session against this repository so its
  `.claude/` is the checkout root.

If the root is wrong you'll hit the *"governance hooks are not active"* refusal
above — that's the liveness guard failing safe (no hooks → no enforcement → no
run), not a bug. Relatedly, GMDW governs changes **in its own repo**; using it to
change a *different* repo from a gmdw-rooted session is out of scope (see
[KNOWN_LIMITATIONS](KNOWN_LIMITATIONS.md)).

### Verify your setup

One read-only command tells you whether the enforcement plane is actually live in
this session — it creates no workflow and has no side effects:
```bash
.workflow/bin/wf doctor        # ✅ LIVE (exit 0), or ❌ not firing (exit 1)
```
Run it before your first `/gmdw` if you're unsure the root is right. Optionally,
confirm the whole toolchain is healthy (standard library only — no `pip install`):
```bash
python3 tests/test_governance.py     # the governance suite must pass
```

---

## What happens after you type `/gmdw`

GMDW first **sizes** your request across six dimensions and picks a *route* — this
decides how much ceremony you'll see:

| Route | When | What runs | Your approvals |
|---|---|---|---|
| **trivial** | tiny, low-risk changes | straight to implementation + review | just the deployment gate |
| **medium** | most everyday changes | analysis → plan → implement → review | the plan is shown to you (FYI); deployment gate |
| **full** | large, architectural, or security/data changes | adds plan review, security review, and a **blocking plan approval** | you approve the plan *and* the deployment |

You don't choose the route — GMDW scores it and explains why. Security- or
data-model-sensitive changes are forced to **full** automatically.

From there it drives itself through the stages, invoking one specialized agent per
stage (repo-analyst → planner → plan-reviewer → security-reviewer → implementor →
code-reviewer), each communicating only through versioned, frozen artifacts.

---

## Your touchpoints (the human gates)

You'll only be interrupted at decision points that are genuinely yours:

- **Deployment approval — always.** Before anything is committed to a shareable
  branch, pushed, or opened as a PR, GMDW stops and asks you to approve. Nothing
  ships without this.
- **Plan approval — on full-route changes** (or under the `strict` profile). Before
  a single line of code is written, GMDW shows you the plan — objective, scope,
  risks, files — and asks: approve, request changes, or reject. On medium-route
  changes the plan is shown as an FYI and it proceeds.
- **Escalation — if it gets stuck.** If a limit is hit, an agent reports it can't
  proceed, or a reviewer rejects the work, GMDW hands you a summary and asks how to
  proceed: **redirect** (re-plan), **override** (proceed with your instruction),
  **force-approve**, or **abandon**.
- **Route downgrade — rare.** If analysis reveals the change is smaller than first
  scored, GMDW asks permission before skipping planning.

Each gate is a simple multiple-choice prompt. Answer it and GMDW continues.

---

## A worked example

You type:
```
/gmdw Add a --dry-run flag to the export CLI so users can preview an export
```

1. **It sizes the change.** GMDW scores it *medium* (a couple of files, no security
   or data-model impact) and tells you so.
2. **It analyzes and plans** — the repo-analyst maps the export CLI, the planner
   writes a plan (which files change, the acceptance criteria, the test), and the
   plan-reviewer checks it. You see a one-paragraph plan summary go by.
3. **It implements.** The implementor writes the `--dry-run` flag, a test, and
   updates the help text — confined by the gate to exactly the files the plan named.
   The code-reviewer checks the result against the plan and confirms the acceptance
   criteria pass. (GMDW independently re-runs the declared tests; a manifest that
   claims "tests pass" but doesn't will freeze the workflow.)
4. **It asks you to deploy.** GMDW prepares a branch and a PR body from the artifact
   trail and stops:
   > *Deployment approval — approve to open the PR, or reject?*

   You approve. GMDW opens the PR and closes the workflow.

On a **full-route** change (say, "Add API-key auth to the public endpoints"), the
only difference you'd feel is one extra prompt up front — approving the plan (now
also security-reviewed) before implementation begins.

---

## Checking progress (you rarely need this)

GMDW narrates as it goes, but if you want the raw state:

```bash
.workflow/bin/wf status      # current state, route, counters, budget
.workflow/bin/wf expected    # what the workflow is waiting for right now
```

Everything the run did is recorded under `.workflow/runs/<id>/`: the state, an
append-only decision log, hashes of every artifact, and every human approval. Any
shipped change is traceable end to end.

---

## When it gets stuck

GMDW is designed to fail safe — at worst it stops and asks, never proceeds
ungoverned. The situations you might hit:

- **"governance hooks are not active"** on start → you're not rooted in this repo.
  Relaunch Claude Code with `gmdw-claude-code` as the project root. (See [Setup](#setup).)
- **A reviewer rejects, or a limit is reached** → GMDW escalates to you (see
  [touchpoints](#your-touchpoints-the-human-gates)). Pick redirect / override /
  force-approve / abandon.
- **Budget or deadline exhausted** → the run stops and escalates. Raising limits is
  an operator action; abandon the run and start fresh, or ask an operator to extend.
- **The run is genuinely stuck after an interruption** (e.g. a session died
  mid-invocation) → two operator recovery commands exist, for use only from a
  session where the hooks are *not* firing (a plain terminal), and both require a
  human name for the audit trail:
  ```bash
  .workflow/bin/wf accept --by <you>             # accept an artifact that was
                                                 # validly produced but never recorded
  .workflow/bin/wf clear-invocation --by <you>   # release a stuck invocation lock
  ```
  While the governance hooks are live these are denied by design — nothing
  legitimate needs them inside a healthy governed session.

If in doubt, `wf status` shows exactly where the workflow is, and escalating
(`ESCALATED`) is always reachable so a human can decide.

---

## Go deeper

- **[CLAUDE.md](../CLAUDE.md)** — the architecture, the invariants, and the
  directory map.
- **[README.md](../README.md)** — the agents, the state machine, and how the three
  hooks enforce governance.
- **[docs/KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)** — what GMDW deliberately
  does and does not guarantee.
- **[docs/adr/](adr/)** — the design decisions (ADR-001: acceptance authority and
  hook liveness).
