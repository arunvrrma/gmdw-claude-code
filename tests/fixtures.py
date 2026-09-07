"""Shared test fixtures: a throwaway GMDW repo and valid Markdown artifacts.

Both suites need the same two things — a scratch copy of the governance plane,
and artifacts that are *correct*, so that a test asserting a rejection is
asserting the one defect it introduced rather than an accidental second one.
Keeping the builders here means a change to the artifact format breaks one file,
not two.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from gmdw import ledger  # noqa: E402
from gmdw import state as S  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                   check=False)


def make_repo_skeleton(tmp: Path) -> None:
    """A throwaway copy of everything the governance plane consists of."""
    shutil.copytree(REPO / ".claude", tmp / ".claude")
    shutil.copytree(REPO / "gmdw", tmp / "gmdw",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for sub in ("schemas", "policies", "bin"):
        shutil.copytree(REPO / ".workflow" / sub, tmp / ".workflow" / sub)
    (tmp / ".workflow" / "runs").mkdir(parents=True)
    shutil.copy(REPO / ".workflow" / "config.json", tmp / ".workflow" / "config.json")
    (tmp / ".workflow" / "index.json").write_text("{}\n")
    os.chmod(tmp / ".workflow" / "bin" / "wf", 0o755)


def make_git_repo(tmp: Path) -> None:
    """One commit, so the implementation manifest's base_commit has an ancestor."""
    git(tmp, "init")
    git(tmp, "config", "user.email", "t@t")
    git(tmp, "config", "user.name", "t")
    (tmp / "README").write_text("seed\n")
    (tmp / "src").mkdir(exist_ok=True)
    (tmp / "src" / "app.py").write_text("# app\n")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-m", "seed")


def head(tmp: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp,
                          capture_output=True, text=True).stdout.strip()


PRODUCER = {"classification": "analyst-planner", "repo_analysis": "analyst-planner",
            "plan": "analyst-planner", "implementation": "implementor",
            "review": "reviewer"}


class ArtifactBuilders:
    """Mixin: valid structured-Markdown artifacts for a workflow.

    Expects ``self.tmp`` (repo root) and ``self.wid`` (workflow id).
    """

    def envelope(self, atype, version, status="complete", extra=""):
        return (f"---\n"
                f"artifact_type: {S.ENVELOPE_TYPE[atype]}\n"
                f"workflow_id: {self.wid}\n"
                f"version: {version}\n"
                f"produced_by: {PRODUCER[atype]}\n"
                f"created_at: {S.utcnow()}\n"
                f"status: {status}\n{extra}"
                f"---\n\n")

    def head(self):
        return head(self.tmp)

    # -- bodies ---------------------------------------------------------------

    def classification_body(self, route="full", flags="security", security=3):
        return (f"## Complexity\n| dimension | score |\n|---|---|\n"
                f"| files_impacted | 2 |\n| architectural | 2 |\n"
                f"| security | {security} |\n| data_model | 1 |\n"
                f"| integrations | 2 |\n| testing | 2 |\n\n"
                f"## Risk Level\nroute: {route}\nflags: {flags or 'none'}\n\n"
                f"## Governance Profile\nBecause of the security surface.\n\n"
                f"## Evidence\n- src/app.py — the entry point\n")

    def repo_analysis_body(self):
        return ("## Summary\nA one-module python app.\n\n"
                "## File Tree\n- src/app.py — the app\n\n"
                "## Dependencies\n- stdlib only\n\n"
                "## Architecture Notes\n- src/app.py — single module\n")

    def plan_body(self, files=("src/app.py",)):
        scope = "\n".join(f"- {f} — modify — because" for f in files)
        return (f"## Objective\nDo the thing.\n\n"
                f"## Files In Scope\n{scope}\n\n"
                f"## Milestones\n- M1 — do it\n  - files: {', '.join(files)}\n"
                f"  - verification: python3 -c \"pass\"\n\n"
                f"## Acceptance Criteria\n| id | criterion | verification method |\n"
                f"|---|---|---|\n| AC1 | it works | unit_test |\n\n"
                f"## Risk Mitigations\n| risk | likelihood | impact | mitigation |\n"
                f"|---|---|---|---|\n| it breaks | low | low | tests |\n")

    def review_body(self, verdict="approve", findings=None, acs=None,
                    checklist=True):
        rows = findings if findings is not None else []
        f_table = ("## Findings\n| id | severity | category | file | description | "
                   "recommendation |\n|---|---|---|---|---|---|\n" +
                   "".join(f"| {f} |\n" for f in rows))
        ac_rows = acs if acs is not None else ["AC1 | pass | tests/t.py"]
        ac_table = ("## Acceptance Criteria\n| id | status | evidence |\n|---|---|---|\n"
                    + "".join(f"| {a} |\n" for a in ac_rows))
        sec = ("## Security Checklist\n| area | evidence |\n|---|---|\n"
               "| input_handling | bounded at src/app.py:1 |\n\n" if checklist else "")
        return (f"## Verdict\nverdict: {verdict}\nrationale: because.\n\n"
                f"{f_table}\n{ac_table}\n{sec}## Summary\nA short summary.\n")

    def manifest_body(self, result="pass", command='python3 -c "pass"',
                      changes=("src/app.py | modified | x",), addressed=None):
        rows = "".join(f"| {c} |\n" for c in changes)
        extra = ""
        if addressed:
            extra = ("\n## Findings Addressed\n| finding id | resolution |\n|---|---|\n"
                     + "".join(f"| {a} |\n" for a in addressed))
        return (f"## Base Commit\n{self.head()}\n\n"
                f"## Changes\n| path | action | summary |\n|---|---|---|\n{rows}\n"
                f"## Tests\n- command: {command}\n- result: {result}\n"
                f"- added: tests/t.py\n\n"
                f"## Milestone Status\n| milestone id | status | note |\n|---|---|---|\n"
                f"| M1 | done | |\n\n## Deviations\n- none\n{extra}")

    # -- writers --------------------------------------------------------------

    def write_artifact(self, atype, version, body, freeze=True, set_pointer=True,
                       status="complete", extra=""):
        text = self.envelope(atype, version, status, extra) + body
        rel = S.artifact_relpath(atype, version)
        full = S.workflow_dir(self.tmp, self.wid) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
        if set_pointer:
            _, st = S.load_state(self.tmp, self.wid)
            st["current"][atype] = version
            S.save_state(self.tmp, self.wid, st)
        if freeze:
            ledger.register(self.tmp, self.wid, rel, frozen=False)
            S.freeze_artifact(self.tmp, self.wid, rel, "TEST")
        return rel, full

    def write_implementation(self, version=1, set_pointer=True, freeze=True, **kw):
        rel, full = self.write_artifact("implementation", version,
                                        self.manifest_body(**kw),
                                        set_pointer=set_pointer, freeze=freeze,
                                        extra="plan_ref: artifacts/plan_v1.md\n")
        (full.parent / "diff.patch").write_text("--- a\n+++ b\n@@\n+change\n")
        (full.parent / "test_output.txt").write_text("ok\n")
        return rel, full

    def write_review(self, version=1, impl=1, set_pointer=True, freeze=True, **kw):
        return self.write_artifact(
            "review", version, self.review_body(**kw), set_pointer=set_pointer,
            freeze=freeze,
            extra=f"subject_ref: artifacts/implementation_v{impl}/manifest.md\n")
