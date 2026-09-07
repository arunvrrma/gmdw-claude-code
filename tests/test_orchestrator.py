#!/usr/bin/env python3
"""GMDW deterministic-orchestrator tests.

The claim under test is the one the v0.3.0 rework is named for: **routing is a
loop over a transition table, not a cognitive task**. So these tests drive the
real orchestrator with a scripted runner in place of the `claude` CLI — no model,
no network, no tokens — and assert that the loop reaches the right state for the
right reason.

What that buys, concretely: the whole control flow is testable. Under the v0.2
architecture the router was a prompt, and "does the Orchestrator handle a
rejected artifact correctly?" could only be answered by running it and hoping.

Stdlib only. Run: python3 tests/test_orchestrator.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gmdw import heartbeat, ledger, orchestrator, runner  # noqa: E402
from gmdw import state as S  # noqa: E402
from fixtures import ArtifactBuilders, make_git_repo, make_repo_skeleton  # noqa: E402


class Base(ArtifactBuilders, unittest.TestCase):
    profile = "standard"
    #: what the scripted agent writes at each stage; tests override per-case
    route = "medium"
    flags = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gmdw_orch_"))
        make_repo_skeleton(self.tmp)
        make_git_repo(self.tmp)
        (self.tmp / "request.md").write_text("Add a rate limiter\n")
        self.wid = None
        self.behaviour = {}     # role -> callable(spec, exp) or "skip"
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the scripted agent ---------------------------------------------------

    def _register(self, rel):
        """Mimic the PostToolUse hook: register the write and leave the
        hook-sourced ``artifact_written`` event. Skipped entirely under
        ``no_hooks`` — a session without the enforcement plane leaves no trace
        of it, which is exactly what the orchestrator checks for."""
        if self.behaviour.get("no_hooks"):
            return
        ledger.register(self.tmp, self.wid, rel)
        ledger.log(self.tmp, self.wid, "artifact_written", path=rel)

    def _write(self, rel, text):
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
        self._register(rel)
        return full

    def agent(self, spec):
        """Default behaviour: write exactly the artifacts the stage owes."""
        self.calls.append(spec)
        _, st = S.load_state(self.tmp, self.wid)
        exp = S.expected(self.tmp, st)
        override = self.behaviour.get(spec.role)
        if override == "silent":       # the agent wrote nothing
            self._liveness(spec)
            return runner.InvocationResult(ok=True, text="", tokens=100)
        if override == "error":
            return runner.InvocationResult(ok=False, error="session crashed",
                                           exit_code=1, tokens=50)
        if callable(override):
            out = override(spec, exp)
            self._liveness(spec)
            return out or runner.InvocationResult(ok=True, tokens=100)
        self.write_stage(spec.role, exp, st)
        self._liveness(spec)
        return runner.InvocationResult(ok=True, text="done", tokens=1000,
                                       exact=True, cost_usd=0.01)

    def _liveness(self, spec):
        if self.behaviour.get("no_hooks"):
            return
        ledger.log(self.tmp, self.wid, "hook_liveness", hook="PreToolUse",
                   role=spec.role)

    def write_stage(self, role, exp, st, **kw):
        v = exp["primary"]["version"]
        if role == "analyst-planner":
            for spec in exp["artifacts"]:
                body = {"repo_analysis": self.repo_analysis_body(),
                        "classification": self.classification_body(
                            route=self.route, flags=self.flags,
                            security=3 if self.flags else 1),
                        "plan": self.plan_body()}[spec["artifact_type"]]
                self._write(spec["run_rel"],
                            self.envelope(spec["artifact_type"], spec["version"])
                            + body)
        elif role == "implementor":
            rel = S.artifact_relpath("implementation", v)
            full = self._write(rel, self.envelope("implementation", v,
                                                  extra="plan_ref: artifacts/plan_v1.md\n")
                               + self.manifest_body(**kw))
            (full.parent / "diff.patch").write_text("--- a\n+++ b\n@@\n+x\n")
            (full.parent / "test_output.txt").write_text("ok\n")
        elif role == "reviewer":
            impl = st["current"]["implementation"]
            self._write(S.artifact_relpath("review", v),
                        self.envelope("review", v,
                                      extra=f"subject_ref: artifacts/"
                                            f"implementation_v{impl}/manifest.md\n")
                        + self.review_body(**kw))

    # -- driving --------------------------------------------------------------

    def orch(self, answers=(), **kw):
        # interactive=False makes the suite hermetic: when the scripted answers
        # run out the Console reports "nobody is there" instead of falling back
        # to input() and hanging on whatever stdin the test runner inherited.
        console = orchestrator.Console(answers=list(answers), interactive=False)
        o = orchestrator.Orchestrator(
            self.tmp, agent_runner=runner.ScriptedRunner(self.agent),
            console=console, by=kw.pop("by", "tester"), **kw)
        return o

    def run_workflow(self, answers=("approve", "approve", "approve"), **kw):
        o = self.orch(answers, **kw)
        self.wid = o.start(self.tmp / "request.md")
        code = o.loop()
        return o, code

    def state(self):
        return S.load_state(self.tmp, self.wid)[1]

    def events(self):
        return [e.get("event") for e in ledger.read_events(self.tmp, self.wid)]

    def roles_called(self):
        return [c.role for c in self.calls]


# =========================================================================== #

class TestHappyPath(Base):

    def test_O1_three_agents_three_gates_one_completed_workflow(self):
        o, code = self.run_workflow()
        self.assertEqual(code, orchestrator.EXIT_OK)
        st = self.state()
        self.assertEqual(st["state"], "COMPLETED")
        self.assertEqual([h["to"] for h in st["history"]],
                         ["RECEIVED", "ANALYSIS_PLANNING", "PLAN_APPROVAL",
                          "IMPLEMENTING", "REVIEW", "FINAL_APPROVAL", "APPROVED",
                          "COMPLETED"])
        self.assertEqual(self.roles_called(),
                         ["analyst-planner", "implementor", "reviewer"])
        self.assertEqual(st["counters"]["agent_invocations"], 3)
        self.assertEqual([(a["gate"], a["decision"]) for a in st["approvals"]],
                         [("plan", "approved"), ("final", "approved"),
                          ("deployment", "approved")])
        self.assertIsNone(S.active_id(self.tmp), "a finished workflow is closed")

    def test_O2_every_artifact_is_frozen_and_hashed(self):
        self.run_workflow()
        registry = json.loads((S.workflow_dir(self.tmp, self.wid) /
                               "artifacts_index.json").read_text())
        for rel in ("artifacts/plan_v1.md", "artifacts/classification_v1.md",
                    "artifacts/repo_analysis_v1.md", "artifacts/review_v1.md",
                    "artifacts/implementation_v1/manifest.md",
                    "artifacts/approvals/final_approval_1.json"):
            self.assertIn(rel, registry)
            self.assertTrue(registry[rel]["frozen"], rel)
            self.assertEqual(len(registry[rel]["sha256"]), 64)
        self.assertEqual(ledger.verify(self.tmp, self.wid), [])

    def test_O3_budget_and_cost_are_accounted_per_role(self):
        self.run_workflow()
        b = self.state()["budget"]
        self.assertEqual(b["tokens_consumed_estimate"], 3000)
        self.assertEqual(set(b["usage_by_role"]),
                         {"analyst-planner", "implementor", "reviewer"})
        self.assertGreater(b["cost_usd_estimate"], 0)

    def test_O4_the_model_for_each_stage_comes_from_the_pinned_profile(self):
        self.run_workflow()
        self.assertEqual([(c.role, c.model) for c in self.calls],
                         [("analyst-planner", "sonnet"), ("implementor", "opus"),
                          ("reviewer", "sonnet")])

    def test_O5_the_audit_trail_keeps_its_shape(self):
        self.run_workflow()
        events = self.events()
        for required in ("workflow_created", "invocation_requested",
                         "invocation_completed", "artifact_accepted",
                         "transition", "human_approval", "workflow_closed"):
            self.assertIn(required, events)
        # decision_log.jsonl stays one JSON object per line, oldest first
        raw = (S.workflow_dir(self.tmp, self.wid) / "decision_log.jsonl").read_text()
        for line in raw.splitlines():
            self.assertIn("ts", json.loads(line))


class TestRouting(Base):
    """Routing is a table lookup. These assert the table, not a model."""

    def test_O6_successors_are_deterministic(self):
        o = self.orch()
        self.wid = o.start(self.tmp / "request.md")
        for state, expected in (("ANALYSIS_PLANNING", "PLAN_APPROVAL"),
                                ("IMPLEMENTING", "REVIEW")):
            st = self.state()
            st["state"] = state
            S.save_state(self.tmp, self.wid, st)
            self.assertEqual(o._next_state_after(self.state()), expected)

    def test_O7_a_revise_verdict_still_goes_to_the_human(self):
        # The Reviewer is advisory: even 'revise' routes to FINAL_APPROVAL so a
        # person sees the findings, unless they explicitly opted out.
        o = self.orch()
        self.wid = o.start(self.tmp / "request.md")
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1)
        self.write_review(1, verdict="revise",
                          findings=["F1 | major | correctness | src/app.py | x | y"])
        st = self.state()
        st["state"] = "REVIEW"
        S.save_state(self.tmp, self.wid, st)
        self.assertEqual(o._next_state_after(self.state()), "FINAL_APPROVAL")
        st = self.state()
        st["auto_approve"] = True
        S.save_state(self.tmp, self.wid, st)
        self.assertEqual(o._next_state_after(self.state()), "IMPLEMENTING")


class TestHumanGates(Base):

    def test_O8_non_interactive_stops_at_the_gate_instead_of_hanging(self):
        # A governance gate must never be resolvable by a process waiting on a
        # closed stdin. It prints the briefing and the command, and exits.
        o, code = self.run_workflow(answers=())
        self.assertEqual(code, orchestrator.EXIT_AWAITING_HUMAN)
        self.assertEqual(self.state()["state"], "PLAN_APPROVAL")
        self.assertIn("awaiting_human", self.events())

    def test_O8b_the_plan_gate_discloses_that_nobody_else_reviewed_it(self):
        # The 3-agent consolidation removed the independent plan-reviewer. The
        # human at this gate is now the only quality check on the plan, and the
        # briefing has to say so where the decision is actually made — a caveat
        # that lives only in the docs is a caveat nobody reads.
        import io
        buf = io.StringIO()
        o = self.orch()
        o.console = orchestrator.Console(stream=buf, answers=[], interactive=False)
        self.wid = o.start(self.tmp / "request.md")
        o.loop()
        printed = buf.getvalue()
        self.assertIn("No independent model review was performed on this plan",
                      printed)
        self.assertIn("sole quality gate before implementation begins", printed)
        # ...and only at the plan gate, not pasted onto every gate
        self.assertEqual(printed.count("No independent model review"), 1)

    def test_O9_resume_continues_after_wf_approve(self):
        o, code = self.run_workflow(answers=())
        self.assertEqual(code, orchestrator.EXIT_AWAITING_HUMAN)
        r = subprocess.run([sys.executable,
                            str(self.tmp / ".workflow" / "bin" / "wf"),
                            "approve", "plan", "--decision", "approved",
                            "--by", "arun"],
                           capture_output=True, text=True, cwd=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        # a fresh orchestrator: the process holds no state the run dir does not
        o2 = self.orch(answers=("approve", "approve"))
        self.wid = S.active_id(self.tmp)
        self.assertEqual(o2.loop(), orchestrator.EXIT_OK)
        self.assertEqual(self.state()["state"], "COMPLETED")

    def test_O10_rejecting_the_plan_ends_the_workflow(self):
        o, code = self.run_workflow(answers=("reject", ""))
        self.assertEqual(self.state()["state"], "REJECTED")
        self.assertEqual(self.roles_called(), ["analyst-planner"],
                         "a rejected plan must not reach the Implementor")

    def test_O11_requesting_changes_loops_back_to_planning(self):
        o, code = self.run_workflow(
            answers=("revise", "narrow the scope", "approve", "approve", "approve"))
        self.assertEqual(code, orchestrator.EXIT_OK)
        st = self.state()
        self.assertEqual(st["state"], "COMPLETED")
        self.assertEqual(self.roles_called(),
                         ["analyst-planner", "analyst-planner", "implementor",
                          "reviewer"])
        self.assertEqual(st["counters"]["plan_revisions"], 1)
        self.assertEqual(st["current"]["plan"], 2)
        # a re-plan need not re-derive an unchanged analysis
        self.assertEqual(st["current"]["repo_analysis"], 2,
                         "the scripted agent rewrote it; the pointer follows")

    def test_O12_a_revision_run_only_requires_a_new_plan(self):
        o = self.orch()
        self.wid = o.start(self.tmp / "request.md")
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body())
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        st = self.state()
        st["state"] = "ANALYSIS_PLANNING"
        S.save_state(self.tmp, self.wid, st)
        exp = S.expected(self.tmp, self.state())
        required = {a["artifact_type"] for a in exp["artifacts"] if a["required"]}
        self.assertEqual(required, {"plan"},
                         "re-ingesting an unchanged analysis is the cost the "
                         "3-agent model exists to remove")


class TestFailureHandling(Base):

    def test_O13_a_silent_agent_is_rejected_then_retried_once(self):
        self.behaviour["analyst-planner"] = "silent"
        o, code = self.run_workflow(answers=())
        # ESCALATED with nobody at the keyboard is "waiting for a human", not a
        # crash: the run parks with an actionable message rather than blocking.
        self.assertEqual(code, orchestrator.EXIT_AWAITING_HUMAN)
        st = self.state()
        self.assertEqual(st["state"], "ESCALATED")
        self.assertEqual(self.roles_called().count("analyst-planner"), 2,
                         "exactly one retry, then escalate")
        self.assertIn("artifact_missing", self.events())

    def test_O14_a_rejected_artifact_is_retried_with_the_reason_attached(self):
        # The whole reason artifacts are Markdown: the correction is one section,
        # so the retry prompt carries the defect rather than "do it all again".
        state = {"n": 0}

        def flaky(spec, exp):
            state["n"] += 1
            body = self.plan_body()
            if state["n"] == 1:
                body = body.replace("## Risk Mitigations", "## Risks")
            for a in exp["artifacts"]:
                b = {"repo_analysis": self.repo_analysis_body(),
                     "classification": self.classification_body(route="medium",
                                                                flags="",
                                                                security=1),
                     "plan": body}[a["artifact_type"]]
                self._write(a["run_rel"],
                            self.envelope(a["artifact_type"], a["version"]) + b)

        self.behaviour["analyst-planner"] = flaky
        o, code = self.run_workflow()
        self.assertEqual(code, orchestrator.EXIT_OK)
        self.assertEqual(self.state()["state"], "COMPLETED")
        self.assertEqual(self.roles_called().count("analyst-planner"), 2)
        retry = self.calls[1].prompt
        self.assertIn("Correction required", retry)
        self.assertIn("## Risk Mitigations", retry)

    def test_O15_a_failed_session_escalates_without_accepting_anything(self):
        self.behaviour["analyst-planner"] = "error"
        o, code = self.run_workflow(answers=())
        self.assertEqual(self.state()["state"], "ESCALATED")
        self.assertEqual(self.state()["current"]["plan"], 0)
        self.assertIn("invocation_failed", self.events())

    def test_O16_a_blocked_artifact_escalates(self):
        def blocked(spec, exp):
            self._write(exp["primary"]["run_rel"],
                        self.envelope("plan", 1, status="blocked",
                                      extra="blocked_reason: the request needs a "
                                            "service that does not exist\n")
                        + "## Objective\nCannot plan this.\n")

        self.behaviour["analyst-planner"] = blocked
        o, code = self.run_workflow(answers=())
        st = self.state()
        self.assertEqual(st["state"], "ESCALATED")
        self.assertEqual(st["escalations"][-1]["reason"], "agent_blocked")
        self.assertIn("does not exist", st["escalations"][-1]["detail"])

    def test_O17_an_ungoverned_agent_session_is_refused(self):
        # The dynamic half of the liveness contract: if the child session left no
        # hook-sourced event, its writes were not gated, so its artifact is not
        # accepted no matter how valid it looks.
        self.behaviour["no_hooks"] = True
        o, code = self.run_workflow(answers=())
        st = self.state()
        self.assertEqual(st["state"], "ESCALATED")
        self.assertEqual(st["current"]["plan"], 0,
                         "an ungoverned artifact must not be accepted")
        self.assertEqual(st["escalations"][-1]["reason"], "integrity_violation")

    def test_O18_the_ungoverned_override_is_audited(self):
        self.behaviour["no_hooks"] = True
        o, code = self.run_workflow(allow_ungoverned=True)
        self.assertEqual(code, orchestrator.EXIT_OK)
        self.assertEqual(self.state()["state"], "COMPLETED")
        self.assertIn("governance_liveness_bypassed", self.events())

    def test_O19_the_loop_is_bounded(self):
        self.behaviour["analyst-planner"] = "silent"
        o = self.orch(max_steps=2)
        self.wid = o.start(self.tmp / "request.md")
        self.assertEqual(o.loop(), orchestrator.EXIT_HALTED)


class TestPreflight(Base):

    def test_O20_refuses_to_start_when_the_hooks_are_not_wired(self):
        settings = self.tmp / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        data["hooks"].pop("PreToolUse")
        settings.write_text(json.dumps(data))
        ok, why = self.orch().preflight()
        self.assertFalse(ok)
        self.assertIn("PreToolUse", why)

    def test_O21_the_override_is_explicit(self):
        settings = self.tmp / ".claude" / "settings.json"
        settings.write_text("{}")
        ok, _ = self.orch(allow_ungoverned=True).preflight()
        self.assertTrue(ok)

    def test_O22_run_refuses_a_second_concurrent_workflow(self):
        o = self.orch()
        self.wid = o.start(self.tmp / "request.md")
        with self.assertRaises(S.GmdwError):
            o.start(self.tmp / "request.md")


class TestRunner(unittest.TestCase):
    """The one place a model is called. Its job is to build an argv and read a
    usage envelope; both must be exact or the budget is fiction."""

    def test_O23_argv_carries_the_model_and_the_prompt_goes_on_stdin(self):
        r = runner.ClaudeRunner(binary="claude")
        spec = runner.InvocationSpec(role="implementor", model="opus",
                                     prompt="x" * 500_000,
                                     allowed_tools=["Read", "Write"])
        argv = r.build_argv(spec)
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--allowed-tools") + 1], "Read,Write")
        self.assertNotIn(spec.prompt, argv,
                         "a plan-sized prompt on argv would outgrow ARG_MAX")

    def test_O24_missing_binary_is_a_clean_refusal_not_a_traceback(self):
        r = runner.ClaudeRunner(binary="definitely-not-a-real-binary-xyz")
        ok, why = r.available()
        self.assertFalse(ok)
        result = r.invoke(runner.InvocationSpec(role="reviewer", model=None,
                                                prompt="hi"))
        self.assertFalse(result.ok)
        self.assertIn("not found on PATH", result.error)

    def test_O25_usage_is_read_from_the_json_envelope(self):
        tokens, cost, exact = runner._extract_usage({
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 100},
            "total_cost_usd": 0.25})
        self.assertEqual((tokens, cost, exact), (115, 0.25, True))
        self.assertEqual(runner._extract_usage({}), (0, 0.0, False))

    def test_O26_prompt_templates_render_without_choking_on_braces(self):
        # Markdown and code samples are full of braces; the template language
        # must not trip over its own payload.
        out = runner.render("$WORKFLOW_ID writes {\"a\": 1} and $MISSING",
                            {"WORKFLOW_ID": "wf_20260828_001"})
        self.assertIn('{"a": 1}', out)
        self.assertIn("$MISSING", out)

    def test_O27_every_role_template_declares_its_artifact_format(self):
        for role in ("analyst-planner", "implementor", "reviewer"):
            text = runner.load_prompt(role)
            self.assertIn("$WORKFLOW_ID", text, role)
            self.assertIn("$OUTPUTS", text, role)
            self.assertIn("---", text, role)


class TestSecurityFlagged(Base):
    route = "full"
    flags = "security"

    def test_O28_a_flagged_run_needs_the_reviewers_security_checklist(self):
        def no_checklist(spec, exp):
            st = self.state()
            self.write_stage("reviewer", exp, st, checklist=False)

        self.behaviour["reviewer"] = no_checklist
        o, code = self.run_workflow(answers=("approve",))
        st = self.state()
        self.assertEqual(st["state"], "ESCALATED")
        self.assertEqual(st["current"]["review"], 0)
        self.assertEqual(self.roles_called().count("reviewer"), 2,
                         "one retry, then escalate")

    def test_O29_the_flag_is_read_from_the_classification_artifact(self):
        o, code = self.run_workflow(answers=("approve", "approve", "approve"))
        st = self.state()
        self.assertEqual(st["route"], "full")
        self.assertTrue(st["flags"]["security"])
        self.assertEqual(st["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
