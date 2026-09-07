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
3. Answer the three approval prompts: the plan before any code is written, the
   review and diff before it's called done, and the deployment before anything
   ships. Done.

Everything in between — sizing the change, reading the repo, planning, writing the
code and tests, reviewing it for correctness and security — happens automatically.
The decisions do not.

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
  gmdw: refusing to start:
    the governance hooks are not fully registered in .claude/settings.json
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

If the root is wrong you'll hit the refusal above — that's the liveness guard failing safe (no hooks → no enforcement → no
run), not a bug.

To govern a *different* repository, install the plane into it rather than pointing
a gmdw-rooted session at it: copy `.claude/`, `gmdw/` and `.workflow/`
(`schemas/`, `policies/`, `bin/`, `config.json`) into that repo and run the
orchestrator from its root. The hooks load from the project you are in, which is
exactly why they have to live there. Running GMDW from one repo against another is
still out of scope — that is the fail-open mode `docs/history/` records from v0.2
(see [KNOWN_LIMITATIONS](KNOWN_LIMITATIONS.md) §F).

### Verify your setup

One read-only command tells you whether the enforcement plane is actually live in
this session — it creates no workflow and has no side effects:
```bash
.workflow/bin/wf doctor        # exit 0 if the plane is usable, 1 if not
```
It reports two things separately, because they answer different questions:
*registered* (all three hooks are wired into `.claude/settings.json` and present on
disk — what the orchestrator checks before it spends anything) and *live* (a hook
has actually fired recently in this session).

Run it before your first `/gmdw` if you're unsure the root is right. Optionally,
confirm the whole toolchain is healthy (standard library only — no `pip install`):
```bash
python3 -m unittest discover -s tests -t tests    # 146 tests, must print OK
```

---

## What happens after you type `/gmdw`

`/gmdw` hands your request to a deterministic Python program — `gmdw/orchestrator.py`
— that drives the whole run. You can also call it directly, which is the same thing
without the middleman:

```bash
python3 -m gmdw.orchestrator run request.md --by "$USER"
```

It reads the workflow state, looks up what comes next in a fixed table, starts
**one** agent, checks what that agent produced, advances, and repeats. No model
decides what happens next — which is why a run does not wander, stall, or talk
itself into skipping a step.

Three agents run, in this order, each in its own session and each communicating
only through versioned, frozen artifacts:

| Stage | Agent | What it does |
|---|---|---|
| `ANALYSIS_PLANNING` | **analyst-planner** | reads the codebase, sizes the change, writes the plan |
| `IMPLEMENTING` | **implementor** | writes the code and the tests, confined to the plan's file list |
| `REVIEW` | **reviewer** | one pass over the diff for both security and code quality |

GMDW also **sizes** your request across six dimensions and picks a *route* —
`trivial`, `medium` or `full`. You don't choose it; GMDW scores it and explains
why, and security- or data-model-sensitive changes are forced to `full`
automatically. The route controls how strict the governance around the run is —
how many revisions are allowed, whether the security checklist is mandatory,
whether the gates can ever be waived.

---

## Your touchpoints (the human gates)

There are three, and the first two are **structural**: the workflow has no path
from planning to implementation, or from review to approval, that does not go
through you.

- **Plan approval — before any code is written.** GMDW prints the objective, the
  exact list of files the Implementor will be allowed to touch, the milestones, the
  acceptance criteria and the risks, then asks: **approve**, **revise**, or
  **reject**. Requesting changes sends it back to planning with your note.
- **Final approval — before anything is called done.** GMDW prints the reviewer's
  findings **and the actual diff**, then asks the same three-way question.

  The reviewer's verdict is **advisory**. It can tell you something is wrong; it
  cannot decide the work is right. That call is yours, and the machine will not
  reach `APPROVED` without a signed record of you making it.
- **Deployment approval — always.** Before anything is pushed or opened as a PR,
  GMDW stops one more time. Nothing ships without this, on any route, under any
  profile.

Plus **escalation**, if it gets stuck: a limit is hit, an agent reports it can't
proceed, or an artifact fails validation twice. GMDW hands you a summary and asks
how to proceed — **redirect** (re-plan), **override** (proceed with your
instruction), **force-approve**, or **abandon**.

If you're not at the keyboard, GMDW doesn't hang waiting. It prints the briefing,
prints the exact command to record your decision, and exits with code `2`:

```bash
.workflow/bin/wf approve plan --decision approved --by "$USER"
python3 -m gmdw.orchestrator resume
```

There is one way to skip the first two gates, and it is deliberately narrow:
`--auto-approve` waives them on a `trivial`-route change whose policy profile
allows it. It never waives the deployment gate, and every use of it is written to
the decision log.

---

## A worked example

You type:
```
/gmdw Add a --dry-run flag to the export CLI so users can preview an export
```

1. **Analysis and planning.** The analyst-planner maps the export CLI, scores the
   change *medium*, and writes a plan: which files change, the acceptance criteria,
   the test. One agent, one context — it doesn't hand its own analysis to a second
   agent to read again from scratch.
2. **You approve the plan.** GMDW prints it and stops:
   > *Your decision on the plan gate [approve/revise/reject]:*

   You read the file list, see it's exactly what you expected, and approve.
3. **It implements.** The implementor writes the `--dry-run` flag, a test, and the
   help text — confined by the gate to exactly the files the plan named. A write
   anywhere else is denied at the tool call.
4. **It reviews.** The reviewer checks the diff against the plan for correctness,
   security and the acceptance criteria. (GMDW independently re-runs the declared
   tests; a manifest that claims "tests pass" but doesn't will freeze the workflow.)
5. **You approve the result.** GMDW prints the findings and the diff and stops
   again. You read it and approve.
6. **You approve the deployment.** GMDW prepares a branch and a PR body from the
   artifact trail and asks one last time. You approve; it opens the PR and closes
   the workflow.

On a **full-route** change (say, "Add API-key auth to the public endpoints") the
gates are the same three — what changes is the rigour behind them: the reviewer's
`## Security Checklist` becomes mandatory and an empty one is not an approval,
fewer revisions are permitted before escalation, and `--auto-approve` cannot apply
at all.

---

## Checking progress (you rarely need this)

GMDW narrates as it goes, but if you want the raw state:

```bash
python3 -m gmdw.orchestrator status   # state, budget, and what it's waiting for
.workflow/bin/wf status               # counters, limits, approvals, escalations
.workflow/bin/wf expected              # the exact artifacts the current stage owes
```

Everything the run did is recorded under `.workflow/runs/<id>/`: the state, an
append-only decision log, hashes of every artifact, and every human approval. Any
shipped change is traceable end to end.

---

## When it gets stuck

GMDW is designed to fail safe — at worst it stops and asks, never proceeds
ungoverned. The situations you might hit:

- **"the governance hooks are not fully registered"** on start → you're not rooted
  in this repo.
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
- **[MIGRATION.md](../MIGRATION.md)** — what changed in v0.3.0 (deterministic
  orchestrator, Markdown artifacts, three agents, human gates) and why.
- **[docs/adr/](adr/)** — the design decisions (ADR-001: acceptance authority and
  hook liveness).
