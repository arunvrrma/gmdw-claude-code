"""GMDW deterministic orchestrator — the router.

    python -m gmdw.orchestrator run request.md

This module is the inversion the v0.3.0 rework is named for. Until v0.2.x the
Orchestrator was an LLM reading a prompt file and *interpreting* a state machine:
it re-read the whole artifact chain on every turn, it stalled whenever it
misparsed a transition, and the governance hooks spent half their effort failing
the Orchestrator closed against itself. Routing is not a cognitive task. It is a
loop over a transition table, and a loop over a transition table belongs in
Python.

So: this program reads state, decides the next stage from
:data:`gmdw.state.TRANSITIONS`, shells out to one agent for the one thing that
*is* cognitive, parses what came back, validates it, advances, and repeats. No
model is consulted about what happens next — ever.

The loop
--------
Each iteration does exactly one of four things, chosen by ``state``:

``RECEIVED``                     open the workflow
``ANALYSIS_PLANNING`` /
``IMPLEMENTING`` / ``REVIEW``    run that stage's agent, accept its artifacts
``PLAN_APPROVAL`` /
``FINAL_APPROVAL`` / ``APPROVED``  put the decision to a human
``ESCALATED`` / terminal          stop, with a reason

Every one of those is a pure function of what is on disk. Kill the process at any
point and re-running ``resume`` picks up from the same place, because the process
holds no state the run directory does not.

Governance
----------
The orchestrator is the host process now, not a subagent, so it is outside the
hooks rather than under them. Two things keep that honest:

1. **Static wiring check before spending anything** — the three hooks must be
   registered in ``.claude/settings.json`` and present on disk
   (``heartbeat.enforcement_installed``).
2. **Dynamic proof after every invocation** — the child session must have left a
   hook-sourced event in the ledger for this leg
   (``ledger.hook_event_since_last_transition``). A child that ran without hooks
   gets its artifact refused, not accepted.

Every limit the PreToolUse gate enforces is also checked here, before the spend
rather than after it: invocation count, revision counts, review cycles, token
budget, the cost ceiling, and the wall-clock deadline.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # `python gmdw/orchestrator.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "gmdw"

from . import __version__, accept, heartbeat, ledger, policy, runner  # noqa: E402
from . import state as S                                              # noqa: E402
from .state import GmdwError                                          # noqa: E402

EXIT_OK = 0
EXIT_HALTED = 1
EXIT_AWAITING_HUMAN = 2
EXIT_USAGE = 3

# How many times one stage may be re-run after a rejected artifact before the
# orchestrator stops paying for the same mistake. One retry, with the validator's
# exact complaint appended — the same budget the v0.2 Orchestrator had.
MAX_STAGE_RETRIES = 1

# Loop bound. Every iteration either advances the state or stops, so this only
# ever catches a bug; it exists so a bug costs an exit code, not a budget.
DEFAULT_MAX_STEPS = 40

_AFFIRMATIVE = {"1", "true", "yes", "on"}


def _affirmative(value) -> bool:
    return str(value or "").strip().lower() in _AFFIRMATIVE


# The v0.3.0 consolidation folded the adversarial plan-reviewer into the
# Analyst-Planner, which now reviews its own plan. That is a defensible trade —
# structure is checked deterministically, substance by a human — but it is only
# defensible if the human knows they are the one making the call. Saying so at
# the moment of the decision is the whole point; saying it only in the docs
# would be saying it to nobody.
NO_INDEPENDENT_REVIEW_NOTICE = (
    "⚠  No independent model review was performed on this plan.\n"
    "   Your approval is the sole quality gate before implementation begins."
)


def _truncate(text: str, lines: int, note: str = "") -> str:
    parts = (text or "").splitlines()
    if len(parts) <= lines:
        return text or ""
    return "\n".join(parts[:lines]) + f"\n… [{len(parts) - lines} more lines{note}]"


# --------------------------------------------------------------------------- #
# Human I/O
# --------------------------------------------------------------------------- #

class Console:
    """The human side of the loop.

    ``ask`` returns None when there is nobody there — no tty, or answers
    exhausted. That is not an error: the orchestrator prints the briefing and the
    exact ``wf approve`` command to run, then exits ``EXIT_AWAITING_HUMAN`` so the
    decision can be made deliberately and the run resumed. A governance gate must
    never be resolvable by a process hanging on a closed stdin.
    """

    def __init__(self, stream=None, answers=None, interactive=None):
        self.stream = stream or sys.stdout
        self.answers = list(answers or [])
        if interactive is None:
            interactive = bool(self.answers) or (
                hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
        self.interactive = interactive

    def say(self, text: str = "") -> None:
        print(text, file=self.stream)

    def rule(self, title: str) -> None:
        self.say("")
        self.say(f"── {title} " + "─" * max(4, 68 - len(title)))

    def ask(self, question: str, choices) -> str | None:
        if self.answers:
            answer = str(self.answers.pop(0)).strip().lower()
            self.say(f"{question} [{'/'.join(choices)}]: {answer}")
            return answer if answer in choices else None
        if not self.interactive:
            return None
        for _ in range(3):
            try:
                raw = input(f"{question} [{'/'.join(choices)}]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                self.say("")
                return None
            if raw in choices:
                return raw
            self.say(f"  please answer one of: {', '.join(choices)}")
        return None

    def prompt_text(self, question: str) -> str:
        if self.answers:
            return str(self.answers.pop(0))
        if not self.interactive:
            return ""
        try:
            return input(f"{question}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


@dataclass
class StepOutcome:
    """One iteration's result: whether to keep looping, and why not."""
    done: bool = False
    exit_code: int = EXIT_OK
    message: str = ""


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #

