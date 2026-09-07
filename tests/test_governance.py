#!/usr/bin/env python3
"""GMDW governance verification suite.

Proves every governance guarantee against a MISBEHAVING model: the tests drive
the real hooks (piping them the stdin JSON Claude Code sends), the real `wf` CLI,
and the real acceptance core against a throwaway copy of the repo, then assert
the deny/block decision, the resulting workflow state, and the audit record.

v0.3.0 adds the guarantee the old architecture could not make: **an LLM cannot
approve anything.** The `H` tests are that claim, stated several ways.

Stdlib only. Run: python3 tests/test_governance.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gmdw import __version__  # noqa: E402
from gmdw import accept, heartbeat, ledger, parsers  # noqa: E402
from gmdw import state as S  # noqa: E402
from fixtures import ArtifactBuilders, make_git_repo, make_repo_skeleton  # noqa: E402


class Base(ArtifactBuilders, unittest.TestCase):
    profile = "standard"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gmdw_test_"))
        make_repo_skeleton(self.tmp)
        make_git_repo(self.tmp)
        self.wid = S.create_workflow(self.tmp, "Test request\nbody", "Test",
                                     "tester", self.profile)
        # Seed a fresh governance heartbeat: these tests drive a workflow that is
        # meant to be governed, so the session they simulate is one where the
        # hooks fire. Without it every `wf transition` here would (correctly) be
        # refused by the mid-run liveness check. The liveness tests that care
        # remove or expire this seed themselves.
        heartbeat.touch(self.tmp, "test-fixture")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- state helpers (setup bypass; tests may seed state directly) ----------
    def load(self):
        return S.load_state(self.tmp, self.wid)

    def save(self, st):
        S.save_state(self.tmp, self.wid, st)

    def seed(self, **fields):
        _, st = self.load()
        for k, v in fields.items():
            if k in ("counters", "flags", "current", "budget", "human_approved"):
                st[k].update(v)
            else:
                st[k] = v
        self.save(st)
        return st

    # -- a workflow parked at a given state ----------------------------------
    def at_final_approval(self, verdict="approve", flags="", **review_kw):
        self.write_artifact("classification", 1,
                            self.classification_body(route="medium", flags=flags,
                                                     security=1))
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1)
        self.write_review(1, verdict=verdict, **review_kw)
        self.seed(state="FINAL_APPROVAL", route="medium",
                  flags={"security": bool(flags)})

    # -- hook / cli drivers ---------------------------------------------------
    def hook(self, script, payload, env=None):
        payload = {**payload, "cwd": str(self.tmp)}
        p = subprocess.run([sys.executable,
                            str(self.tmp / ".claude" / "hooks" / script)],
                           input=json.dumps(payload), capture_output=True,
                           text=True, cwd=str(self.tmp),
                           env={**os.environ, **(env or {})} if env else None)
        out = p.stdout.strip()
        return json.loads(out) if out else None

    def pretool(self, tool, tool_input, env=None):
        return self.hook("pretooluse_gate.py",
                         {"tool_name": tool, "tool_input": dict(tool_input)}, env)

    def subagentstop(self):
        return self.hook("subagentstop_validate.py", {"tool_name": "SubagentStop"})

    def posttool(self, tool, tool_input):
        return self.hook("posttooluse_ledger.py",
                         {"tool_name": tool, "tool_input": tool_input})

    def wf(self, *args, env=None):
        e = {**os.environ, **(env or {})} if env else None
        return subprocess.run([sys.executable,
                               str(self.tmp / ".workflow" / "bin" / "wf"), *args],
                              capture_output=True, text=True, cwd=str(self.tmp),
                              env=e)

    def approve(self, gate, decision="approved", by="human", **kw):
        _, st = self.load()
        res = accept.record_approval(self.tmp, st, gate=gate, decision=decision,
                                     by=by,
                                     subject=kw.pop("subject", None)
                                     if "subject" in kw
                                     else (S.approval_subject(st, gate) or ""),
                                     **kw)
        self.save(st)
        return res

    def events(self):
        return [e.get("event") for e in ledger.read_events(self.tmp, self.wid)]

    def assertDenied(self, decision, needle=None):
        self.assertIsNotNone(decision, "expected a deny, got allow")
        hso = decision.get("hookSpecificOutput", {})
        self.assertEqual(hso.get("permissionDecision"), "deny",
                         f"expected deny, got {decision}")
        if needle:
            self.assertIn(needle, hso.get("permissionDecisionReason", ""))

    def assertAllowed(self, decision):
        self.assertIsNone(decision, f"expected allow, got deny: {decision}")

    def assertBlocked(self, decision, needle=None):
        self.assertIsNotNone(decision, "expected a block, got accept")
        self.assertEqual(decision.get("decision"), "block", f"got {decision}")
        if needle:
            self.assertIn(needle, decision.get("reason", ""))


# =========================================================================== #
# Isolation / write-scope
# =========================================================================== #

class TestWriteScope(Base):

    def test_01_planner_source_write_denied(self):
        self.seed(state="ANALYSIS_PLANNING")
        d = self.pretool("Write", {"file_path": "src/app.py", "content": "x"})
        self.assertDenied(d, "source writes are not permitted in state ANALYSIS_PLANNING")

    def test_02_reviewer_source_write_denied(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.seed(state="REVIEW")
        d = self.pretool("Write", {"file_path": "src/app.py", "content": "x"})
        self.assertDenied(d, "source writes are not permitted in state REVIEW")

    def test_03_out_of_scope_edit_denied(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body(files=("src/app.py",)))
        self.seed(state="IMPLEMENTING", route="full")
        d = self.pretool("Write", {"file_path": "src/other.py", "content": "x"})
        self.assertDenied(d, "outside the approved plan")

    def test_03b_trivial_route_is_also_plan_scoped(self):
        # v0.2 let the trivial route write anywhere because it had no plan. The
        # 3-agent model always produces one, so the allowlist always applies.
        self.write_artifact("classification", 1,
                            self.classification_body(route="trivial", flags=""))
        self.write_artifact("plan", 1, self.plan_body(files=("src/app.py",)))
        self.seed(state="IMPLEMENTING", route="trivial")
        self.assertDenied(self.pretool("Write", {"file_path": "src/other.py",
                                                 "content": "x"}),
                          "outside the approved plan")

    def test_P2_planned_write_allowed(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body(files=("src/app.py",)))
        self.seed(state="IMPLEMENTING", route="full")
        self.assertAllowed(self.pretool("Write",
                           {"file_path": "src/app.py", "content": "x"}))
        self.assertAllowed(self.pretool("Write",
                           {"file_path": "tests/test_new.py", "content": "x"}))

    def test_analyst_planner_may_write_all_three_artifacts(self):
        # The stage that produces three artifacts needs an allowlist, not a
        # single expected path.
        self.seed(state="ANALYSIS_PLANNING")
        run = f".workflow/runs/{self.wid}/artifacts"
        for name in ("plan_v1.md", "classification_v1.md", "repo_analysis_v1.md"):
            self.assertAllowed(self.pretool("Write", {"file_path": f"{run}/{name}",
                                                      "content": "x"}))
        self.assertDenied(self.pretool("Write", {"file_path": f"{run}/review_v1.md",
                                                 "content": "x"}),
                          "the writable artifacts are")

    def test_protected_paths_denied(self):
        self.seed(state="IMPLEMENTING", route="trivial")
        for p in (".workflow/runs/%s/state.json" % self.wid,
                  ".claude/hooks/pretooluse_gate.py",
                  ".workflow/policies/standard.json",
                  ".claude/settings.json",
                  ".workflow/.hooks_heartbeat",
                  "gmdw/orchestrator.py",      # new in v0.3.0
                  "gmdw/state.py"):
            self.assertDenied(self.pretool("Write", {"file_path": p, "content": "x"}),
                              "governance-protected")

    def test_frozen_artifact_is_immutable(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.seed(state="ANALYSIS_PLANNING", current={"classification": 0})
        rel = f".workflow/runs/{self.wid}/artifacts/classification_v1.md"
        self.assertDenied(self.pretool("Write", {"file_path": rel, "content": "x"}),
                          "immutable")

    def test_stale_role_session_is_denied(self):
        # A child session started for one stage must not act in another.
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="IMPLEMENTING", route="full")
        self.assertDenied(
            self.pretool("Write", {"file_path": "src/app.py", "content": "x"},
                         env={"GMDW_ROLE": "reviewer"}),
            "belongs to 'implementor'")


# =========================================================================== #
# Loop prevention / limits
# =========================================================================== #

class TestLimits(Base):
    """Limits are enforced in two places now: the orchestrator refuses to START
    an invocation that would exceed one, and the guard refuses the transition.
    Both are deterministic Python; the difference is only which one you hit
    first."""

    def orchestrator(self, **kw):
        from gmdw import orchestrator, runner
        return orchestrator.Orchestrator(
            self.tmp, agent_runner=runner.ScriptedRunner(),
            console=orchestrator.Console(answers=[], interactive=False), **kw)

    def test_04_plan_revision_limit(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="PLAN_APPROVAL",
                  counters={"plan_revisions": 2})  # == max_plan_revisions
        self.approve("plan", "changes_requested")
        r = self.wf("transition", "ANALYSIS_PLANNING", "--trigger", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("revision limit", r.stderr)

    def test_05_invocation_limit(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        _, st = self.load()
        self.seed(state="IMPLEMENTING",
                  counters={"agent_invocations":
                            st["limits"]["max_agent_invocations"]})
        _, st = self.load()
        why = self.orchestrator()._limits_preflight(st, S.expected(self.tmp, st))
        self.assertIn("invocation limit reached", why or "")

    def test_06_budget_exhaustion(self):
        self.write_artifact("classification", 1, self.classification_body())
        _, st = self.load()
        self.seed(state="ANALYSIS_PLANNING",
                  budget={"tokens_consumed_estimate":
                          st["budget"]["token_budget"]})
        _, st = self.load()
        why = self.orchestrator()._limits_preflight(st, S.expected(self.tmp, st))
        self.assertIn("token budget exhausted", why or "")

    def test_06b_cost_ceiling(self):
        # The profile's cost ceiling was advisory in v0.2; the orchestrator now
        # actually stops on it.
        self.write_artifact("classification", 1, self.classification_body())
        _, st = self.load()
        self.seed(state="ANALYSIS_PLANNING",
                  budget={"cost_usd_estimate":
                          st["budget"]["cost_ceiling_usd"] + 0.5})
        _, st = self.load()
        why = self.orchestrator()._limits_preflight(st, S.expected(self.tmp, st))
        self.assertIn("cost ceiling reached", why or "")

    def test_06c_deadline(self):
        self.seed(state="ANALYSIS_PLANNING",
                  budget={"deadline": "2020-01-01T00:00:00Z"})
        _, st = self.load()
        why = self.orchestrator()._limits_preflight(st, S.expected(self.tmp, st))
        self.assertIn("deadline", why or "")

    def test_10_review_cycle_limit(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1)
        self.seed(state="REVIEW", counters={"review_cycles": 6})  # == max
        _, st = self.load()
        why = self.orchestrator()._limits_preflight(st, S.expected(self.tmp, st))
        self.assertIn("review cycle limit reached", why or "")

    def test_single_flight(self):
        self.seed(state="ANALYSIS_PLANNING",
                  open_invocation={"agent": "analyst-planner", "version": 1,
                                   "expected_type": "plan"})
        _, st = self.load()
        why = self.orchestrator()._limits_preflight(st, S.expected(self.tmp, st))
        self.assertIn("single-flight", why or "")


# =========================================================================== #
# Artifact integrity
# =========================================================================== #

class TestArtifacts(Base):

    def test_07_missing_artifact(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="IMPLEMENTING",
                  last_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1},
                  open_invocation=None)
        d = self.subagentstop()
        self.assertBlocked(d, "expected artifact not produced")
        _, st = self.load()
        self.assertEqual(st["counters"]["consecutive_failures"], 1)

    def test_08_invalid_artifact_names_the_defect(self):
        # The whole point of the Markdown format: a rejection is a one-line
        # correction, not "regenerate the blob".
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body(),
                            freeze=False, set_pointer=False)
        body = self.plan_body().replace("## Risk Mitigations", "## Risks")
        self.write_artifact("plan", 1, body, freeze=False, set_pointer=False)
        self.seed(state="ANALYSIS_PLANNING",
                  open_invocation={"agent": "analyst-planner",
                                   "expected_type": "plan", "version": 1})
        d = self.subagentstop()
        self.assertBlocked(d, "missing required section '## Risk Mitigations'")
        self.assertEqual(self.load()[1]["current"]["plan"], 0)

    def test_P3_valid_artifact_accepted(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        rel, _ = self.write_implementation(1, set_pointer=False, freeze=False)
        self.seed(state="IMPLEMENTING", route="medium",
                  current={"implementation": 0},
                  last_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1})
        d = self.subagentstop()
        self.assertIsNone(d, f"expected accept, got {d}")
        _, st = self.load()
        self.assertEqual(st["current"]["implementation"], 1)
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel))

    def test_P4_stage_accepts_all_three_analyst_artifacts(self):
        # The Analyst-Planner writes three artifacts in one invocation, so
        # acceptance is per-stage. All three pointers must advance together.
        for atype, body in (("repo_analysis", self.repo_analysis_body()),
                            ("classification", self.classification_body()),
                            ("plan", self.plan_body())):
            self.write_artifact(atype, 1, body, freeze=False, set_pointer=False)
        self.seed(state="ANALYSIS_PLANNING",
                  current={"plan": 0, "classification": 0, "repo_analysis": 0})
        _, st = self.load()
        res = accept.accept_stage(self.tmp, st, S.expected(self.tmp, st))
        self.assertTrue(res.accepted, res.reason)
        self.save(st)
        for atype in ("plan", "classification", "repo_analysis"):
            self.assertEqual(st["current"][atype], 1, atype)
            self.assertTrue(ledger.is_frozen(self.tmp, self.wid,
                                             S.artifact_relpath(atype, 1)))

    def test_P4b_a_rejected_required_artifact_leaves_the_pointer_unmoved(self):
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body(),
                            freeze=False, set_pointer=False)
        self.write_artifact("classification", 1, self.classification_body(),
                            freeze=False, set_pointer=False)
        self.write_artifact("plan", 1,
                            self.plan_body().replace("## Objective", "## Goal"),
                            freeze=False, set_pointer=False)
        self.seed(state="ANALYSIS_PLANNING",
                  current={"plan": 0, "classification": 0, "repo_analysis": 0})
        _, st = self.load()
        res = accept.accept_stage(self.tmp, st, S.expected(self.tmp, st))
        self.assertFalse(res.accepted)
        self.assertEqual(st["current"]["plan"], 0)

    def test_P5_second_invocation_validated_not_skipped(self):
        # Regression: after an invocation completes, last_invocation can be a
        # stale record without an expected_type. SubagentStop must validate
        # against the ACTIVE open_invocation, not that stale record.
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        rel, _ = self.write_implementation(1, set_pointer=False, freeze=False)
        self.seed(state="IMPLEMENTING", route="medium",
                  current={"implementation": 0},
                  last_invocation={"agent": "implementor", "closed_at": S.utcnow(),
                                   "errored": None, "tokens": 100},
                  open_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1,
                                   "expected_artifact": rel})
        self.assertIsNone(self.subagentstop())
        _, st = self.load()
        self.assertEqual(st["current"]["implementation"], 1)
        self.assertIsNone(st["open_invocation"])

    def test_P6_in_invocation_correction_revalidated(self):
        # After a rejection, open_invocation must survive so the agent's
        # corrected artifact is re-validated on its next stop.
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body())
        rel = S.artifact_relpath("plan", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        open_inv = {"agent": "analyst-planner", "expected_type": "plan",
                    "version": 1, "expected_artifact": rel}
        full.write_text(self.envelope("plan", 1) +
                        self.plan_body().replace("## Objective", "## Goal"))
        self.seed(state="ANALYSIS_PLANNING", current={"plan": 0},
                  open_invocation=open_inv,
                  last_invocation={"agent": "analyst-planner",
                                   "closed_at": S.utcnow(), "errored": None})
        self.assertBlocked(self.subagentstop(), "## Objective")
        _, st = self.load()
        self.assertEqual(st["current"]["plan"], 0)
        self.assertIsNotNone(st["open_invocation"],
                             "open_invocation must survive a rejection")
        full.write_text(self.envelope("plan", 1) + self.plan_body())
        self.assertIsNone(self.subagentstop())
        _, st = self.load()
        self.assertEqual(st["current"]["plan"], 1)
        self.assertEqual(st["counters"]["consecutive_failures"], 0)

    def test_09_tampered_artifact(self):
        rel, full = self.write_artifact("plan", 1, self.plan_body())
        self.assertEqual(ledger.verify(self.tmp, self.wid), [])
        os.chmod(full, 0o644)
        full.write_text("tampered")
        bad = ledger.verify(self.tmp, self.wid)
        self.assertTrue(bad and bad[0]["problem"] == "hash_mismatch")
        # the APPROVED->COMPLETED tamper guard and `wf close` both refuse
        self.seed(state="APPROVED", route="medium")
        self.approve("deployment")
        r = self.wf("transition", "COMPLETED", "--trigger", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tamper", r.stderr)
        self.assertEqual(self.wf("close").returncode, 1)

    def test_scope_violation_in_manifest_rejected(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body(files=("src/app.py",)))
        self.write_implementation(1, set_pointer=False, freeze=False,
                                  changes=("src/rogue.py | modified | sneaky",))
        self.seed(state="IMPLEMENTING", route="medium",
                  current={"implementation": 0},
                  open_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1})
        self.assertBlocked(self.subagentstop(), "outside the plan's")

    def test_rework_must_address_blocker_findings(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1)
        self.write_review(1, verdict="revise",
                          findings=["F1 | blocker | correctness | src/app.py | "
                                    "wrong | fix it"])
        self.write_implementation(2, set_pointer=False, freeze=False)
        self.seed(state="IMPLEMENTING", route="medium",
                  open_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 2})
        self.assertBlocked(self.subagentstop(), "Findings Addressed")


# =========================================================================== #
# Security / deployment / integrity
# =========================================================================== #

class TestSecurityAndDeploy(Base):

    def test_11_security_flagged_review_needs_a_checklist(self):
        # v0.2 routed a flagged workflow through a separate security-reviewer.
        # The unified Reviewer keeps the guarantee: no checklist, no acceptance.
        self.write_artifact("classification", 1,
                            self.classification_body(flags="security"))
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1)
        self.write_review(1, set_pointer=False, freeze=False, checklist=False)
        self.seed(state="REVIEW", route="full", flags={"security": True},
                  current={"review": 0},
                  open_invocation={"agent": "reviewer", "expected_type": "review",
                                   "version": 1})
        self.assertBlocked(self.subagentstop(), "Security Checklist")

    def test_11b_flagged_workflow_cannot_reach_approved_without_a_checklist(self):
        self.at_final_approval(flags="security")
        # rewrite the review without a checklist, bypassing acceptance entirely
        rel = S.artifact_relpath("review", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        os.chmod(full, 0o644)
        full.write_text(self.envelope("review", 1,
                                      extra="subject_ref: artifacts/"
                                            "implementation_v1/manifest.md\n") +
                        self.review_body(checklist=False))
        self.approve("final")
        _, st = self.load()
        ok, why = S.guard(self.tmp, st, "APPROVED")
        self.assertFalse(ok)
        self.assertIn("security checklist", why)

    def test_12a_complete_without_deploy_denied(self):
        self.seed(state="APPROVED", route="medium")
        r = self.wf("transition", "COMPLETED", "--trigger", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("deployment approval required", r.stderr)

    def test_12b_push_without_deploy_denied(self):
        self.seed(state="APPROVED")
        self.assertDenied(self.pretool("Bash", {"command": "git push origin main"}),
                          "deployment approval")

    def test_12c_push_after_deploy_allowed(self):
        self.seed(state="APPROVED")
        self.approve("deployment")
        self.assertAllowed(self.pretool("Bash", {"command": "git push origin main"}))

    def test_13_false_test_pass_integrity(self):
        self.write_artifact("classification", 1,
                            self.classification_body(route="medium", flags=""))
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1, set_pointer=False, freeze=False, result="pass",
                                  command='python3 -c "import sys; sys.exit(1)"')
        self.seed(state="IMPLEMENTING", route="medium", flags={"security": False},
                  current={"implementation": 0},
                  last_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1})
        self.assertBlocked(self.subagentstop(), "INTEGRITY VIOLATION")
        _, st = self.load()
        self.assertTrue(st["flags"]["integrity_hold"])
        # an integrity hold leaves only ESCALATED/FAILED reachable
        ok, why = S.guard(self.tmp, st, "REVIEW")
        self.assertFalse(ok)
        self.assertIn("integrity hold", why)

    def test_13b_honest_test_pass_accepted(self):
        self.write_artifact("classification", 1,
                            self.classification_body(route="medium", flags=""))
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1, set_pointer=False, freeze=False)
        self.seed(state="IMPLEMENTING", route="medium", flags={"security": False},
                  current={"implementation": 0},
                  last_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1})
        self.assertIsNone(self.subagentstop())
        self.assertEqual(self.load()[1]["current"]["implementation"], 1)


# =========================================================================== #
# Human-in-the-loop — the v0.3.0 guarantee
# =========================================================================== #

class TestHumanGates(Base):
    """An LLM cannot approve anything. Stated several ways, because this is the
    unsound guarantee the rework exists to fix: in v0.2 a reviewer subagent that
    rubber-stamped a hallucinated implementation was sufficient for the machine
    to reach APPROVED on its own."""

    def test_H1_there_is_no_edge_from_review_to_approved(self):
        # Not a policy setting that could be misconfigured — an edge that does
        # not exist in the transition table at all.
        self.assertNotIn("APPROVED", S.TRANSITIONS["REVIEW"])
        self.assertNotIn("IMPLEMENTING", S.TRANSITIONS["ANALYSIS_PLANNING"])
        self.assertEqual({s for s, t in S.TRANSITIONS.items() if "APPROVED" in t},
                         {"FINAL_APPROVAL", "ESCALATED"})

    def test_H2_approve_verdict_alone_does_not_reach_approved(self):
        self.at_final_approval(verdict="approve")
        r = self.wf("transition", "APPROVED", "--trigger", "artifacts/review_v1.md")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("human sign-off", r.stderr)
        self.assertEqual(self.load()[1]["state"], "FINAL_APPROVAL")

    def test_H3_human_approval_opens_the_gate(self):
        self.at_final_approval(verdict="approve")
        self.approve("final", by="arun")
        r = self.wf("transition", "APPROVED", "--trigger", "artifacts/review_v1.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, st = self.load()
        self.assertEqual(st["state"], "APPROVED")
        self.assertTrue(st["human_approved"]["final"])
        ev = [e for e in ledger.read_events(self.tmp, self.wid)
              if e.get("event") == "human_approval"][-1]
        self.assertEqual(ev["gate"], "final")
        self.assertTrue(ev["human_approved"])
        self.assertFalse(ev["auto_approved"])

    def test_H4_an_approval_of_a_stale_review_does_not_count(self):
        # A human approved review v1; the implementation was then reworked and
        # re-reviewed. The old signature must not vouch for the new artifact.
        self.at_final_approval(verdict="approve")
        self.approve("final", by="arun")
        self.write_implementation(2)
        self.write_review(2, impl=2)
        self.seed(state="FINAL_APPROVAL")
        _, st = self.load()
        ok, why = S.guard(self.tmp, st, "APPROVED")
        self.assertFalse(ok, "a stale approval must not satisfy the gate")
        self.assertIn("has not been approved", why)

    def test_H5_a_tampered_approval_is_rejected(self):
        self.at_final_approval(verdict="approve")
        res = self.approve("final", by="arun")
        full = S.workflow_dir(self.tmp, self.wid) / res.extra["artifact"]
        os.chmod(full, 0o644)
        record = json.loads(full.read_text())
        record["notes"] = "edited after the fact"
        full.write_text(json.dumps(record))
        _, st = self.load()
        ok, why = S.guard(self.tmp, st, "APPROVED")
        self.assertFalse(ok)
        self.assertIn("has been modified", why)

    def test_H6_an_agent_cannot_sign_its_own_gate(self):
        self.at_final_approval()
        for cmd in ("wf approve final --decision approved --by me",
                    ".workflow/bin/wf approve plan --decision approved --by me",
                    "wf accept --by me", "wf clear-invocation --by me"):
            self.assertDenied(self.pretool("Bash", {"command": cmd}),
                              "may not sign your own gate")
        # ...without swallowing the read-only rest of the CLI
        self.assertAllowed(self.pretool("Bash", {"command": "wf status"}))
        self.assertAllowed(self.pretool("Bash", {"command": "wf expected"}))

    def test_H7_failing_acceptance_criteria_block_approval(self):
        self.at_final_approval(verdict="approve",
                               acs=["AC1 | fail | the test does not assert it"])
        self.approve("final", by="arun")
        _, st = self.load()
        ok, why = S.guard(self.tmp, st, "APPROVED")
        self.assertFalse(ok)
        self.assertIn("acceptance criteria not passing", why)

    def test_H8_force_approved_overrides_but_is_recorded_as_such(self):
        self.at_final_approval(verdict="approve",
                               acs=["AC1 | fail | not asserted"])
        self.approve("final", decision="force_approved", by="arun",
                     overridden_findings=["AC1"])
        r = self.wf("transition", "APPROVED", "--trigger", "artifacts/review_v1.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads((S.workflow_dir(self.tmp, self.wid) /
                          "artifacts/approvals/final_approval_1.json").read_text())
        self.assertEqual(rec["decision"], "force_approved")
        self.assertEqual(rec["overridden_findings"], ["AC1"])
        self.assertTrue(rec["human_approved"])

    def test_H9_plan_gate_is_the_only_way_into_implementing(self):
        self.write_artifact("classification", 1,
                            self.classification_body(route="medium", flags=""))
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="ANALYSIS_PLANNING", route="medium")
        r = self.wf("transition", "IMPLEMENTING", "--trigger", "artifacts/plan_v1.md")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in transition table", r.stderr)
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", "artifacts/plan_v1.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.wf("transition", "IMPLEMENTING", "--trigger", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no plan approval on record", r.stderr)
        self.approve("plan", by="arun")
        r = self.wf("transition", "IMPLEMENTING", "--trigger", "x")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_H10_wf_approve_defaults_the_subject_to_the_current_artifact(self):
        self.at_final_approval(verdict="approve")
        r = self.wf("approve", "final", "--decision", "approved", "--by", "arun")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["subject"], "artifacts/review_v1.md")
        r = self.wf("transition", "APPROVED", "--trigger", "x")
        self.assertEqual(r.returncode, 0, r.stderr)


# =========================================================================== #
# --auto-approve
# =========================================================================== #

class TestAutoApprove(Base):
    """The explicit opt-out. It must be narrow, refused where it does not apply,
    and always visible in the audit trail — an opt-out nobody can find later is
    not an opt-out."""

    def orchestrator(self, **kw):
        from gmdw import orchestrator, runner
        return orchestrator.Orchestrator(
            self.tmp, agent_runner=runner.ScriptedRunner(),
            console=orchestrator.Console(answers=[], interactive=False),
            auto_approve=True, by="ci", **kw)

    def test_A1_refused_on_a_non_trivial_route(self):
        self.at_final_approval(verdict="approve")
        self.seed(auto_approve=True, route="medium")
        _, st = self.load()
        self.assertIsNone(self.orchestrator()._auto_decision(self.wid, st, "final"))
        self.assertIn("auto_approve_refused", self.events())
        ok, why = S.guard(self.tmp, st, "APPROVED")
        self.assertFalse(ok)

    def test_A2_allowed_on_trivial_and_recorded(self):
        self.write_artifact("classification", 1,
                            self.classification_body(route="trivial", flags="",
                                                     security=0))
        self.write_artifact("plan", 1, self.plan_body())
        self.write_implementation(1)
        self.write_review(1)
        self.seed(state="FINAL_APPROVAL", route="trivial", auto_approve=True)
        _, st = self.load()
        self.assertEqual(self.orchestrator()._auto_decision(self.wid, st, "final"),
                         "approve")
        self.assertIn("auto_approve_used", self.events())
        _, st = self.load()
        ok, why = S.guard(self.tmp, st, "APPROVED")
        self.assertTrue(ok, why)
        rec = json.loads((S.workflow_dir(self.tmp, self.wid) /
                          "artifacts/approvals/final_approval_1.json").read_text())
        self.assertTrue(rec["auto_approved"])
        self.assertFalse(rec["human_approved"],
                         "a waiver must never claim a human looked at it")

    def test_A3_never_waives_the_deployment_gate(self):
        self.seed(state="APPROVED", route="trivial", auto_approve=True)
        _, st = self.load()
        self.assertIsNone(self.orchestrator()._auto_decision(self.wid, st,
                                                             "deployment"))
        ok, _ = S.guard(self.tmp, st, "COMPLETED")
        self.assertFalse(ok)

    def test_A4_a_strict_profile_refuses_it_outright(self):
        from gmdw import policy
        prof, _ = policy.load_profile(self.tmp, "strict")
        for gate in ("plan", "final"):
            allowed, why = policy.auto_approve_allowed(prof, "trivial", gate)
            self.assertFalse(allowed)
            self.assertIn("strict", why)

    def test_A5_a_profile_cannot_switch_a_human_gate_off(self):
        from gmdw import policy
        prof, _ = policy.load_profile(self.tmp, "standard")
        prof["gates"]["final_approval"]["full"] = "off"
        errs = policy.validate_profile(prof)
        self.assertTrue(any("never remove them" in e for e in errs))


# =========================================================================== #
# Subagent isolation
# =========================================================================== #

class TestAgentIsolation(Base):

    def test_agents_cannot_spawn_subagents(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.seed(state="ANALYSIS_PLANNING")
        d = self.pretool("Agent", {"subagent_type": "implementor",
                                   "prompt": "do the work"})
        self.assertDenied(d, "agents may not invoke subagents")

    def test_the_deny_is_audited(self):
        self.seed(state="ANALYSIS_PLANNING")
        self.pretool("Agent", {"subagent_type": "reviewer", "prompt": "x"})
        self.assertIn("gate_denied", self.events())

    def test_the_model_comes_from_policy_not_the_agent(self):
        # v0.2 checked a model the Orchestrator asked for. The orchestrator now
        # reads it from the pinned profile, so it cannot be asked for at all.
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="IMPLEMENTING")
        _, st = self.load()
        self.assertEqual(S.expected(self.tmp, st)["model"], "opus")
        self.seed(state="ANALYSIS_PLANNING")
        _, st = self.load()
        self.assertEqual(S.expected(self.tmp, st)["model"], "sonnet")


# =========================================================================== #
# Governance liveness
# =========================================================================== #

class TestGovernanceLiveness(unittest.TestCase):
    """The enforcement plane must actually be there. Two independent readings:
    *installed* (the hooks are wired up — what the orchestrator can check before
    any session exists) and *live* (a hook has fired recently — the in-session
    self-attestation)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gmdw_live_"))
        make_repo_skeleton(self.tmp)
        (self.tmp / "request.md").write_text("Do a thing\nwith details\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wf(self, *args, env=None):
        e = {**os.environ, **(env or {})}
        return subprocess.run([sys.executable,
                               str(self.tmp / ".workflow" / "bin" / "wf"), *args],
                              capture_output=True, text=True, cwd=str(self.tmp),
                              env=e)

    def drive_hook(self, tool="Bash", tool_input=None):
        payload = {"tool_name": tool,
                   "tool_input": tool_input or {"command": ".workflow/bin/wf init"},
                   "cwd": str(self.tmp)}
        subprocess.run([sys.executable,
                        str(self.tmp / ".claude" / "hooks" / "pretooluse_gate.py")],
                       input=json.dumps(payload), capture_output=True, text=True,
                       cwd=str(self.tmp))

    def write_heartbeat(self, age_sec):
        ts = (S.datetime.datetime.now(S.datetime.timezone.utc)
              - S.datetime.timedelta(seconds=age_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.tmp / heartbeat.HEARTBEAT_REL).write_text(
            json.dumps({"ts": ts, "event": "PreToolUse", "pid": 1}))

    def unwire(self):
        """Remove the SubagentStop hook from settings: the plane is incomplete."""
        p = self.tmp / ".claude" / "settings.json"
        settings = json.loads(p.read_text())
        settings["hooks"].pop("SubagentStop")
        p.write_text(json.dumps(settings))

    # -- installed (static) ---------------------------------------------------

    def test_L1_installed_detects_a_complete_wiring(self):
        ok, why = heartbeat.enforcement_installed(self.tmp)
        self.assertTrue(ok, why)

    def test_L2_installed_detects_a_missing_hook(self):
        self.unwire()
        ok, why = heartbeat.enforcement_installed(self.tmp)
        self.assertFalse(ok)
        self.assertIn("SubagentStop", why)

    def test_L3_installed_detects_a_missing_script(self):
        (self.tmp / ".claude" / "hooks" / "subagentstop_validate.py").unlink()
        ok, why = heartbeat.enforcement_installed(self.tmp)
        self.assertFalse(ok)
        self.assertIn("the file is missing", why)

    def test_L4_init_refused_when_the_plane_is_absent(self):
        self.unwire()
        (self.tmp / ".claude" / "hooks" / "posttooluse_ledger.py").unlink()
        r = self.wf("init", "--request", "request.md", "--by", "tester")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("enforcement plane is not available", r.stderr)
        self.assertFalse(S.active_id(self.tmp))

    def test_L5_init_allowed_when_wired(self):
        r = self.wf("init", "--request", "request.md", "--by", "tester")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(S.active_id(self.tmp))
        self.assertEqual(json.loads(r.stdout)["state"], "RECEIVED")

    def test_L6_bypass_env_allows_and_is_audited(self):
        self.unwire()
        (self.tmp / ".claude" / "hooks" / "posttooluse_ledger.py").unlink()
        r = self.wf("init", "--request", "request.md", "--by", "ci",
                    env={"GMDW_ALLOW_UNGOVERNED": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        wid = json.loads(r.stdout)["workflow_id"]
        log = (self.tmp / ".workflow" / "runs" / wid / "decision_log.jsonl").read_text()
        self.assertIn("governance_liveness_bypassed", log)

    # -- live (dynamic heartbeat) ---------------------------------------------

    def test_L7_hook_writes_a_live_heartbeat(self):
        self.assertFalse((self.tmp / heartbeat.HEARTBEAT_REL).exists())
        self.drive_hook()
        live, why = heartbeat.status(self.tmp)
        self.assertTrue(live, why)

    def test_L8_stale_heartbeat_is_not_live(self):
        self.write_heartbeat(age_sec=10000)
        live, why = heartbeat.status(self.tmp)
        self.assertFalse(live)
        self.assertIn("stale", why)

    def test_L9_future_dated_heartbeat_rejected(self):
        # Without an upper skew bound a single planted `ts: 2099-…` beacon would
        # vouch for liveness forever, defeating the bounded trust window.
        self.write_heartbeat(age_sec=-10 * 365 * 24 * 3600)
        live, why = heartbeat.status(self.tmp)
        self.assertFalse(live)
        self.assertIn("future", why)

    def test_L10_small_future_skew_tolerated(self):
        self.write_heartbeat(age_sec=-5)
        live, why = heartbeat.status(self.tmp)
        self.assertTrue(live, why)

    def test_L11_custom_freshness_window_env(self):
        self.write_heartbeat(age_sec=120)
        r = self.wf("doctor", "--json", env={"GMDW_HEARTBEAT_MAX_AGE_SEC": "60"})
        self.assertIn("stale", json.loads(r.stdout)["reason"] if
                      not json.loads(r.stdout)["hooks_installed"] else "stale")

    # -- doctor ---------------------------------------------------------------

    def test_L12_doctor_reports_both_readings_read_only(self):
        self.drive_hook()
        r = self.wf("doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("REGISTERED", r.stdout)
        self.assertIn("LIVE", r.stdout)
        rj = json.loads(self.wf("doctor", "--json").stdout)
        self.assertTrue(rj["hooks_installed"] and rj["hooks_live"])
        self.assertFalse(S.active_id(self.tmp), "doctor must not create a workflow")

    def test_L13_doctor_reports_a_dark_plane(self):
        self.unwire()
        (self.tmp / ".claude" / "hooks" / "posttooluse_ledger.py").unlink()
        r = self.wf("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT registered", r.stdout)

    def test_L14_version_flag_prints_package_version(self):
        r = self.wf("--version")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(__version__, r.stdout)


# =========================================================================== #
# Recovery + mid-run liveness
# =========================================================================== #

class TestRecovery(Base):
    """The hooks-dark failure modes and their human-gated recovery: a workflow
    refuses to advance (R11/R12), and the two states a dark session can strand —
    an unaccepted artifact (R1-R6) and an orphaned invocation lock (R7-R9) — are
    recoverable by a named human, but only from a session where the hooks really
    are dark (R10).

    These tests invoke the wf entry point directly, as an operator's terminal
    would. That is exactly the path the R10 gate deny leaves open."""

    def strand_plan(self, version=1):
        """A valid plan on disk that nothing ever accepted."""
        self.write_artifact("repo_analysis", version, self.repo_analysis_body(),
                            freeze=False, set_pointer=False)
        self.write_artifact("classification", version, self.classification_body(),
                            freeze=False, set_pointer=False)
        return self.write_artifact("plan", version, self.plan_body(),
                                   freeze=False, set_pointer=False)

    def go_dark(self):
        (self.tmp / heartbeat.HEARTBEAT_REL).unlink()

    def accepted_events(self):
        return [e for e in ledger.read_events(self.tmp, self.wid)
                if e.get("event") == "artifact_accepted"]

    def test_R1_accept_unstrands_a_valid_stage(self):
        rel, _ = self.strand_plan()
        self.seed(state="ANALYSIS_PLANNING",
                  current={"plan": 0, "classification": 0, "repo_analysis": 0},
                  open_invocation={"agent": "analyst-planner",
                                   "expected_type": "plan", "version": 1,
                                   "expected_artifact": rel})
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", rel)
        self.assertEqual(r.returncode, 1, "a stranded artifact must block")

        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, st = self.load()
        self.assertEqual(st["current"]["plan"], 1)
        self.assertEqual(st["current"]["classification"], 1)
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel))
        self.assertIsNone(st["open_invocation"])
        ev = self.accepted_events()[-1]
        self.assertEqual(ev["actor"], "human_recovery")
        self.assertEqual(ev["by"], "arun")
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_R2_accept_rejects_an_invalid_artifact(self):
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body(),
                            freeze=False, set_pointer=False)
        self.write_artifact("classification", 1, self.classification_body(),
                            freeze=False, set_pointer=False)
        rel, _ = self.write_artifact(
            "plan", 1, self.plan_body().replace("## Milestones", "## Steps"),
            freeze=False, set_pointer=False)
        self.seed(state="ANALYSIS_PLANNING", current={"plan": 0})
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("## Milestones", r.stderr)
        self.assertEqual(self.load()[1]["current"]["plan"], 0)
        self.assertFalse(ledger.is_frozen(self.tmp, self.wid, rel))

    def test_R3_accept_rejects_the_wrong_version(self):
        rel2, _ = self.strand_plan(version=2)
        self.seed(state="ANALYSIS_PLANNING", current={"plan": 0})
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("only that exact version", r.stderr)
        self.assertFalse(ledger.is_frozen(self.tmp, self.wid, rel2))

    def test_R4_accept_rejects_an_already_frozen_artifact(self):
        rel, _ = self.write_artifact("plan", 1, self.plan_body(), freeze=True,
                                     set_pointer=False)
        self.seed(state="ANALYSIS_PLANNING", current={"plan": 0})
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("immutable", r.stderr)

    def test_R5_accept_requires_by(self):
        self.strand_plan()
        self.seed(state="ANALYSIS_PLANNING", current={"plan": 0})
        self.assertNotEqual(self.wf("accept").returncode, 0,
                            "recovery must name a human")

    def test_R6_accept_refuses_at_a_human_gate(self):
        self.seed(state="PLAN_APPROVAL")
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("produces no artifact", r.stderr)
        self.assertIn("wf approve plan", r.stderr)

    def test_R7_clear_invocation_unblocks_escalate(self):
        self.seed(state="ANALYSIS_PLANNING", counters={"consecutive_failures": 1},
                  open_invocation={"agent": "analyst-planner",
                                   "expected_type": "plan", "version": 1,
                                   "expected_artifact":
                                       S.artifact_relpath("plan", 1)})
        # guard() checks open_invocation before it allows ESCALATED: total deadlock
        r = self.wf("transition", "ESCALATED", "--trigger", "orphan",
                    "--reason", "other")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invocation is still open", r.stderr)

        r = self.wf("clear-invocation", "--by", "arun")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, st = self.load()
        self.assertIsNone(st["open_invocation"])
        self.assertEqual(st["counters"]["consecutive_failures"], 1,
                         "releasing the lock must not reset the failure counter")
        self.assertEqual(st["current"]["plan"], 0,
                         "clearing a lock must not accept anything")
        ev = [e for e in ledger.read_events(self.tmp, self.wid)
              if e.get("event") == "invocation_cleared"]
        self.assertEqual(ev[0]["actor"], "human_recovery")
        self.assertEqual(ev[0]["by"], "arun")
        r = self.wf("transition", "ESCALATED", "--trigger", "orphan",
                    "--reason", "other")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_R8_clear_invocation_refuses_when_none(self):
        self.seed(state="ANALYSIS_PLANNING", open_invocation=None)
        before = json.dumps(self.load()[1], sort_keys=True)
        n_events = len(self.events())
        r = self.wf("clear-invocation", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open invocation", r.stderr)
        self.assertEqual(json.dumps(self.load()[1], sort_keys=True), before)
        self.assertEqual(len(self.events()), n_events,
                         "a refusal must not write a ledger event")

    def test_R9_clear_invocation_requires_by(self):
        inv = {"agent": "analyst-planner", "expected_type": "plan", "version": 1}
        self.seed(state="ANALYSIS_PLANNING", open_invocation=inv)
        self.assertNotEqual(self.wf("clear-invocation").returncode, 0)
        self.assertEqual(self.load()[1]["open_invocation"], inv)

    def test_R10_gate_denies_recovery_commands_while_active(self):
        self.seed(state="ANALYSIS_PLANNING")
        for cmd in ("wf accept --by ci",
                    ".workflow/bin/wf accept --by ci",
                    "wf clear-invocation --by ci",
                    "python3 .workflow/bin/wf clear-invocation --by ci"):
            self.assertDenied(self.pretool("Bash", {"command": cmd}),
                              "hooks-dark session")

    def test_R11_transition_refuses_when_hooks_dark(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="ANALYSIS_PLANNING")
        self.go_dark()
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", "artifacts/plan_v1.md")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("governance hooks are not firing", r.stderr)
        self.assertEqual(self.load()[1]["state"], "ANALYSIS_PLANNING")
        self.assertIn("transition_denied", self.events())

    def test_R12_transition_bypass_proceeds_and_logs(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body())
        self.write_artifact("plan", 1, self.plan_body())
        self.seed(state="ANALYSIS_PLANNING")
        self.go_dark()
        trig = "artifacts/plan_v1.md"

        # =0 must NOT bypass: the affirmative-only read fails closed.
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", trig,
                    env={"GMDW_ALLOW_UNGOVERNED": "0"})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("governance_liveness_bypassed", self.events())

        # ESCALATED is exempt: a dark session must still be able to bail out.
        r = self.wf("transition", "ESCALATED", "--trigger", trig, "--reason", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("governance_liveness_bypassed", self.events())

        self.seed(state="ANALYSIS_PLANNING")
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", trig,
                    env={"GMDW_ALLOW_UNGOVERNED": "TRUE"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("governance_liveness_bypassed", self.events())

        self.seed(state="ANALYSIS_PLANNING")
        r = self.wf("transition", "PLAN_APPROVAL", "--trigger", trig,
                    "--allow-ungoverned")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            len([e for e in self.events() if e == "governance_liveness_bypassed"]), 2)

    def test_R13_validator_error_sets_integrity_hold(self):
        self.write_artifact("classification", 1, self.classification_body())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body())
        rel, _ = self.write_artifact("plan", 1, self.plan_body(), freeze=False,
                                     set_pointer=False)
        self.seed(state="ANALYSIS_PLANNING", current={"plan": 0},
                  open_invocation={"agent": "analyst-planner",
                                   "expected_type": "plan", "version": 1,
                                   "expected_artifact": rel})
        # A deterministic fault PAST validation and inside main(): the freeze
        # step loads the registry, which is no longer JSON.
        (S.workflow_dir(self.tmp, self.wid) / "artifacts_index.json") \
            .write_text("{ not json")
        self.assertBlocked(self.subagentstop(), "validator internal error")
        _, st = self.load()
        self.assertTrue(st["flags"]["integrity_hold"])
        self.assertIn("validator_error", self.events())
        self.assertEqual(st["current"]["plan"], 0)

    def test_R14_hook_liveness_event_is_written_once_per_leg(self):
        self.seed(state="ANALYSIS_PLANNING")
        for _ in range(3):
            self.pretool("Bash", {"command": "ls"})
        self.assertEqual(self.events().count("hook_liveness"), 1)
        self.assertTrue(ledger.hook_event_since_last_transition(self.tmp, self.wid))


# =========================================================================== #
# Schema hygiene
# =========================================================================== #

class TestSchemaHygiene(unittest.TestCase):

    ALLOWED = {"$schema", "$id", "title", "description", "type", "required",
               "properties", "additionalProperties", "enum", "const", "pattern",
               "items", "minItems", "maxItems", "minimum", "maximum"}

    def test_schemas_use_only_the_supported_subset(self):
        # The stdlib-only validator implements a deliberate subset; a schema that
        # reaches past it would silently stop validating.
        for p in (REPO / ".workflow" / "schemas").glob("*.schema.json"):
            schema = json.loads(p.read_text())
            self._walk(schema, p.name)

    def _walk(self, node, name, path="$"):
        if isinstance(node, dict):
            for key, value in node.items():
                if path == "$" or key in self.ALLOWED or path.endswith(".properties"):
                    pass
                elif key not in self.ALLOWED:
                    self.fail(f"{name}{path}: unsupported keyword {key!r}")
                self._walk(value, name, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                self._walk(value, name, f"{path}[{i}]")

    def test_every_markdown_artifact_type_has_a_spec(self):
        self.assertEqual(set(parsers.ARTIFACT_SPEC),
                         set(S.ARTIFACT_TYPES))

    def test_every_agent_state_has_a_prompt_template(self):
        from gmdw import runner
        for role in S.AGENT_FOR_STATE.values():
            self.assertTrue(runner.load_prompt(role).strip(), role)

    def test_agent_definitions_match_the_roles(self):
        defined = {p.stem for p in (REPO / ".claude" / "agents").glob("*.md")}
        self.assertEqual(defined, set(S.AGENT_FOR_STATE.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
