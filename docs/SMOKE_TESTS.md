# GMDW Smoke Tests — first live runs

Three scripted runs that exercise the whole system against a real model, in
increasing order of governance weight. The automated suites already prove every
guarantee against a *simulated* model (`python3 -m unittest discover -s tests -t
tests`, 146 tests, no model called); these answer the questions only a live run
can: does the `claude` CLI behave the way `gmdw/runner.py` assumes, do the hooks
actually fire inside a child session, and is the human experience at the gates
one a person can actually act on.

Run them in order. Each says what to type, what you should see, and what it
proves. Stop and file a finding the moment reality diverges — a smoke test that
"basically worked" has proved nothing.

## Before you start

```bash
.workflow/bin/wf doctor            # must report REGISTERED (exit 0)
which claude                       # or set GMDW_CLAUDE_BIN
python3 -m unittest discover -s tests -t tests   # must print OK
```

A headless `claude -p` session cannot answer a tool-permission prompt. Set the
permission posture your environment calls for before the first run — GMDW does not
pick one for you:

```bash
export GMDW_CLAUDE_ARGS="--permission-mode acceptEdits"
```

The PreToolUse gate still runs inside every child session regardless, so this
widens what an agent may *attempt*, never what it may do.

Each run writes to `.workflow/runs/<id>/`. Keep the directory; it is the evidence.

---

## S1 — trivial route, gates waived

**Request.** Something genuinely small and self-contained, e.g. *"Add a `--version`
flag that prints the package version to the export CLI."*

```bash
printf 'Add a --version flag to the export CLI.\n' > /tmp/s1.md
python3 -m gmdw.orchestrator run /tmp/s1.md --by "$USER" --auto-approve
```

**Expected states.**
`RECEIVED → ANALYSIS_PLANNING → PLAN_APPROVAL → IMPLEMENTING → REVIEW →
FINAL_APPROVAL → APPROVED → COMPLETED`

Note that the gate *states* are still traversed — `--auto-approve` records an
approval at them, it does not remove them.

**Expected artifacts.** `repo_analysis_v1.md`, `classification_v1.md` (route
`trivial`), `plan_v1.md`, `implementation_v1/{manifest.md,diff.patch,test_output.txt}`,
`review_v1.md`, and three approval records.

**Expected audit entries.** `workflow_created` (with `auto_approve: true`);
`auto_approve_enabled`; `transition` ×7; `invocation_requested` /
`invocation_completed` ×3; `hook_liveness` once per leg; `artifact_written` and
`artifact_accepted` for each artifact; `verification` (the test re-run);
`auto_approve_used` ×2 (plan, final); `human_approval` ×3; `workflow_closed`.

**You should still be asked once** — the deployment gate is a policy floor and
`--auto-approve` never touches it.

**What it proves.** The loop runs end to end against a real model; the hooks fire
inside child sessions (`hook_liveness` is present, so the orchestrator accepted the
artifacts); `--auto-approve` applies exactly where policy permits and nowhere else.

**Check by hand:**
```bash
jq -r 'select(.event=="auto_approve_used") | .gate' .workflow/runs/<id>/decision_log.jsonl
jq -r '.human_approved, .auto_approved' .workflow/runs/<id>/artifacts/approvals/final_approval_1.json
# expect: plan, final  /  false, true
```
An `auto_approved` approval must record `human_approved: false`. If it claims a
human looked at it, that is a P0 finding.

---

## S2 — medium route, all three gates

**Request.** An everyday change across two or three files, e.g. *"Add a `--dry-run`
flag to the export CLI so users can preview an export."*

```bash
printf 'Add a --dry-run flag to the export CLI so users can preview an export.\n' > /tmp/s2.md
python3 -m gmdw.orchestrator run /tmp/s2.md --by "$USER"
```

**Expected states.** The same seven transitions — but you answer all three gates.

**What to actually read at each gate** (this is the test):

1. **Plan gate.** Is the `FILES IN SCOPE` list one you'd have written? Are the
   acceptance criteria checkable, or are they "works correctly"? Try
   **`revise`** the first time through with a real instruction, confirm the run
   loops back to `ANALYSIS_PLANNING`, produces `plan_v2.md`, and that
   `plan_revisions` incremented. Then approve.
2. **Final gate.** You get the findings *and the diff*. Read the diff. The whole
   point of this gate is that a person looked at the code — if the diff is
   unreadable at this length, that is a finding about the presentation, not about
   you.
3. **Deployment gate.**

**Also exercise the parked path.** Interrupt at a gate (Ctrl-C or answer nothing)
and confirm the orchestrator exits `2` with a briefing rather than hanging, then:

```bash
.workflow/bin/wf approve plan --decision approved --by "$USER"
python3 -m gmdw.orchestrator resume
```

The resumed run must pick up exactly where it stopped, from a *fresh process*.

**What it proves.** The human gates are usable, not just present; the plan-revision
loop works end to end; the run is resumable because the orchestrator holds no state
the run directory does not.

---

## S3 — full route, security-flagged

**Request.** Something that genuinely touches an auth or data surface, e.g. *"Add
API-key authentication to the public endpoints."*

```bash
printf 'Add API-key authentication to the public endpoints.\n' > /tmp/s3.md
python3 -m gmdw.orchestrator run /tmp/s3.md --by "$USER"
```

**Expected classification.** `route: full`, `flags: security` (a security score of
2+ forces it). Confirm in `classification_v1.md` — and confirm `wf status` shows
`flags.security: true`, i.e. the route was synced from the artifact, not asserted.

**Expected additions.** `review_v1.md` must carry a non-empty `## Security
Checklist` with real `area | evidence` rows. An empty one is rejected by the
acceptance pipeline, and the run will retry once and then escalate.

**Adversarial checks to run by hand** (from an ordinary terminal, *not* inside an
agent session):

```bash
# 1. an LLM verdict alone must not reach APPROVED
.workflow/bin/wf status | jq .state          # FINAL_APPROVAL, review verdict approve
.workflow/bin/wf transition APPROVED --trigger x
# expect: exit 1, "human sign-off on the review and the diff is required"

# 2. an approval must name the current review
.workflow/bin/wf approve final --decision approved --by "$USER"
jq -r .subject_ref .workflow/runs/<id>/artifacts/approvals/final_approval_1.json
# expect: artifacts/review_v1.md — not empty

# 3. push is gated on the deployment approval, not on APPROVED alone
git push        # from inside an agent session: denied
```

**What it proves.** The security flag propagates from the classification artifact
into the guards; the merged Reviewer preserves the dedicated security reviewer's
one real guarantee (an empty checklist is not an approval); and the human gate is
the only path to `APPROVED`.

---

## Recording what you find

For each run, keep the run directory and note:

- **Token and cost totals** (`wf status | jq .budget`). Compare against the
  profile's `token_budget` and `cost_ceiling_usd`. This is the first real evidence
  for the cost claim in `MIGRATION.md`, which is currently structural rather than
  measured — if a standard-profile run does not fit in 900,000 tokens and 8
  invocations, say so.
- **Whether usage was exact** (`estimated: false` on `invocation_completed`). If
  the `claude` CLI's JSON envelope does not carry `usage`, the budget falls back to
  a size estimate and every budget number is coarser than it looks.
- **Any retry.** `invocation_requested` with `attempt: 2` means an artifact was
  rejected. Read the rejection reason: it should name one section or one row. If it
  says something vague, that is a defect in `gmdw/parsers.py`, because the whole
  Markdown-artifact argument rests on corrections being local.
- **Anything you had to do by hand.** A step you performed that the orchestrator
  should have is the most valuable finding in the whole exercise.