class Orchestrator:

    def __init__(self, root, *, agent_runner=None, console=None,
                 by: str = "unknown", auto_approve: bool = False,
                 allow_ungoverned: bool = False,
                 max_steps: int = DEFAULT_MAX_STEPS):
        self.root = Path(root)
        self.runner = agent_runner or runner.ClaudeRunner()
        self.console = console or Console()
        self.by = by
        self.auto_approve = auto_approve
        self.allow_ungoverned = allow_ungoverned or _affirmative(
            os.environ.get("GMDW_ALLOW_UNGOVERNED"))
        self.max_steps = max_steps
        self._stage_retries = {}

    # -- state access -------------------------------------------------------- #

    def load(self):
        return S.load_state(self.root)

    def save(self, wf_id, st):
        S.save_state(self.root, wf_id, st)

    def log(self, wf_id, event, **fields):
        ledger.log(self.root, wf_id, event, **fields)

    # -- preflight ----------------------------------------------------------- #

    def preflight(self) -> tuple:
        """(ok, reason) — refuse to start a run that cannot be governed."""
        installed, why = heartbeat.enforcement_installed(self.root)
        if not installed and not self.allow_ungoverned:
            return False, (
                f"{why}\n"
                f"  Enforcement (write-scoping, protected paths, the push gate) "
                f"lives in hooks that run inside each agent's Claude Code "
                f"session.\n"
                f"  Without them an agent session is ungoverned. Fix the wiring, "
                f"or set GMDW_ALLOW_UNGOVERNED=1 for legitimate non-Claude use "
                f"(recorded in the ledger).")
        if isinstance(self.runner, runner.ClaudeRunner):
            ok, why = self.runner.available()
            if not ok:
                return False, why
        return True, ""

    # -- workflow creation --------------------------------------------------- #

    def start(self, request_path, *, title=None, profile=None) -> str:
        p = Path(request_path)
        text = sys.stdin.read() if str(request_path) == "-" else (
            p.read_text(encoding="utf-8") if p.exists() else "")
        if not text.strip():
            raise GmdwError(f"request is empty or unreadable: {request_path}")
        title = title or text.strip().splitlines()[0][:120]

        wf_id = S.create_workflow(self.root, text, title, self.by, profile,
                                  auto_approve=self.auto_approve)
        installed, why = heartbeat.enforcement_installed(self.root)
        if not installed:
            self.log(wf_id, "governance_liveness_bypassed", reason=why, by=self.by,
                     phase="init")
        if self.auto_approve:
            # The opt-out is a governance event in its own right: it must be
            # visible in the audit trail whether or not it is ever exercised.
            self.log(wf_id, "auto_approve_enabled", by=self.by,
                     gates=["plan", "final"],
                     note="deployment approval is a policy floor and is never "
                          "auto-approved")
        return wf_id

    # -- the loop ------------------------------------------------------------ #

    def loop(self) -> int:
        for _ in range(self.max_steps):
            try:
                outcome = self.step()
            except GmdwError as e:
                self.console.say(f"\n✖ {e}")
                return EXIT_HALTED
            if outcome.done:
                if outcome.message:
                    self.console.say(outcome.message)
                return outcome.exit_code
        self.console.say(f"\n✖ step limit ({self.max_steps}) reached without "
                         f"terminating; this is a bug — the workflow is intact, "
                         f"inspect it with `wf status`.")
        return EXIT_HALTED

    def step(self) -> StepOutcome:
        wf_id, st = self.load()
        state = st["state"]

        if state in S.TERMINAL:
            self._maybe_close(wf_id, st)
            return StepOutcome(True, EXIT_OK,
                               f"\n✔ workflow {wf_id} finished in {state}.")

        if state == "ESCALATED":
            return self._escalation(wf_id, st)

        if state == "RECEIVED":
            self.console.rule(f"{wf_id} — {st['workflow']['title']}")
            S.transition(self.root, "ANALYSIS_PLANNING", trigger="request.md")
            return StepOutcome()

        if state in S.GATE_FOR_STATE:
            return self._human_gate(wf_id, st, S.GATE_FOR_STATE[state])

        if state == "APPROVED":
            return self._human_gate(wf_id, st, "deployment")

        if state in S.AGENT_FOR_STATE:
            return self._agent_stage(wf_id, st)

        return StepOutcome(True, EXIT_HALTED,
                           f"✖ no orchestration rule for state {state}")

    # -- agent stages -------------------------------------------------------- #

    def _next_state_after(self, st: dict) -> str:
        """The deterministic successor of a completed agent stage.

        This is the whole 'routing decision' the LLM Orchestrator used to make,
        and it is four lines of table lookup.
        """
        state = st["state"]
        if state == "ANALYSIS_PLANNING":
            return "PLAN_APPROVAL"
        if state == "IMPLEMENTING":
            return "REVIEW"
        if state == "REVIEW":
            review = S.load_artifact(self.root, st, "review")
            verdict = (review or {}).get("verdict")
            # A 'revise' verdict may loop straight back to the Implementor,
            # but only when a human has already opted out of the review gate.
            # Otherwise the person sees the findings first — that is the point.
            if verdict == "revise" and st.get("auto_approve") and \
                    st["counters"]["impl_revisions"] < st["limits"]["max_impl_revisions"]:
                return "IMPLEMENTING"
            return "FINAL_APPROVAL"
        raise GmdwError(f"no successor defined for agent state {state}")

    def _limits_preflight(self, st: dict, exp: dict) -> str | None:
        """Every limit the PreToolUse gate enforces, checked before the spend.

        The gate still enforces these inside the child session; duplicating them
        here is not redundancy, it is the difference between refusing to start an
        invocation and discovering halfway through that it was never allowed.
        """
        c, lim, b = st["counters"], st["limits"], st["budget"]
        if st.get("open_invocation"):
            return (f"an invocation is already open "
                    f"({st['open_invocation'].get('agent')}); single-flight only. "
                    f"Release it with `wf clear-invocation --by <you>`.")
        if c["agent_invocations"] >= lim["max_agent_invocations"]:
            return (f"invocation limit reached "
                    f"({c['agent_invocations']}/{lim['max_agent_invocations']})")
        if c["consecutive_failures"] >= 2:
            return "two consecutive agent failures; a human must look at this"
        if st["state"] == "ANALYSIS_PLANNING" and exp["version"] > 1 and \
                c["plan_revisions"] >= lim["max_plan_revisions"]:
            return f"plan revision limit reached ({lim['max_plan_revisions']})"
        if st["state"] == "IMPLEMENTING" and exp["version"] > 1 and \
                c["impl_revisions"] >= lim["max_impl_revisions"]:
            return f"implementation revision limit reached ({lim['max_impl_revisions']})"
        if exp["artifact_type"] in S.REVIEW_TYPES and \
                c["review_cycles"] >= lim["max_review_cycles"]:
            return f"review cycle limit reached ({lim['max_review_cycles']})"
        if b["tokens_consumed_estimate"] >= b["token_budget"]:
            return (f"token budget exhausted "
                    f"({b['tokens_consumed_estimate']}/{b['token_budget']})")
        ceiling = policy.cost_ceiling(st["policy"]["snapshot"])
        if ceiling and b.get("cost_usd_estimate", 0) >= ceiling:
            return (f"cost ceiling reached "
                    f"(${b['cost_usd_estimate']:.2f}/${ceiling:.2f})")
        deadline = S.datetime.datetime.strptime(
            b["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=S.datetime.timezone.utc)
        if S.datetime.datetime.now(S.datetime.timezone.utc) >= deadline:
            return f"wall-clock deadline {b['deadline']} passed"
        return None

    def _build_prompt(self, st: dict, exp: dict, extra: str = "") -> str:
        wf_id = st["workflow"]["id"]
        run_dir = f".workflow/runs/{wf_id}"
        prof = st["policy"]["snapshot"]
        request = (S.workflow_dir(self.root, wf_id) / "request.md").read_text(
            encoding="utf-8")

        inputs = [f"- {run_dir}/request.md — the original request"]
        for atype, label in (("repo_analysis", "the repository analysis"),
                             ("classification", "the classification"),
                             ("plan", "the approved plan"),
                             ("implementation", "the implementation manifest"),
                             ("review", "the review you must respond to")):
            version = st["current"].get(atype, 0)
            if version >= 1 and atype != exp["artifact_type"]:
                inputs.append(f"- {run_dir}/"
                              f"{S.artifact_relpath(atype, version)} — {label}")
        if st["current"].get("implementation", 0) >= 1 and \
                st["state"] in ("REVIEW",):
            impl = st["current"]["implementation"]
            inputs.append(f"- {run_dir}/artifacts/implementation_v{impl}/diff.patch "
                          f"— the diff you are reviewing")
            inputs.append(f"- {run_dir}/artifacts/implementation_v{impl}/"
                          f"test_output.txt — the test run it reports")

        outputs = []
        for spec in exp["artifacts"]:
            tag = "REQUIRED" if spec["required"] else "optional (reuse the " \
                                                     "accepted version if it still holds)"
            outputs.append(f"- {spec['path']} — {tag}")

        impl_version = st["current"].get("implementation", 0)
        values = {
            "WORKFLOW_ID": wf_id,
            "STATE": st["state"],
            "RUN_DIR": run_dir,
            "REQUEST": request.strip(),
            "INPUTS": "\n".join(inputs),
            "OUTPUTS": "\n".join(outputs),
            "VERSION": str(exp["version"]),
            "EXTRA": extra,
            "T1": str(prof["routing"]["t1"]),
            "T2": str(prof["routing"]["t2"]),
            "PLAN_REF": S.artifact_relpath("plan", st["current"].get("plan", 1)),
            "SUBJECT_REF": S.artifact_relpath("implementation", impl_version)
            if impl_version else "",
            "IMPL_DIR": f"{run_dir}/artifacts/implementation_v{exp['version']}"
            if st["state"] == "IMPLEMENTING" else "",
        }
        return runner.build_prompt(exp["agent"], values)

    def _agent_stage(self, wf_id: str, st: dict) -> StepOutcome:
        exp = S.expected(self.root, st)
        blocked = self._limits_preflight(st, exp)
        if blocked:
            return self._escalate(wf_id, "limit", blocked)

        stage_key = f"{st['state']}:{exp['version']}"
        attempt = self._stage_retries.get(stage_key, 0)
        extra = self._stage_retries.get(stage_key + ":reason", "")

        self.console.rule(f"{st['state']} — {exp['agent']} "
                          f"(v{exp['version']}, {exp['model'] or 'default model'})")
        for spec in exp["artifacts"]:
            self.console.say(f"   → {spec['path']}"
                             f"{'' if spec['required'] else '  (optional)'}")

        spec = runner.InvocationSpec(
            role=exp["agent"], model=exp["model"],
            prompt=self._build_prompt(st, exp, extra),
            cwd=str(self.root), workflow_id=wf_id,
            timeout_sec=st["limits"]["per_invocation_timeout_min"] * 60,
            allowed_tools=runner.TOOLS_FOR_ROLE.get(exp["agent"], []),
        )

        # Open the single-flight lock exactly as the PreToolUse gate used to.
        st["open_invocation"] = {
            "agent": exp["agent"], "started_at": S.utcnow(),
            "expected_artifact": exp["path"],
            "expected_type": exp["artifact_type"],
            "version": exp["version"], "retries": attempt,
        }
        st["counters"]["agent_invocations"] += 1
        self.save(wf_id, st)
        self.log(wf_id, "invocation_requested", agent=exp["agent"],
                 expected=exp["path"], attempt=attempt + 1,
                 invocations=st["counters"]["agent_invocations"],
                 actor="orchestrator")

        result = self.runner.invoke(spec)

        # Re-read: the child session's hooks may have written state under us.
        _, st = self.load()
        self._account(wf_id, st, exp["agent"], result)
        st["open_invocation"] = None
        st["last_invocation"] = {"agent": exp["agent"], "closed_at": S.utcnow(),
                                 "errored": not result.ok,
                                 "tokens": result.tokens,
                                 "expected_type": exp["artifact_type"],
                                 "version": exp["version"]}
        self.save(wf_id, st)

        if not result.ok:
            st["counters"]["consecutive_failures"] += 1
            self.save(wf_id, st)
            self.log(wf_id, "invocation_failed", agent=exp["agent"],
                     tokens=result.tokens, error=result.error[:500])
            return self._retry_or_escalate(
                wf_id, st, stage_key, attempt,
                f"the {exp['agent']} session failed: {result.error[:300]}")

        self.log(wf_id, "invocation_completed", agent=exp["agent"],
                 tokens=result.tokens, estimated=not result.exact,
                 cost_usd=result.cost_usd, expected=exp["path"])

        # Dynamic governance proof: did the hooks fire inside that session?
        if not ledger.hook_event_since_last_transition(self.root, wf_id):
            reason = ("the agent session left no governance-hook event in the "
                      "ledger, so its writes were not gated. Refusing to accept "
                      "the artifact.")
            if not self.allow_ungoverned:
                return self._escalate(wf_id, "integrity_violation", reason)
            self.log(wf_id, "governance_liveness_bypassed", reason=reason,
                     by=self.by, phase="invocation")

        res = accept.accept_stage(self.root, st, exp, actor="orchestrator")
        if res.outcome == "blocked":
            self.save(wf_id, st)
            self.log(wf_id, res.event, **res.extra)
            return self._escalate(wf_id, "agent_blocked", res.reason)
        if not res.accepted:
            st["counters"]["consecutive_failures"] += 1
            self.save(wf_id, st)
            self.log(wf_id, res.event or "artifact_rejected",
                     **{k: v for k, v in res.extra.items() if k != "errors"},
                     reason=res.reason[:500])
            if res.outcome == "integrity_hold":
                return self._escalate(wf_id, "integrity_violation", res.reason)
            return self._retry_or_escalate(wf_id, st, stage_key, attempt,
                                           res.reason)

        st["counters"]["consecutive_failures"] = 0
        self.save(wf_id, st)
        self._stage_retries.pop(stage_key, None)
        self._stage_retries.pop(stage_key + ":reason", None)
        for entry in res.extra.get("accepted", []):
            self.console.say(f"   ✔ accepted {entry['path']}")

        S.transition(self.root, self._next_state_after(st),
                     trigger=exp["primary"]["run_rel"])
        return StepOutcome()

    def _account(self, wf_id, st, role, result) -> None:
        b = st["budget"]
        b["tokens_consumed_estimate"] += result.tokens
        b["usage_by_role"][role] = b["usage_by_role"].get(role, 0) + result.tokens
        model = st["policy"]["snapshot"]["models"].get(role, {}).get("model")
        cost = result.cost_usd or ledger.est_cost(model, result.tokens)
        b["cost_usd_estimate"] = round(b["cost_usd_estimate"] + cost, 4)

        prof = st["policy"]["snapshot"]
        soft = b["token_budget"] * prof["budget"]["soft_threshold_pct"] // 100
        if b["tokens_consumed_estimate"] >= soft and not st["flags"]["budget_warning"]:
            st["flags"]["budget_warning"] = True
            self.log(wf_id, "budget_warning",
                     consumed=b["tokens_consumed_estimate"],
                     budget=b["token_budget"])
            self.console.say(f"   ⚠ token budget {prof['budget']['soft_threshold_pct']}% "
                             f"consumed ({b['tokens_consumed_estimate']}/"
                             f"{b['token_budget']})")

    def _retry_or_escalate(self, wf_id, st, stage_key, attempt, reason) -> StepOutcome:
        """One retry, with the exact complaint appended to the prompt.

        The validator's message names one section or one table row, so the retry
        is a small correction rather than the whole artifact regenerated — which
        is the entire reason the artifacts are Markdown.
        """
        self.console.say(f"   ✖ {reason}")
        if attempt >= MAX_STAGE_RETRIES:
            return self._escalate(wf_id, "agent_blocked",
                                  f"stage failed twice: {reason}")
        self._stage_retries[stage_key] = attempt + 1
        self._stage_retries[stage_key + ":reason"] = (
            "## Correction required\n\n"
            "Your previous attempt was rejected. Fix exactly this and rewrite "
            "the artifact — nothing else about it needs to change:\n\n"
            f"{reason}\n")
        self.console.say("   ↻ retrying once with the rejection reason attached")
        return StepOutcome()

    def _escalate(self, wf_id, reason_code, detail) -> StepOutcome:
        self.console.say(f"   ⚠ escalating: {detail}")
        _, st = self.load()
        if st.get("open_invocation"):
            st["open_invocation"] = None
            self.save(wf_id, st)
            self.log(wf_id, "invocation_cleared", actor="orchestrator",
                     reason="escalation")
        S.transition(self.root, "ESCALATED", trigger="orchestrator",
                     reason=reason_code, detail=str(detail)[:1000])
        return StepOutcome()

    # -- human gates --------------------------------------------------------- #

    def _briefing(self, wf_id, st, gate) -> str:
        run = S.workflow_dir(self.root, wf_id)
        out = []
        if gate == "plan":
            plan = S.load_artifact(self.root, st, "plan") or {}
            cls = S.load_artifact(self.root, st, "classification") or {}
            out.append(NO_INDEPENDENT_REVIEW_NOTICE)
            out.append("")
            out.append(f"route: {cls.get('route', st['route'])}    "
                       f"flags: {', '.join(cls.get('override_flags')) or 'none'}    "
                       f"score: {cls.get('total', '?')}")
            out.append("")
            out.append("OBJECTIVE")
            out.append("  " + _truncate(plan.get("objective", ""), 8))
            out.append("")
            out.append("FILES IN SCOPE (the Implementor's write allowlist)")
            for f in plan.get("files", []):
                out.append(f"  {f['action']:<7} {f['path']}")
            out.append("")
            out.append("MILESTONES")
            for m in plan.get("milestones", []):
                out.append(f"  {m['id']}: {m['description']}")
            out.append("")
            out.append("ACCEPTANCE CRITERIA")
            for ac in plan.get("acceptance_criteria", []):
                out.append(f"  {ac['id']}: {ac['criterion']} "
                           f"[{ac['verification_method']}]")
            if plan.get("scope_out"):
                out.append("")
                out.append("EXPLICITLY OUT OF SCOPE")
                out += [f"  - {s}" for s in plan["scope_out"]]
            if plan.get("risks"):
                out.append("")
                out.append("RISKS")
                for r in plan["risks"]:
                    out.append(f"  [{r['likelihood']}/{r['impact']}] {r['risk']}"
                               f" → {r['mitigation']}")
            out.append("")
            out.append(f"full plan: {run.relative_to(self.root)}/"
                       f"{S.artifact_relpath('plan', st['current']['plan'])}")

        elif gate == "final":
            review = S.load_artifact(self.root, st, "review") or {}
            out.append(f"the Reviewer's verdict is ADVISORY: "
                       f"{review.get('verdict', '?').upper()}")
            out.append("")
            out.append("SUMMARY")
            out.append("  " + _truncate(review.get("summary", ""), 12))
            out.append("")
            out.append("FINDINGS")
            if not review.get("findings"):
                out.append("  (none reported)")
            for f in review.get("findings", []):
                out.append(f"  [{f['severity']:<7}] {f['id']} {f['category']} "
                           f"— {f['location']}")
                out.append(f"      {f['description']}")
                out.append(f"      → {f['recommendation']}")
            out.append("")
            out.append("ACCEPTANCE CRITERIA")
            for ac in review.get("acceptance_criteria", []):
                out.append(f"  {ac['id']}: {ac['status']}  {ac.get('evidence', '')}")
            if review.get("checked"):
                out.append("")
                out.append("SECURITY CHECKLIST")
                for c in review["checked"]:
                    out.append(f"  {c['area']}: {c['evidence']}")

            impl = st["current"].get("implementation", 0)
            patch = run / f"artifacts/implementation_v{impl}/diff.patch"
            out.append("")
            out.append("DIFF")
            if patch.exists():
                out.append(_truncate(patch.read_text(encoding="utf-8", errors="replace"),
                                     200, f"; full diff at {patch.relative_to(self.root)}"))
            else:
                out.append(f"  (no diff.patch at {patch.relative_to(self.root)})")

        elif gate == "deployment":
            prof = st["policy"]["snapshot"]
            out.append(f"finalization mode: {prof['finalization']['mode']}")
            out.append(f"tokens: {st['budget']['tokens_consumed_estimate']:,}    "
                       f"est. cost: ${st['budget']['cost_usd_estimate']:.2f}")
            out.append("")
            out.append("This is the last gate before anything can be pushed. The "
                       "PreToolUse gate blocks `git push` and PR creation until "
                       "this approval exists.")
        return "\n".join(out)

    def _gate_subject(self, st, gate) -> str:
        return S.approval_subject(st, gate) or ""

    def _human_gate(self, wf_id, st, gate) -> StepOutcome:
        subject = self._gate_subject(st, gate)
        already, _ = accept.human_approval_satisfied(self.root, st, gate,
                                                     subject=subject or None)
        if already:
            return self._advance_after_gate(wf_id, st, gate, "approved")

        latest = S.latest_approval(st, gate)
        if latest and latest["decision"] == "changes_requested" and \
                latest.get("artifact_ref") and self._decision_is_current(
                    st, latest, subject):
            return self._advance_after_gate(wf_id, st, gate, "changes_requested")

        self.console.rule(f"HUMAN GATE — {gate}")
        self.console.say(self._briefing(wf_id, st, gate))
        self.console.say("")

        decision = self._auto_decision(wf_id, st, gate)
        if decision is None:
            choices = ("approve", "revise", "reject") if gate != "deployment" \
                else ("approve", "reject")
            decision = self.console.ask(f"Your decision on the {gate} gate", choices)
        if decision is None:
            return self._await_human(wf_id, st, gate, subject)

        notes = ""
        if decision in ("revise", "reject"):
            notes = self.console.prompt_text(
                f"Say what needs to change (this is passed to the next stage)")

        mapped = {"approve": "approved", "revise": "changes_requested",
                  "reject": "rejected"}[decision]
        res = accept.record_approval(self.root, st, gate=gate, decision=mapped,
                                     by=self.by, subject=subject, notes=notes,
                                     auto=False)
        if not res.accepted:
            return StepOutcome(True, EXIT_HALTED, f"✖ {res.reason}")
        self.save(wf_id, st)
        self.console.say(f"   ✔ recorded: {gate} {mapped} by {self.by}")
        return self._advance_after_gate(wf_id, st, gate, mapped, notes)

    def _decision_is_current(self, st, approval, subject) -> bool:
        if not subject:
            return True
        return str(approval.get("artifact_ref", "")) and \
            self._approval_subject_ref(st, approval).endswith(subject)

    def _approval_subject_ref(self, st, approval) -> str:
        rel = approval.get("artifact_ref", "")
        full = S.workflow_dir(self.root, st["workflow"]["id"]) / rel
        if not full.exists():
            return ""
        try:
            return str(json.loads(full.read_text(encoding="utf-8"))
                       .get("subject_ref", ""))
        except (OSError, ValueError):
            return ""

    def _auto_decision(self, wf_id, st, gate) -> str | None:
        """``--auto-approve``, and only where policy actually allows it.

        Two independent conditions must hold — the route must be one the system
        treats as low-risk, and the pinned profile must mark the gate 'off' for
        it. The deployment gate is a policy floor and is never eligible. Whatever
        happens is logged either way: an opt-out that is not in the audit trail
        is not an opt-out, it is a hole.
        """
        if not st.get("auto_approve") or gate == "deployment":
            return None
        allowed, why = policy.auto_approve_allowed(st["policy"]["snapshot"],
                                                   st["route"], gate)
        if not allowed:
            self.console.say(f"   --auto-approve does not apply here: {why}")
            self.log(wf_id, "auto_approve_refused", gate=gate, route=st["route"],
                     reason=why)
            return None
        subject = self._gate_subject(st, gate)
        res = accept.record_approval(self.root, st, gate=gate, decision="approved",
                                     by=f"auto-approve:{self.by}", subject=subject,
                                     notes="waived by --auto-approve", auto=True)
        if not res.accepted:
            return None
        self.save(wf_id, st)
        self.log(wf_id, "auto_approve_used", gate=gate, route=st["route"],
                 by=self.by, artifact=res.extra.get("artifact"))
        self.console.say(f"   ⚡ {gate} gate auto-approved (route={st['route']}, "
                         f"profile allows it); recorded in the decision log")
        return "approve"

    def _await_human(self, wf_id, st, gate, subject) -> StepOutcome:
        cmd = (f"  .workflow/bin/wf approve {gate} --decision approved "
               f"--by <you>" + (f" --subject {subject}" if subject else ""))
        alt = (f"  .workflow/bin/wf approve {gate} --decision changes_requested "
               f"--by <you> --notes \"...\"" if gate != "deployment" else
               f"  .workflow/bin/wf approve {gate} --decision rejected --by <you>")
        self.log(wf_id, "awaiting_human", gate=gate, state=st["state"])
        return StepOutcome(
            True, EXIT_AWAITING_HUMAN,
            f"\n⏸ waiting for a human decision on the {gate} gate.\n"
            f"  Record it:\n{cmd}\n{alt}\n"
            f"  Then continue:\n"
            f"  python3 -m gmdw.orchestrator resume")

    def _advance_after_gate(self, wf_id, st, gate, decision, notes="") -> StepOutcome:
        if decision in ("approved", "force_approved"):
            target = {"plan": "IMPLEMENTING", "final": "APPROVED",
                      "deployment": "COMPLETED"}[gate]
        elif decision == "changes_requested":
            target = {"plan": "ANALYSIS_PLANNING", "final": "IMPLEMENTING"}.get(gate)
            if target is None:
                return StepOutcome(True, EXIT_HALTED,
                                   "✖ the deployment gate has no revise path")
        else:
            target = "REJECTED"
        try:
            S.transition(self.root, target, trigger=f"{gate}_approval",
                         actor="human", detail=notes or None)
        except GmdwError as e:
            self.log(wf_id, "transition_denied", to_state=target, reason=str(e))
            return self._escalate(wf_id, "other", str(e))
        return StepOutcome()

    # -- escalation & finalization ------------------------------------------- #

    def _escalation(self, wf_id, st) -> StepOutcome:
        last = st["escalations"][-1] if st["escalations"] else {}
        if last.get("resolution", "pending") != "pending":
            target = S.ESCALATION_TARGET.get(last["resolution"])
            S.transition(self.root, target, trigger="escalation_resolution",
                         actor="human")
            return StepOutcome()

        self.console.rule("ESCALATED — human decision required")
        self.console.say(f"  reason: {last.get('reason')}")
        self.console.say(f"  detail: {last.get('detail')}")
        self.console.say(f"  from:   {last.get('from_state')}")
        self.console.say(f"  counters: {json.dumps(st['counters'])}")
        self.console.say("")
        choice = self.console.ask(
            "How should this be resolved",
            ("redirect", "override", "force_approve", "abandon"))
        if choice is None:
            self.log(wf_id, "awaiting_human", gate="escalation",
                     state="ESCALATED")
            return StepOutcome(
                True, EXIT_AWAITING_HUMAN,
                "\n⏸ waiting for an escalation decision.\n"
                "  .workflow/bin/wf approve escalation --by <you> "
                "--resolution redirect|override|force_approve|abandon\n"
                "  python3 -m gmdw.orchestrator resume")

        notes = self.console.prompt_text("Notes for the record")
        res = accept.record_approval(
            self.root, st, gate="escalation",
            decision="force_approved" if choice == "force_approve" else "approved",
            by=self.by, notes=notes, resolution=choice)
        if not res.accepted:
            return StepOutcome(True, EXIT_HALTED, f"✖ {res.reason}")
        self.save(wf_id, st)
        return StepOutcome()

    def _maybe_close(self, wf_id, st) -> None:
        if S.active_id(self.root) != wf_id:
            return
        try:
            summary = S.close_workflow(self.root, wf_id, st)
            self.console.say(f"   ✔ closed {summary['closed']} "
                             f"({summary['final_state']})")
        except GmdwError as e:
            self.console.say(f"   ✖ could not close the workflow: {e}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _root() -> Path:
    try:
        return S.find_root(os.getcwd())
    except GmdwError:
        return Path(__file__).resolve().parent.parent


def _make(args) -> Orchestrator:
    return Orchestrator(
        _root(),
        agent_runner=runner.ScriptedRunner() if getattr(args, "dry_run", False)
        else runner.ClaudeRunner(),
        console=Console(answers=getattr(args, "answer", None)),
        by=args.by, auto_approve=getattr(args, "auto_approve", False),
        allow_ungoverned=getattr(args, "allow_ungoverned", False),
        max_steps=getattr(args, "max_steps", DEFAULT_MAX_STEPS))


def cmd_run(args) -> int:
    orch = _make(args)
    ok, why = orch.preflight()
    if not ok:
        print(f"gmdw: refusing to start:\n  {why}", file=sys.stderr)
        return EXIT_HALTED
    if S.active_id(orch.root):
        print(f"gmdw: workflow {S.active_id(orch.root)} is already ACTIVE; "
              f"`resume` it or `wf close` it first", file=sys.stderr)
        return EXIT_USAGE
    try:
        wf_id = orch.start(args.request, title=args.title, profile=args.profile)
    except GmdwError as e:
        print(f"gmdw: {e}", file=sys.stderr)
        return EXIT_HALTED
    print(json.dumps({"workflow_id": wf_id, "state": "RECEIVED",
                      "run_dir": f".workflow/runs/{wf_id}"}))
    return orch.loop()


def cmd_resume(args) -> int:
    orch = _make(args)
    if not S.active_id(orch.root):
        print("gmdw: no ACTIVE workflow to resume", file=sys.stderr)
        return EXIT_USAGE
    ok, why = orch.preflight()
    if not ok:
        print(f"gmdw: refusing to resume:\n  {why}", file=sys.stderr)
        return EXIT_HALTED
    return orch.loop()


def cmd_status(args) -> int:
    root = _root()
    try:
        wf_id, st = S.load_state(root)
    except GmdwError as e:
        print(f"gmdw: {e}", file=sys.stderr)
        return EXIT_USAGE
    exp = S.expected(root, st)
    print(json.dumps({
        "workflow_id": wf_id, "state": st["state"], "route": st["route"],
        "flags": st["flags"], "counters": st["counters"], "budget": st["budget"],
        "current": st["current"], "human_approved": st.get("human_approved"),
        "auto_approve": st.get("auto_approve"),
        "waiting_for": ({"agent": exp["agent"],
                         "artifacts": [a["path"] for a in exp["artifacts"]]}
                        if exp else S.GATE_FOR_STATE.get(st["state"], "human")),
    }, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m gmdw.orchestrator",
        description="GMDW deterministic orchestrator — reads state, invokes one "
                    "agent, validates its Markdown artifacts, advances. No LLM "
                    "is consulted about routing.")
    ap.add_argument("--version", action="version", version=f"GMDW {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--by", default=os.environ.get("USER", "unknown"),
                        help="the human accountable for this run")
    common.add_argument("--answer", action="append", default=None,
                        help="pre-supply a gate answer (repeatable; for scripted "
                             "runs). Each is consumed in order.")
    common.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        dest="max_steps")
    common.add_argument("--allow-ungoverned", action="store_true",
                        dest="allow_ungoverned",
                        help="proceed even though the enforcement hooks are not "
                             "wired up (audited; for CI / manual ops)")
    common.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="walk the loop with a no-op runner: no model is "
                             "called and no artifact is produced")

    p = sub.add_parser("run", parents=[common], help="start and drive a workflow")
    p.add_argument("request", help="path to the request file, or - for stdin")
    p.add_argument("--title", default=None)
    p.add_argument("--profile", default=None, help="policy profile override")
    p.add_argument("--auto-approve", action="store_true", dest="auto_approve",
                   help="waive the plan and final human gates. Only takes effect "
                        "on a trivial-route workflow whose profile marks those "
                        "gates 'off'; the deployment gate is never waived. "
                        "Recorded in decision_log.jsonl.")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", parents=[common],
                       help="continue the ACTIVE workflow (e.g. after a human "
                            "recorded a gate decision with `wf approve`)")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("status", help="what the ACTIVE workflow is waiting for")
    p.set_defaults(fn=cmd_status)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
