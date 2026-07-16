"""GMDW artifact-acceptance core (ADR-001 Phase 2).

The single home of the artifact accept pipeline. It performs, in order:

  1. expected-artifact existence
  2. envelope + type schema validation
  3. blocked-status detection ("agent could not complete")
  4. type-specific semantic cross-checks (path existence, revision coverage,
     verdict consistency, deviation scope, test re-verification)
  5. counter bumps (plan_revisions / impl_revisions / review_cycles / …)
  6. current-pointer update + freeze (chmod a-w + hash registry)
  7. the ``artifact_accepted`` ledger event

Two callers share it: the SubagentStop validator hook (the normal path, where a
subagent has just produced the artifact) and ``wf accept`` (the human-gated
recovery path for an artifact that was validly produced but left stranded by a
session whose hooks were not firing). Both therefore apply *identical*
validation — the recovery path cannot accept anything the hook would refuse.

Plane separation
----------------
This module owns the **acceptance plane**: everything that is a property of the
artifact and the workflow's record of it. It deliberately does NOT own the
**session plane** — ``open_invocation``, ``consecutive_failures``, the hook's
block/allow protocol — nor does it persist state: every caller decides when to
``S.save_state``. The one intentional exception is ``_semantic``'s integrity-hold
branch, which saves and logs inline so a detected test-rerun forgery is durable
even if the caller never completes.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger, schemas
from . import state as S

# artifact type -> the per-type review counter it bumps
REVIEW_TO_ATYPE_COUNTER = S.REVIEW_COUNTER


@dataclass
class Result:
    """Outcome of one acceptance attempt.

    outcome:  accepted | rejected | blocked | integrity_hold
    reason:   human-readable explanation (the hook feeds it back to the model;
              the CLI prints it)
    event:    the ledger event the caller should log for a non-accepted outcome
    extra:    the fields that event carries (path, errors, …)
    """
    outcome: str
    reason: str = ""
    event: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.outcome == "accepted"


# --------------------------------------------------------------------------- #
# Semantic checks per artifact type
# --------------------------------------------------------------------------- #

def _semantic(root: Path, st: dict, atype: str, version: int,
              artifact: dict) -> str | None:
    """Return an error string if the artifact is semantically invalid."""
    prof = st["policy"]["snapshot"]
    verify = prof["verification"]

    if atype == "repo_analysis" and verify.get("path_existence_checks", True):
        for group in ("relevant_modules", "conventions", "security_surfaces"):
            for item in artifact.get(group, []):
                ev = str(item.get("evidence", "")).split(":")[0].strip()
                if ev and "/" in ev and not (Path(root) / ev).exists():
                    return f"evidence path does not exist: {ev}"

    if atype == "plan":
        if verify.get("path_existence_checks", True):
            for f in artifact.get("files", []):
                if f.get("action") == "modify" and not (Path(root) / f["path"]).exists():
                    return f"plan modifies a non-existent file: {f['path']}"
        if version > 1:
            prior = S.load_artifact(root, st, "plan_review", st["current"]["plan_review"])
            sprior = S.load_artifact(root, st, "security_review_plan",
                                     st["current"]["security_review_plan"])
            must = set()
            for rev in (prior, sprior):
                if rev:
                    key = "findings"
                    for fnd in rev.get(key, []):
                        if fnd.get("severity") in ("blocker", "major"):
                            must.add(fnd["id"])
            covered = {rn.get("finding_id") for rn in artifact.get("revision_notes", [])}
            missing = must - covered
            if missing:
                return f"revision does not address findings: {sorted(missing)}"

    if atype in ("plan_review", "code_review", "security_review_plan",
                 "security_review_code"):
        verdict = artifact.get("verdict")
        findings = artifact.get("findings", [])
        open_bm = [f for f in findings if f.get("severity") in ("blocker", "major")]
        if verdict == "approve" and open_bm:
            return f"verdict 'approve' with open blocker/major findings: " \
                   f"{[f['id'] for f in open_bm]}"
        if atype in ("security_review_plan", "security_review_code"):
            if not artifact.get("checked"):
                return "security review must record checked[] areas (non-empty)"
        if atype == "code_review" and st["route"] != "trivial":
            if not artifact.get("acceptance_criteria"):
                return "code_review must evaluate acceptance_criteria"

    if atype == "implementation":
        base = artifact.get("base_commit", "")
        # base_commit must be an ancestor of HEAD (or HEAD itself)
        try:
            r = subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"],
                               cwd=root, capture_output=True, timeout=15)
            if r.returncode not in (0,):
                return f"base_commit {base[:12]} is not an ancestor of HEAD " \
                       f"(stale workspace / concurrent change)"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass  # no git or timeout: skip ancestry, other checks still apply

        # scope: changed files subset of plan.files ∪ tests ∪ docs ∪ deviations
        if st["route"] != "trivial":
            plan = S.load_artifact(root, st, "plan")
            planned = {f["path"] for f in (plan or {}).get("files", [])}
            devs = {d["what"] for d in artifact.get("deviations", [])}
            for ch in artifact.get("changes", []):
                p = ch["path"]
                if p in planned or p.startswith(("tests/", "test/", "docs/")):
                    continue
                if any(p in d or d in p for d in devs):
                    continue
                return f"changed file outside plan scope and undeclared: {p}"

        # test re-verification (profile-gated) — the highest-severity check
        if verify.get("rerun_tests", True):
            declared = artifact.get("tests", {}).get("result")
            cmd = artifact.get("tests", {}).get("command", "").strip()
            if declared in ("pass", "fail") and cmd:
                try:
                    r = subprocess.run(cmd, cwd=root, shell=True,
                                       capture_output=True, timeout=300)
                    observed = "pass" if r.returncode == 0 else "fail"
                except subprocess.TimeoutExpired:
                    observed = "timeout"
                except OSError:
                    observed = None
                if observed and observed != declared:
                    # The one place this module persists state: a detected forgery
                    # must survive even if the caller dies before saving.
                    st["flags"]["integrity_hold"] = True
                    S.save_state(root, st["workflow"]["id"], st)
                    ledger.log(root, st["workflow"]["id"], "integrity_violation",
                               check="tests_rerun", declared=declared,
                               observed=observed, command=cmd)
                    return (f"INTEGRITY VIOLATION: manifest reports tests "
                            f"'{declared}' but re-run observed '{observed}'. "
                            f"Workflow frozen; human escalation required.")
                ledger.log(root, st["workflow"]["id"], "verification",
                           check="tests_rerun", declared=declared, observed=observed,
                           command=cmd)
    return None


# --------------------------------------------------------------------------- #
# The accept pipeline
# --------------------------------------------------------------------------- #

def accept_artifact(root, st: dict, atype: str, version: int, *,
                    actor: str | None = None, by: str | None = None) -> Result:
    """Validate and (if valid) accept ``<atype>_v<version>`` for the workflow in
    ``st``: bump counters, advance ``current[atype]``, freeze the artifact and
    log ``artifact_accepted``.

    Mutates ``st`` in memory but never saves it (see the module docstring); the
    caller owns persistence and the session plane. ``actor``/``by`` are recorded
    on the acceptance event only when supplied, so the hook path — which passes
    neither — keeps writing exactly the event it always has, and a human recovery
    via ``wf accept`` is distinguishable in the audit trail.

    An ``accepted`` result always implies the artifact was frozen.
    """
    wid = st["workflow"]["id"]
    run_rel = S.artifact_relpath(atype, version)
    full = S.workflow_dir(root, wid) / run_rel

    # 1. existence
    if not full.exists():
        return Result("rejected", f"expected artifact not produced: {run_rel}",
                      "artifact_missing", {"path": run_rel})

    # 2. schema
    try:
        artifact = json.loads(full.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return Result("rejected", f"artifact is not valid JSON: {e}",
                      "artifact_invalid", {"path": run_rel})
    errs = schemas.validate_artifact(root, artifact, atype)
    if errs:
        return Result("rejected", "schema validation failed: " + "; ".join(errs[:5]),
                      "artifact_invalid", {"path": run_rel, "errors": errs[:8]})

    # 3. blocked-status artifacts are a clean "agent could not complete" signal
    if artifact.get("status") == "blocked":
        why = artifact.get("blocked_reason", "")
        return Result("blocked",
                      f"agent reported blocked: {why}. "
                      f"Escalate (wf transition ESCALATED).",
                      "artifact_blocked", {"path": run_rel, "reason": why})

    # 4. semantics. _semantic sets integrity_hold itself on a test-rerun forgery;
    # report that distinctly (callers still treat it as a rejection) but only when
    # THIS check raised it, not when the workflow was already held.
    held_before = bool(st["flags"].get("integrity_hold"))
    sem = _semantic(root, st, atype, version, artifact)
    if sem:
        outcome = "integrity_hold" if (st["flags"].get("integrity_hold")
                                       and not held_before) else "rejected"
        return Result(outcome, sem, "artifact_rejected", {"path": run_rel})

    # 5. counters
    if atype == "plan" and version > 1:
        st["counters"]["plan_revisions"] += 1
    if atype == "implementation" and version > 1:
        st["counters"]["impl_revisions"] += 1
    if atype in REVIEW_TO_ATYPE_COUNTER:
        st["counters"]["review_cycles"] += 1
        st["counters"][REVIEW_TO_ATYPE_COUNTER[atype]] = \
            st["counters"].get(REVIEW_TO_ATYPE_COUNTER[atype], 0) + 1

    # 6. accept: advance the pointer, freeze, record. A raise anywhere here leaves
    # the caller's unsaved st on the floor and trips its fail-closed wrapper.
    st["current"][atype] = version
    S.freeze_artifact(root, wid, run_rel, st["state"])
    fields = {"type": atype, "version": version, "verdict": artifact.get("verdict")}
    if actor is not None:
        fields["actor"] = actor
    if by is not None:
        fields["by"] = by
    ledger.log(root, wid, "artifact_accepted", **fields)
    return Result("accepted", "", None,
                  {"path": run_rel, "type": atype, "version": version})
