---
description: Run a Governed Multi-Agent Delivery Workflow for the given request
argument-hint: <the change request, in natural language>
---

Start a GMDW run for this request:

$ARGUMENTS

You are **not** the Orchestrator. Since v0.3.0 the Orchestrator is a deterministic
Python program — `gmdw/orchestrator.py` — that reads state, picks the next stage
from a transition table, starts exactly one agent, parses its Markdown artifacts,
validates them, and advances. No model decides what happens next, including you.

Your entire job here is to hand the request to that program and report what it
says back.

## Do this

1. Write the request verbatim to a scratch file, e.g. `/tmp/gmdw_request.md`.
   Keep the user's own words; do not summarize, expand, or "improve" the request —
   the Analyst-Planner is the thing that interprets it, and it should interpret
   what the user actually wrote.
2. Run:
   ```bash
   python3 -m gmdw.orchestrator run /tmp/gmdw_request.md --by "<the user>"
   ```
3. Relay the output. The orchestrator prints each stage as it runs, prints the
   full briefing at every human gate, and exits:
   - **0** — the workflow reached a terminal state (COMPLETED / REJECTED / FAILED).
   - **2** — it is waiting for a human decision. The output names the exact
     `wf approve …` command and tells the user to `resume`. Surface both.
   - **1** — it halted. The reason is in the output; do not try to work around it.

## Do not

- Do not invoke subagents. The PreToolUse gate denies it, and there is nothing to
  invoke: the orchestrator starts agents as their own sessions.
- Do not run `wf approve` on the user's behalf. The gates exist to put a person in
  the loop; you signing them is the failure mode the rework removed. Show the user
  the briefing and the command, and let them decide.
- Do not edit `state.json`, `decision_log.jsonl`, `artifacts_index.json`, any
  file under `gmdw/`, `.claude/hooks/`, `.workflow/schemas/`, `.workflow/policies/`,
  or any artifact an agent produced. The gate denies all of these.
- Do not use `--auto-approve` unless the user explicitly asks for it. It waives
  the plan and final human gates, applies only to a trivial-route workflow whose
  profile permits it, never waives the deployment gate, and is recorded in
  `decision_log.jsonl`.

## Useful while a run is in flight

```bash
python3 -m gmdw.orchestrator status   # what is it waiting for?
.workflow/bin/wf status               # counters, budget, approvals
.workflow/bin/wf doctor               # is the enforcement plane wired up and live?
python3 -m gmdw.orchestrator resume   # continue after a human recorded a decision
```

If the orchestrator refuses to start because the enforcement hooks are not
registered, the session is not rooted in this project. Relaunch Claude Code with
`gmdw-claude-code` as the project root rather than reaching for
`GMDW_ALLOW_UNGOVERNED` — that override is for CI and manual ops, and it is
recorded in the ledger.
