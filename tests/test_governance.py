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

    def wf(self, *args):
        return subprocess.run([sys.executable,
                               str(self.tmp / ".workflow" / "bin" / "wf"), *args],
                              capture_output=True, text=True, cwd=str(self.tmp))

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
                  ".claude/settings.json"):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
