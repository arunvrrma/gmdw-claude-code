#!/usr/bin/env python3
"""GMDW governance verification suite.

Proves every governance guarantee against a MISBEHAVING model: the tests drive
the real hooks (piping them the stdin JSON Claude Code sends) and the real `wf`
CLI against a throwaway copy of the repo, then assert the deny/block decision,
the resulting workflow state, and the audit record.

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
sys.path.insert(0, str(REPO / ".claude" / "hooks"))
from gmdw import heartbeat  # noqa: E402
from gmdw import ledger  # noqa: E402
from gmdw import state as S  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


class Base(unittest.TestCase):
    profile = "standard"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gmdw_test_"))
        # copy only governance-owned trees (not runs)
        shutil.copytree(REPO / ".claude", self.tmp / ".claude")
        for sub in ("schemas", "policies", "bin"):
            shutil.copytree(REPO / ".workflow" / sub, self.tmp / ".workflow" / sub)
        (self.tmp / ".workflow" / "runs").mkdir(parents=True)
        shutil.copy(REPO / ".workflow" / "config.json",
                    self.tmp / ".workflow" / "config.json")
        (self.tmp / ".workflow" / "index.json").write_text("{}\n")
        os.chmod(self.tmp / ".workflow" / "bin" / "wf", 0o755)
        # a git repo with one commit (implementation checks need HEAD)
        _git(self.tmp, "init")
        _git(self.tmp, "config", "user.email", "t@t")
        _git(self.tmp, "config", "user.name", "t")
        (self.tmp / "README").write_text("seed\n")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "app.py").write_text("# app\n")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-m", "seed")
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
            if k in ("counters", "flags", "current", "budget"):
                st[k].update(v)
            else:
                st[k] = v
        self.save(st)
        return st

    def head(self):
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tmp,
                           capture_output=True, text=True)
        return r.stdout.strip()

    # -- artifact builders ----------------------------------------------------
    def envelope(self, atype, version, produced_by, status="complete"):
        env_type = S.ENVELOPE_TYPE.get(atype, atype)
        return {
            "artifact_type": env_type, "workflow_id": self.wid, "version": version,
            "produced_by": produced_by, "input_artifact_refs": [],
            "created_at": S.utcnow(), "status": status,
        }

    def write_artifact(self, atype, version, body, produced_by="orchestrator",
                       freeze=True, set_pointer=True, status="complete"):
        obj = {**self.envelope(atype, version, produced_by, status), **body}
        rel = S.artifact_relpath(atype, version)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(obj, indent=2))
        if set_pointer:
            _, st = self.load()
            st["current"][atype] = version
            self.save(st)
        if freeze:
            ledger.register(self.tmp, self.wid, rel, frozen=False)
            S.freeze_artifact(self.tmp, self.wid, rel, "TEST")
        return rel, full

    def classification(self, route="full", flags=("security",)):
        return {"scores": {"files_impacted": 2, "architectural": 2, "security": 3,
                           "data_model": 1, "integrations": 2, "testing": 2},
                "total": 12, "override_flags": list(flags), "route": route,
                "evidence": ["src/app.py: entry point"]}

    def plan(self, files=("src/app.py",)):
        return {"objective": "do the thing", "scope": {"in": ["a"], "out": []},
                "milestones": [{"id": "M1", "description": "d",
                                "files": list(files), "verification": "run tests"}],
                "acceptance_criteria": [{"id": "AC1", "criterion": "works",
                                         "verification_method": "unit_test"}],
                "risks": [], "assumptions": [], "dependencies": [],
                "files": [{"path": f, "action": "modify"} for f in files]}

    def review(self, mode, subject, verdict, findings=None, acs=None):
        body = {"mode": mode, "subject_ref": subject, "verdict": verdict,
                "verdict_rationale": "because", "findings": findings or [],
                "acceptance_criteria": acs if acs is not None else
                [{"id": "AC1", "status": "pass", "evidence": "ok"}]}
        return body

    def manifest(self, plan_ref, result="pass", command='python3 -c "pass"',
                 changes=None):
        return {"plan_ref": plan_ref, "base_commit": self.head(),
                "changes": changes or [{"path": "src/app.py", "action": "modified",
                                        "summary": "x"}],
                "tests": {"added": ["tests/t.py"], "command": command,
                          "result": result},
                "milestone_status": [{"milestone_id": "M1", "status": "done"}],
                "deviations": []}

    # -- hook / cli drivers ---------------------------------------------------
    def hook(self, script, payload):
        payload = {**payload, "cwd": str(self.tmp)}
        p = subprocess.run([sys.executable,
                            str(self.tmp / ".claude" / "hooks" / script)],
                           input=json.dumps(payload), capture_output=True,
                           text=True, cwd=str(self.tmp))
        out = p.stdout.strip()
        return json.loads(out) if out else None

    def pretool(self, tool, tool_input, model=None):
        ti = dict(tool_input)
        if model:
            ti["model"] = model
        return self.hook("pretooluse_gate.py",
                         {"tool_name": tool, "tool_input": ti})

    def subagentstop(self):
        return self.hook("subagentstop_validate.py", {"tool_name": "SubagentStop"})

    def posttool(self, tool, tool_input, tool_response=None, is_error=False):
        return self.hook("posttooluse_ledger.py",
                         {"tool_name": tool, "tool_input": tool_input,
                          "tool_response": tool_response, "is_error": is_error})

    def wf(self, *args, env=None):
        e = {**os.environ, **(env or {})} if env else None
        return subprocess.run([sys.executable,
                               str(self.tmp / ".workflow" / "bin" / "wf"), *args],
                              capture_output=True, text=True, cwd=str(self.tmp),
                              env=e)

    def events(self):
        """Every decision-log event name, oldest first."""
        return [e.get("event") for e in ledger.read_events(self.tmp, self.wid)]

    def invocation_prompt(self, exp):
        return (f"GMDW-INVOCATION\n"
                f"workflow_id: {self.wid}\n"
                f"agent: {exp['agent']}\n"
                f"state: {exp['state']}\n"
                f"output_artifact: {exp['path']}\n"
                f"inputs:\n  - .workflow/runs/{self.wid}/request.md\n\n"
                f"Do the work.")

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
        self.seed(state="PLANNING")
        d = self.pretool("Write", {"file_path": "src/app.py", "content": "x"})
        self.assertDenied(d, "source writes are not permitted in state PLANNING")

    def test_02_reviewer_source_write_denied(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="PLAN_REVIEW")
        d = self.pretool("Write", {"file_path": "src/app.py", "content": "x"})
        self.assertDenied(d, "source writes are not permitted in state PLAN_REVIEW")

    def test_03_out_of_scope_edit_denied(self):
        self.write_artifact("classification", 1, self.classification(route="full"))
        self.write_artifact("plan", 1, self.plan(files=("src/app.py",)))
        self.seed(state="IMPLEMENTING", route="full")
        d = self.pretool("Write", {"file_path": "src/other.py", "content": "x"})
        self.assertDenied(d, "outside the approved plan")

    def test_P2_planned_write_allowed(self):
        self.write_artifact("classification", 1, self.classification(route="full"))
        self.write_artifact("plan", 1, self.plan(files=("src/app.py",)))
        self.seed(state="IMPLEMENTING", route="full")
        self.assertAllowed(self.pretool("Write",
                           {"file_path": "src/app.py", "content": "x"}))
        self.assertAllowed(self.pretool("Write",
                           {"file_path": "tests/test_new.py", "content": "x"}))

    def test_protected_paths_denied(self):
        self.seed(state="IMPLEMENTING", route="trivial")
        for p in (".workflow/runs/%s/state.json" % self.wid,
                  ".claude/hooks/pretooluse_gate.py",
                  ".workflow/policies/standard.json",
                  ".claude/settings.json",
                  ".workflow/.hooks_heartbeat"):
            self.assertDenied(self.pretool("Write", {"file_path": p, "content": "x"}),
                              "governance-protected")


# =========================================================================== #
# Loop prevention / limits
# =========================================================================== #

class TestLimits(Base):

    def test_04_plan_revision_limit(self):
        self.write_artifact("classification", 1, self.classification())
        self.write_artifact("plan", 1, self.plan())
        self.write_artifact("plan_review", 1,
                            self.review("plan", f".workflow/runs/{self.wid}/"
                                        f"artifacts/plan_v1.json", "revise",
                                        findings=[{"id": "F1", "category": "scope",
                                                   "severity": "major",
                                                   "location": "x", "description": "d",
                                                   "recommendation": "fix"}]))
        self.seed(state="PLAN_REVIEW",
                  counters={"plan_revisions": 2},  # == max_plan_revisions
                  current={"plan": 1, "plan_review": 1})
        r = self.wf("transition", "PLANNING", "--trigger",
                    f"artifacts/plan_review_v1.json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("revision limit", r.stderr)

    def test_05_invocation_limit(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS",
                  counters={"agent_invocations": 14})  # == max
        _, st = self.load()
        exp = S.expected(self.tmp, st)
        d = self.pretool("Agent",
                         {"subagent_type": exp["agent"],
                          "prompt": self.invocation_prompt(exp)})
        self.assertDenied(d, "invocation limit reached")

    def test_06_budget_exhaustion(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS",
                  budget={"tokens_consumed_estimate": 2000000})
        _, st = self.load()
        exp = S.expected(self.tmp, st)
        d = self.pretool("Agent", {"subagent_type": exp["agent"],
                                   "prompt": self.invocation_prompt(exp)})
        self.assertDenied(d, "token budget exhausted")

    def test_10_review_cycle_limit(self):
        self.write_artifact("classification", 1, self.classification())
        self.write_artifact("plan", 1, self.plan())
        self.seed(state="PLAN_REVIEW", counters={"review_cycles": 6},  # == max
                  current={"plan": 1})
        _, st = self.load()
        exp = S.expected(self.tmp, st)
        d = self.pretool("Agent", {"subagent_type": exp["agent"],
                                   "prompt": self.invocation_prompt(exp)})
        self.assertDenied(d, "review cycle limit reached")


# =========================================================================== #
# Artifact integrity
# =========================================================================== #

class TestArtifacts(Base):

    def test_07_missing_artifact(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS",
                  last_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1},
                  open_invocation=None)
        d = self.subagentstop()
        self.assertBlocked(d, "expected artifact not produced")
        _, st = self.load()
        self.assertEqual(st["counters"]["consecutive_failures"], 1)

    def test_08_invalid_artifact(self):
        self.write_artifact("classification", 1, self.classification())
        # write a repo_analysis missing required fields, do not freeze/point
        rel = S.artifact_relpath("repo_analysis", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps({"artifact_type": "repo_analysis",
                                    "workflow_id": self.wid, "version": 1,
                                    "produced_by": "repo-analyst",
                                    "input_artifact_refs": [],
                                    "created_at": S.utcnow(), "status": "complete",
                                    "stack": {}}))  # missing required keys
        self.seed(state="REPOSITORY_ANALYSIS",
                  last_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1})
        d = self.subagentstop()
        self.assertBlocked(d, "schema validation failed")

    def test_P3_valid_artifact_accepted(self):
        rel = S.artifact_relpath("classification", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        # classification is produced by orchestrator, but validate the accept path
        # using repo_analysis which the validator tracks
        self.write_artifact("classification", 1, self.classification(),
                            freeze=True)
        # produce a valid repo_analysis and let SubagentStop accept it
        ra = {"stack": {"languages": ["python"]},
              "relevant_modules": [{"finding": "app", "evidence": "src/app.py"}],
              "conventions": [], "test_infrastructure": {"test_command": "pytest"},
              "security_surfaces": [], "unverified": []}
        rel2 = S.artifact_relpath("repo_analysis", 1)
        full2 = S.workflow_dir(self.tmp, self.wid) / rel2
        obj = {**self.envelope("repo_analysis", 1, "repo-analyst"), **ra}
        full2.write_text(json.dumps(obj, indent=2))
        self.seed(state="REPOSITORY_ANALYSIS",
                  last_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1})
        d = self.subagentstop()
        self.assertIsNone(d, f"expected accept, got {d}")
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 1)
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel2))

    def test_P4_orchestrator_classification_accepted_on_transition(self):
        # Regression: the Orchestrator writes classification itself, so no
        # SubagentStop fires to advance current.classification. wf transition
        # must accept the on-disk classification (pointer starts at 0) before
        # guarding, or CLASSIFYING can never be left. set_pointer=False mimics
        # the real orchestrator-write path the other tests short-circuit.
        rel, _ = self.write_artifact("classification", 1,
                                     self.classification(route="full"),
                                     freeze=False, set_pointer=False)
        self.seed(state="CLASSIFYING", route="medium", flags={"security": False})
        _, st = self.load()
        self.assertEqual(st["current"]["classification"], 0)
        r = self.wf("transition", "REPOSITORY_ANALYSIS", "--trigger", rel)
        self.assertEqual(r.returncode, 0, f"transition failed: {r.stderr}")
        _, st = self.load()
        self.assertEqual(st["state"], "REPOSITORY_ANALYSIS")
        self.assertEqual(st["current"]["classification"], 1)
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel),
                        "accepted classification must be frozen (immutable)")
        # routing + flags are synced from the accepted classification artifact
        self.assertEqual(st["route"], "full")
        self.assertTrue(st["flags"]["security"])

    def test_P5_second_invocation_validated_not_skipped(self):
        # Regression for the invocation-precedence bug: after the first agent
        # completes, PostToolUse leaves last_invocation without an expected_type.
        # SubagentStop must validate against the ACTIVE open_invocation (set by
        # the gate), not the stale last_invocation -- otherwise every 2nd+ agent
        # artifact in a workflow is silently skipped (never validated/accepted).
        ra = {"stack": {"languages": ["python"]},
              "relevant_modules": [{"finding": "app", "evidence": "src/app.py"}],
              "conventions": [], "test_infrastructure": {"test_command": "pytest"},
              "security_surfaces": [], "unverified": []}
        rel = S.artifact_relpath("repo_analysis", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(
            {**self.envelope("repo_analysis", 1, "repo-analyst"), **ra}, indent=2))
        self.seed(state="REPOSITORY_ANALYSIS",
                  # stale record from a prior completed invocation: NO expected_type
                  last_invocation={"agent": "repo-analyst",
                                   "closed_at": S.utcnow(), "errored": None,
                                   "tokens": 100},
                  # the gate's record of the CURRENT (2nd) invocation
                  open_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1,
                                   "expected_artifact": rel})
        d = self.subagentstop()
        self.assertIsNone(d, f"expected accept, got {d}")
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 1,
                         "2nd-invocation artifact must be validated + accepted")
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel))
        self.assertIsNone(st["open_invocation"])

    def test_P6_in_invocation_correction_revalidated(self):
        # Regression for gap #3: after a SubagentStop rejection, open_invocation
        # must stay set so the agent's corrected artifact is re-validated (and
        # accepted) on its NEXT stop -- otherwise a valid-but-unaccepted artifact
        # is stranded and the workflow silently stalls.
        self.write_artifact("classification", 1, self.classification(), freeze=True)
        rel = S.artifact_relpath("repo_analysis", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        open_inv = {"agent": "repo-analyst", "expected_type": "repo_analysis",
                    "version": 1, "expected_artifact": rel}
        ra_body = {"stack": {"languages": ["python"]},
                   "relevant_modules": [{"finding": "app",
                                         "evidence": "src/app.py"}],
                   "conventions": [], "test_infrastructure":
                   {"test_command": "pytest"}, "security_surfaces": [],
                   "unverified": []}
        # 1) first stop: INVALID artifact_type -> rejected
        full.write_text(json.dumps({"artifact_type": "review",  # wrong enum value
                                    "workflow_id": self.wid, "version": 1,
                                    "produced_by": "repo-analyst",
                                    "input_artifact_refs": [],
                                    "created_at": S.utcnow(), "status": "complete",
                                    **ra_body}))
        self.seed(state="REPOSITORY_ANALYSIS", open_invocation=open_inv,
                  # stale prior-completion record, no expected_type
                  last_invocation={"agent": "repo-analyst", "closed_at": S.utcnow(),
                                   "errored": None, "tokens": 1})
        d = self.subagentstop()
        self.assertBlocked(d, "artifact_type")
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 0)
        self.assertIsNotNone(st["open_invocation"],
                             "gap #3: open_invocation must survive a rejection")
        # 2) agent corrects the artifact and stops again -> must be accepted
        full.write_text(json.dumps(
            {**self.envelope("repo_analysis", 1, "repo-analyst"), **ra_body}))
        d2 = self.subagentstop()
        self.assertIsNone(d2, f"expected accept on corrected re-stop, got {d2}")
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 1)
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel))
        self.assertEqual(st["counters"]["consecutive_failures"], 0)

    def test_09_tampered_artifact(self):
        rel, full = self.write_artifact("plan", 1, self.plan())
        self.assertEqual(ledger.verify(self.tmp, self.wid), [])
        # tamper: rewrite the frozen artifact
        os.chmod(full, 0o644)
        full.write_text(json.dumps({"tampered": True}))
        bad = ledger.verify(self.tmp, self.wid)
        self.assertTrue(bad and bad[0]["problem"] == "hash_mismatch")
        # APPROVED->COMPLETED tamper guard + wf close both refuse
        self.write_artifact("classification", 1, self.classification(flags=()))
        self.seed(state="APPROVED", route="medium", flags={"security": False},
                  approvals=[{"gate": "deployment", "decision": "approved",
                              "by": "u", "at": S.utcnow(),
                              "artifact_ref": "x", "notes": ""}])
        r = self.wf("transition", "COMPLETED", "--trigger", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tamper", r.stderr)


# =========================================================================== #
# Security bypass / deployment / integrity
# =========================================================================== #

class TestSecurityAndDeploy(Base):

    def test_11_security_bypass_denied(self):
        self.write_artifact("classification", 1, self.classification(flags=("security",)))
        self.write_artifact("plan", 1, self.plan())
        self.write_artifact("implementation", 1,
                            self.manifest(f"artifacts/plan_v1.json"),
                            produced_by="implementor")
        self.seed(state="IMPLEMENTING", route="full", flags={"security": True},
                  current={"classification": 1, "plan": 1, "implementation": 1})
        r = self.wf("transition", "CODE_REVIEW", "--trigger",
                    "artifacts/implementation_v1/manifest.json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SECURITY_REVIEW", r.stderr)

    def test_12a_complete_without_deploy_denied(self):
        self.write_artifact("classification", 1, self.classification(flags=()))
        self.seed(state="APPROVED", route="medium", flags={"security": False})
        r = self.wf("transition", "COMPLETED", "--trigger", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("deployment approval required", r.stderr)

    def test_12b_push_without_deploy_denied(self):
        self.seed(state="APPROVED")
        d = self.pretool("Bash", {"command": "git push origin main"})
        self.assertDenied(d, "deployment approval")

    def test_12c_push_after_deploy_allowed(self):
        self.seed(state="APPROVED",
                  approvals=[{"gate": "deployment", "decision": "approved",
                              "by": "u", "at": S.utcnow(), "artifact_ref": "x",
                              "notes": ""}])
        self.assertAllowed(self.pretool("Bash", {"command": "git push origin main"}))

    def test_13_false_test_pass_integrity(self):
        self.write_artifact("classification", 1, self.classification(flags=()))
        self.write_artifact("plan", 1, self.plan())
        # manifest claims pass, but command exits nonzero
        rel = S.artifact_relpath("implementation", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        obj = {**self.envelope("implementation", 1, "implementor"),
               **self.manifest("artifacts/plan_v1.json", result="pass",
                               command='python3 -c "import sys; sys.exit(1)"')}
        full.write_text(json.dumps(obj, indent=2))
        self.seed(state="IMPLEMENTING", route="medium", flags={"security": False},
                  current={"classification": 1, "plan": 1},
                  last_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1})
        d = self.subagentstop()
        self.assertBlocked(d, "INTEGRITY VIOLATION")
        _, st = self.load()
        self.assertTrue(st["flags"]["integrity_hold"])

    def test_13b_honest_test_pass_accepted(self):
        self.write_artifact("classification", 1, self.classification(flags=()))
        self.write_artifact("plan", 1, self.plan())
        rel = S.artifact_relpath("implementation", 1)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        obj = {**self.envelope("implementation", 1, "implementor"),
               **self.manifest("artifacts/plan_v1.json", result="pass",
                               command='python3 -c "pass"')}
        full.write_text(json.dumps(obj, indent=2))
        self.seed(state="IMPLEMENTING", route="medium", flags={"security": False},
                  current={"classification": 1, "plan": 1},
                  last_invocation={"agent": "implementor",
                                   "expected_type": "implementation", "version": 1})
        d = self.subagentstop()
        self.assertIsNone(d, f"expected accept, got {d}")
        _, st = self.load()
        self.assertEqual(st["current"]["implementation"], 1)


# =========================================================================== #
# Positive control: full legal invocation path
# =========================================================================== #

class TestLegalPath(Base):

    def test_P1_legal_invocation_allowed(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS")
        _, st = self.load()
        exp = S.expected(self.tmp, st)
        d = self.pretool("Agent", {"subagent_type": exp["agent"],
                                   "prompt": self.invocation_prompt(exp)},
                         model=exp["model"])
        self.assertAllowed(d)
        _, st = self.load()
        self.assertEqual(st["counters"]["agent_invocations"], 1)
        self.assertIsNotNone(st["open_invocation"])
        self.assertEqual(st["open_invocation"]["agent"], "repo-analyst")

    def test_wrong_agent_for_state_denied(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS")
        _, st = self.load()
        exp = S.expected(self.tmp, st)
        prompt = self.invocation_prompt(exp).replace("agent: repo-analyst",
                                                     "agent: planner")
        d = self.pretool("Agent", {"subagent_type": "planner", "prompt": prompt})
        self.assertDenied(d, "requires agent 'repo-analyst'")

    def test_missing_header_denied(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS")
        d = self.pretool("Agent", {"subagent_type": "repo-analyst",
                                   "prompt": "just do it, no header"})
        self.assertDenied(d, "GMDW-INVOCATION header")

    def test_wrong_model_denied(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS")
        _, st = self.load()
        exp = S.expected(self.tmp, st)
        d = self.pretool("Agent", {"subagent_type": exp["agent"],
                                   "prompt": self.invocation_prompt(exp)},
                         model="haiku")  # policy says sonnet for repo-analyst
        self.assertDenied(d, "policy assigns model")


# =========================================================================== #
# Governance-liveness heartbeat (ADR-001 Phase 1)
# =========================================================================== #

class TestGovernanceLiveness(unittest.TestCase):
    """`wf init` must refuse to start a workflow in a session where the
    enforcement hooks are not firing (the out-of-project deadlock from
    wf_20260713_003 / KNOWN_LIMITATIONS R4,F1). It attests liveness via the
    heartbeat the hooks write. These tests build a fresh repo skeleton with NO
    ACTIVE workflow, so they exercise the CLI init path directly."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gmdw_live_"))
        shutil.copytree(REPO / ".claude", self.tmp / ".claude")
        for sub in ("schemas", "policies", "bin"):
            shutil.copytree(REPO / ".workflow" / sub, self.tmp / ".workflow" / sub)
        (self.tmp / ".workflow" / "runs").mkdir(parents=True)
        shutil.copy(REPO / ".workflow" / "config.json",
                    self.tmp / ".workflow" / "config.json")
        (self.tmp / ".workflow" / "index.json").write_text("{}\n")
        os.chmod(self.tmp / ".workflow" / "bin" / "wf", 0o755)
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
        """Fire the real PreToolUse gate exactly as Claude Code would on a Bash
        call — this is what writes the heartbeat in a governed session."""
        payload = {"tool_name": tool,
                   "tool_input": tool_input or {"command": ".workflow/bin/wf init"},
                   "cwd": str(self.tmp)}
        subprocess.run([sys.executable,
                        str(self.tmp / ".claude" / "hooks" / "pretooluse_gate.py")],
                       input=json.dumps(payload), capture_output=True, text=True,
                       cwd=str(self.tmp))

    def write_heartbeat(self, age_sec):
        # age_sec > 0 => past; age_sec < 0 => future
        ts = (S.datetime.datetime.now(S.datetime.timezone.utc)
              - S.datetime.timedelta(seconds=age_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = self.tmp / heartbeat.HEARTBEAT_REL
        p.write_text(json.dumps({"ts": ts, "event": "PreToolUse", "pid": 1}))

    def test_L1_init_refused_when_hookless(self):
        # No heartbeat at all: the enforcement plane is absent. init must refuse.
        r = self.wf("init", "--request", "request.md", "--by", "tester")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("governance hooks are not active", r.stderr)
        self.assertFalse(S.active_id(self.tmp), "no workflow may have been created")

    def test_L2_hook_writes_live_heartbeat(self):
        # The real PreToolUse gate must write a fresh, well-formed heartbeat even
        # when no workflow is active (this is what self-attests on the init call).
        self.assertFalse((self.tmp / heartbeat.HEARTBEAT_REL).exists())
        self.drive_hook()
        live, why = heartbeat.status(self.tmp)
        self.assertTrue(live, why)

    def test_L3_init_allowed_when_hooks_live(self):
        # Governed session: the gate fires on the init Bash call, then init sees a
        # fresh heartbeat and proceeds. This is the end-to-end self-attestation.
        self.drive_hook()
        r = self.wf("init", "--request", "request.md", "--by", "tester")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(S.active_id(self.tmp))
        self.assertEqual(json.loads(r.stdout)["state"], "RECEIVED")

    def test_L4_init_refused_when_heartbeat_stale(self):
        # A leftover heartbeat from an old governed session must not vouch forever.
        self.write_heartbeat(age_sec=10000)
        r = self.wf("init", "--request", "request.md", "--by", "tester")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stale", r.stderr)
        self.assertFalse(S.active_id(self.tmp))

    def test_L5_bypass_env_allows_and_is_audited(self):
        # The explicit escape hatch for legitimate non-Claude use: proceeds
        # despite no heartbeat, but records the ungoverned start in the ledger.
        r = self.wf("init", "--request", "request.md", "--by", "ci",
                    env={"GMDW_ALLOW_UNGOVERNED_INIT": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        wid = json.loads(r.stdout)["workflow_id"]
        log = (self.tmp / ".workflow" / "runs" / wid / "decision_log.jsonl") \
            .read_text()
        self.assertIn("governance_liveness_bypassed", log)

    def test_L6_custom_freshness_window_env(self):
        # The window is tunable; a heartbeat older than the override is stale.
        self.write_heartbeat(age_sec=120)
        r = self.wf("init", "--request", "request.md", "--by", "tester",
                    env={"GMDW_HEARTBEAT_MAX_AGE_SEC": "60"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("stale", r.stderr)

    def test_L7_future_dated_heartbeat_rejected(self):
        # ADR-001 review F-1: a future-dated beacon must NOT be treated as fresh.
        # Without an upper skew bound, `age<0 -> 0` made a single planted
        # `ts: 2099-...` heartbeat vouch for liveness forever, defeating the
        # bounded trust window. It must be rejected outright, and init must refuse.
        self.write_heartbeat(age_sec=-10 * 365 * 24 * 3600)  # ~10 years ahead
        live, why = heartbeat.status(self.tmp)
        self.assertFalse(live, f"future-dated heartbeat must not be live: {why}")
        self.assertIn("future", why)
        r = self.wf("init", "--request", "request.md", "--by", "tester")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("governance hooks are not active", r.stderr)
        self.assertFalse(S.active_id(self.tmp))

    def test_L8_small_future_skew_tolerated(self):
        # Benign same-machine clock jitter (within MAX_FUTURE_SKEW_SEC) is still
        # accepted, so the fix does not introduce false refusals.
        self.write_heartbeat(age_sec=-5)  # 5s ahead, inside the 60s skew window
        live, why = heartbeat.status(self.tmp)
        self.assertTrue(live, f"small forward skew should stay live: {why}")

    def test_L9_doctor_reports_live_after_hook_fires(self):
        # `wf doctor` is a read-only preflight: once a hook has written a fresh
        # heartbeat it reports LIVE and exits 0, and it must create no workflow.
        self.drive_hook()
        r = self.wf("doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("LIVE", r.stdout)
        rj = self.wf("doctor", "--json")
        self.assertEqual(rj.returncode, 0)
        self.assertTrue(json.loads(rj.stdout)["hooks_live"])
        self.assertFalse(S.active_id(self.tmp), "doctor must not create a workflow")

    def test_L10_doctor_reports_dark_without_heartbeat(self):
        # No heartbeat (hooks not firing): doctor reports the plane down, exits 1.
        r = self.wf("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT firing", r.stdout)
        rj = self.wf("doctor", "--json")
        self.assertEqual(rj.returncode, 1)
        self.assertFalse(json.loads(rj.stdout)["hooks_live"])

    def test_L11_doctor_reports_dark_when_heartbeat_stale(self):
        # A stale heartbeat is treated as down, same as init / transition.
        self.write_heartbeat(age_sec=10000)
        r = self.wf("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT firing", r.stdout)


# =========================================================================== #
# Recovery + mid-run liveness (ADR-001 Phase 2)
# =========================================================================== #

class TestPhase2Recovery(Base):
    """The hooks-dark failure modes and their human-gated recovery.

    Phase 1 proved the hooks were live when a workflow STARTED. These cover what
    happens when a session loses them mid-run: the workflow refuses to advance
    (R11/R12), and the two states a dark session can strand — an unaccepted
    artifact (R1-R6) and an orphaned invocation lock (R7-R9) — are recoverable by
    a named human, but only from a session where the hooks really are dark (R10).

    Note: these tests invoke the wf entry point directly, as an operator's
    terminal would. That is exactly the path the R10 gate deny leaves open, so
    the deny does not (and must not) affect them.
    """

    def repo_analysis_body(self):
        return {"stack": {"languages": ["python"]},
                "relevant_modules": [{"finding": "app", "evidence": "src/app.py"}],
                "conventions": [], "test_infrastructure": {"test_command": "pytest"},
                "security_surfaces": [], "unverified": []}

    def strand_repo_analysis(self, version=1):
        """A schema-valid repo_analysis on disk that nothing ever accepted —
        what a hooks-dark session leaves behind (reproduces wf_20260713_003)."""
        rel = S.artifact_relpath("repo_analysis", version)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(
            {**self.envelope("repo_analysis", version, "repo-analyst"),
             **self.repo_analysis_body()}, indent=2))
        return rel, full

    def go_dark(self):
        """Drop the heartbeat seeded by setUp: the hooks stop firing mid-run."""
        (self.tmp / heartbeat.HEARTBEAT_REL).unlink()

    def accepted_events(self):
        return [e for e in ledger.read_events(self.tmp, self.wid)
                if e.get("event") == "artifact_accepted"]

    # -- wf accept: stranded-artifact recovery --------------------------------

    def test_R1_accept_unstrands_valid_repo_analysis(self):
        self.write_artifact("classification", 1, self.classification())
        rel, _ = self.strand_repo_analysis()
        self.seed(state="REPOSITORY_ANALYSIS", current={"repo_analysis": 0},
                  open_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1,
                                   "expected_artifact": rel})
        # Stuck: the artifact is valid and on disk, but only acceptance sets the
        # pointer the guard requires, and only a hook accepts.
        r = self.wf("transition", "PLANNING", "--trigger", rel)
        self.assertEqual(r.returncode, 1, "a stranded artifact must block")

        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 1)
        self.assertTrue(ledger.is_frozen(self.tmp, self.wid, rel),
                        "a recovered artifact is accepted, so it is immutable")
        self.assertIsNone(st["open_invocation"],
                          "recovery must release the dangling invocation too")
        ev = self.accepted_events()[-1]
        self.assertEqual(ev["actor"], "human_recovery",
                         "audits must separate hook-accepted from human-recovered")
        self.assertEqual(ev["by"], "arun")
        # ...and the workflow moves again
        r = self.wf("transition", "PLANNING", "--trigger", rel)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.load()[1]["state"], "PLANNING")

    def test_R2_accept_rejects_schema_invalid_artifact(self):
        rel, full = self.strand_repo_analysis()
        full.write_text(json.dumps({"artifact_type": "repo_analysis",
                                    "workflow_id": self.wid, "version": 1,
                                    "produced_by": "repo-analyst",
                                    "input_artifact_refs": [],
                                    "created_at": S.utcnow(), "status": "complete",
                                    "stack": {}}))  # missing required keys
        self.seed(state="REPOSITORY_ANALYSIS", current={"repo_analysis": 0})
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("schema validation failed", r.stderr)
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 0)
        self.assertFalse(ledger.is_frozen(self.tmp, self.wid, rel))
        self.assertEqual(self.accepted_events(), [])

    def test_R3_accept_rejects_wrong_version(self):
        # v2 on disk while the state waits for v1: accepting "whatever is there"
        # would let an out-of-band version skip a version of review.
        rel2, _ = self.strand_repo_analysis(version=2)
        self.seed(state="REPOSITORY_ANALYSIS", current={"repo_analysis": 0})
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("only that exact version", r.stderr)
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 0)
        self.assertFalse(ledger.is_frozen(self.tmp, self.wid, rel2))

    def test_R4_accept_rejects_already_frozen(self):
        rel, _ = self.write_artifact("repo_analysis", 1, self.repo_analysis_body(),
                                     produced_by="repo-analyst", freeze=True,
                                     set_pointer=False)
        self.seed(state="REPOSITORY_ANALYSIS", current={"repo_analysis": 0})
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("immutable", r.stderr)
        self.assertEqual(self.load()[1]["current"]["repo_analysis"], 0)

    def test_R5_accept_requires_by(self):
        rel, _ = self.strand_repo_analysis()
        self.seed(state="REPOSITORY_ANALYSIS", current={"repo_analysis": 0})
        r = self.wf("accept")
        self.assertNotEqual(r.returncode, 0, "recovery must name a human")
        _, st = self.load()
        self.assertEqual(st["current"]["repo_analysis"], 0)
        self.assertFalse(ledger.is_frozen(self.tmp, self.wid, rel))

    def test_R6_accept_refuses_when_expected_is_none(self):
        self.seed(state="PLAN_APPROVAL")  # a human gate: no artifact is produced
        r = self.wf("accept", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("produces no artifact", r.stderr)
        self.assertEqual(self.load()[1]["state"], "PLAN_APPROVAL")

    # -- wf clear-invocation: orphaned-lock recovery ---------------------------

    def test_R7_clear_invocation_unblocks_escalate(self):
        self.write_artifact("classification", 1, self.classification())
        self.seed(state="REPOSITORY_ANALYSIS", counters={"consecutive_failures": 1},
                  open_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1,
                                   "expected_artifact":
                                       S.artifact_relpath("repo_analysis", 1)})
        # The documented escape (KNOWN_LIMITATIONS C1) is itself blocked: guard()
        # checks open_invocation before it allows ESCALATED. Total deadlock.
        r = self.wf("transition", "ESCALATED", "--trigger", "orphan",
                    "--reason", "other")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invocation is still open", r.stderr)

        r = self.wf("clear-invocation", "--by", "arun")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, st = self.load()
        self.assertIsNone(st["open_invocation"])
        self.assertEqual(st["counters"]["consecutive_failures"], 1,
                         "releasing the lock must not reset the failure counter "
                         "(or it becomes a way to dodge the escalation limit)")
        self.assertEqual(st["current"]["repo_analysis"], 0,
                         "clearing a lock must not accept anything")
        ev = [e for e in ledger.read_events(self.tmp, self.wid)
              if e.get("event") == "invocation_cleared"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["actor"], "human_recovery")
        self.assertEqual(ev[0]["by"], "arun")
        self.assertEqual(ev[0]["agent"], "repo-analyst")

        r = self.wf("transition", "ESCALATED", "--trigger", "orphan",
                    "--reason", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.load()[1]["state"], "ESCALATED")

    def test_R8_clear_invocation_refuses_when_none(self):
        self.seed(state="REPOSITORY_ANALYSIS", open_invocation=None)
        before = json.dumps(self.load()[1], sort_keys=True)
        n_events = len(self.events())
        r = self.wf("clear-invocation", "--by", "arun")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open invocation", r.stderr)
        self.assertEqual(json.dumps(self.load()[1], sort_keys=True), before)
        self.assertEqual(len(self.events()), n_events,
                         "a refusal must not write a ledger event")

    def test_R9_clear_invocation_requires_by(self):
        inv = {"agent": "repo-analyst", "expected_type": "repo_analysis",
               "version": 1}
        self.seed(state="REPOSITORY_ANALYSIS", open_invocation=inv)
        r = self.wf("clear-invocation")
        self.assertNotEqual(r.returncode, 0, "recovery must name a human")
        self.assertEqual(self.load()[1]["open_invocation"], inv)

    # -- SF1: the recovery commands are unreachable while hooks are live -------

    def test_R10_gate_denies_recovery_commands_while_active(self):
        self.seed(state="REPOSITORY_ANALYSIS")
        for cmd in ("wf accept --by ci",
                    ".workflow/bin/wf accept --by ci",
                    "wf clear-invocation --by ci",
                    "python3 .workflow/bin/wf clear-invocation --by ci"):
            self.assertDenied(self.pretool("Bash", {"command": cmd}),
                              "recovery commands")
        # ...without swallowing the rest of the CLI
        self.assertAllowed(self.pretool("Bash", {"command": "wf status"}))
        self.assertAllowed(self.pretool("Bash", {"command": "wf expected"}))

    # -- mid-run transition liveness ------------------------------------------

    def test_R11_transition_refuses_when_hooks_dark(self):
        self.write_artifact("classification", 1, self.classification())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body(),
                            produced_by="repo-analyst")
        self.seed(state="REPOSITORY_ANALYSIS")
        self.go_dark()
        r = self.wf("transition", "PLANNING", "--trigger",
                    "artifacts/repo_analysis_v1.json")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("governance hooks are not firing", r.stderr)
        self.assertEqual(self.load()[1]["state"], "REPOSITORY_ANALYSIS")
        self.assertIn("transition_denied", self.events(),
                      "a mid-run refusal must leave an audit trail")

    def test_R12_transition_bypass_proceeds_and_logs(self):
        self.write_artifact("classification", 1, self.classification())
        self.write_artifact("repo_analysis", 1, self.repo_analysis_body(),
                            produced_by="repo-analyst")
        self.seed(state="REPOSITORY_ANALYSIS")
        self.go_dark()
        trig = "artifacts/repo_analysis_v1.json"

        # =0 must NOT bypass: the affirmative-only read fails closed, so a stray
        # assignment cannot silently switch governance off.
        r = self.wf("transition", "PLANNING", "--trigger", trig,
                    env={"GMDW_ALLOW_UNGOVERNED": "0"})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("governance hooks are not firing", r.stderr)
        self.assertEqual(self.load()[1]["state"], "REPOSITORY_ANALYSIS")
        self.assertNotIn("governance_liveness_bypassed", self.events())

        # ESCALATED is exempt: a dark session must still be able to bail out.
        r = self.wf("transition", "ESCALATED", "--trigger", trig, "--reason", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.load()[1]["state"], "ESCALATED")
        self.assertNotIn("governance_liveness_bypassed", self.events())

        # An explicit affirmative proceeds, and says so in the ledger.
        self.seed(state="REPOSITORY_ANALYSIS")
        r = self.wf("transition", "PLANNING", "--trigger", trig,
                    env={"GMDW_ALLOW_UNGOVERNED": "TRUE"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.load()[1]["state"], "PLANNING")
        self.assertIn("governance_liveness_bypassed", self.events())

        # ...as does the flag.
        self.seed(state="REPOSITORY_ANALYSIS")
        r = self.wf("transition", "PLANNING", "--trigger", trig,
                    "--allow-ungoverned")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            len([e for e in self.events() if e == "governance_liveness_bypassed"]), 2)

    # -- the validator still fails closed after the extraction ----------------

    def test_R13_validator_error_sets_integrity_hold(self):
        self.write_artifact("classification", 1, self.classification(), freeze=True)
        rel, _ = self.strand_repo_analysis()
        self.seed(state="REPOSITORY_ANALYSIS",
                  open_invocation={"agent": "repo-analyst",
                                   "expected_type": "repo_analysis", "version": 1,
                                   "expected_artifact": rel})
        # A deterministic fault PAST the schema/semantic stage and inside main():
        # the freeze step loads the registry, which is no longer JSON.
        (S.workflow_dir(self.tmp, self.wid) / "artifacts_index.json") \
            .write_text("{ not json")
        d = self.subagentstop()
        self.assertBlocked(d, "validator internal error")
        _, st = self.load()
        self.assertTrue(st["flags"]["integrity_hold"])
        self.assertIn("validator_error", self.events())
        self.assertEqual(st["current"]["repo_analysis"], 0,
                         "a fault mid-accept must not leave the artifact accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
