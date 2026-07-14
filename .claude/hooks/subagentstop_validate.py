#!/usr/bin/env python3
"""GMDW SubagentStop validator — artifact acceptance gate.

When a subagent finishes, this hook decides whether its artifact is ACCEPTED
(advancing the workflow) or REJECTED (a failed invocation the Orchestrator may
retry once). It performs, in order:

  1. expected-artifact existence
  2. envelope + type schema validation
  3. type-specific semantic cross-checks (path existence, subject match,
     verdict consistency, deviation scope, test re-verification)
  4. counter bumps (plan_revisions / impl_revisions / review_cycles / …)
  5. current-pointer update + freeze (chmod a-w + hash registry)

FAIL-CLOSED: on rejection it bumps consecutive_failures and logs the reason;
it does not advance any pointer. On internal error while a workflow is active
it records an integrity_hold. It uses the "block" reason channel to push the
rejection reason back to the model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmdw import heartbeat, ledger, schemas  # noqa: E402
from gmdw import state as S       # noqa: E402
from gmdw.state import GmdwError  # noqa: E402


def block(reason: str):
    """Push a rejection reason back to the model (does not stop the agent
    again; the Orchestrator reads it and retries-once or escalates)."""
    print(json.dumps({"decision": "block", "reason": f"[GMDW] {reason}"}))
    sys.exit(0)


def ok():
    sys.exit(0)


def _fail(root, wid, st, reason: str, event: str, **extra):
    st["counters"]["consecutive_failures"] = \
        st["counters"].get("consecutive_failures", 0) + 1
    # Keep open_invocation set: the block reason is fed back to the agent, which
    # may correct the artifact and stop again. Retaining the active invocation
    # (with its expected_type/version) lets that corrected re-stop be re-validated
    # and accepted, instead of stranding a valid-but-unaccepted artifact. If the
    # agent gives up instead, PostToolUse clears open_invocation when the Agent
    # tool returns, so this never deadlocks the next invocation.
    S.save_state(root, wid, st)
    ledger.log(root, wid, event, reason=reason,
               consecutive_failures=st["counters"]["consecutive_failures"], **extra)
    block(reason)


# --------------------------------------------------------------------------- #
# Semantic checks per artifact type
# --------------------------------------------------------------------------- #

def _subject_matches(review: dict, atype: str, version: int) -> bool:
    subject = str(review.get("subject_ref", ""))
    if atype == "implementation":
        return f"implementation_v{version}/" in subject or \
               subject.endswith(f"implementation_v{version}")
    return subject.endswith(f"{atype}_v{version}.json")


def _semantic(root: Path, st: dict, atype: str, version: int, artifact: dict,
              run_rel: str) -> str | None:
    """Return an error string if the artifact is semantically invalid."""
    prof = st["policy"]["snapshot"]
    verify = prof["verification"]
    wdir = S.workflow_dir(root, st["workflow"]["id"])

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

REVIEW_TO_ATYPE_COUNTER = S.REVIEW_COUNTER


def main():
    data = json.load(sys.stdin)
    cwd = data.get("cwd") or os.getcwd()
    try:
        root = S.find_root(cwd)
    except GmdwError:
        ok()
    heartbeat.touch(root, "SubagentStop")  # attest the enforcement plane is live
    wid = S.active_id(root)
    if not wid:
        ok()
    _, st = S.load_state(root, wid)

    # Which artifact was this invocation expected to produce? The ACTIVE
    # invocation (open_invocation, set by the gate with expected_type/version)
    # is authoritative here: SubagentStop fires before PostToolUse clears it,
    # so it is still current. last_invocation is only a fallback (e.g. tests
    # that seed it directly) and can be a stale prior record without an
    # expected_type -- preferring it would skip validation on every 2nd+
    # invocation in a workflow.
    inv = st.get("open_invocation") or st.get("last_invocation")
    if not inv:
        ok()  # not a GMDW agent invocation we track
    atype = inv.get("expected_type")
    version = inv.get("version")
    if not atype or not version:
        ok()

    run_rel = S.artifact_relpath(atype, version)
    full = S.workflow_dir(root, wid) / run_rel

    # 1. existence
    if not full.exists():
        _fail(root, wid, st, f"expected artifact not produced: {run_rel}",
              "artifact_missing", path=run_rel)

    # 2. schema
    try:
        artifact = json.loads(full.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _fail(root, wid, st, f"artifact is not valid JSON: {e}",
              "artifact_invalid", path=run_rel)
    errs = schemas.validate_artifact(root, artifact, atype)
    if errs:
        _fail(root, wid, st, "schema validation failed: " + "; ".join(errs[:5]),
              "artifact_invalid", path=run_rel, errors=errs[:8])

    # blocked-status artifacts are a clean "agent could not complete" signal
    if artifact.get("status") == "blocked":
        st["open_invocation"] = None
        S.save_state(root, wid, st)
        ledger.log(root, wid, "artifact_blocked", path=run_rel,
                   reason=artifact.get("blocked_reason", ""))
        block(f"agent reported blocked: {artifact.get('blocked_reason','')}. "
              f"Escalate (wf transition ESCALATED).")

    # 3. semantics
    sem = _semantic(root, st, atype, version, artifact, run_rel)
    if sem:
        _fail(root, wid, st, sem, "artifact_rejected", path=run_rel)

    # 4. counters
    if atype == "plan" and version > 1:
        st["counters"]["plan_revisions"] += 1
    if atype == "implementation" and version > 1:
        st["counters"]["impl_revisions"] += 1
    if atype in REVIEW_TO_ATYPE_COUNTER:
        st["counters"]["review_cycles"] += 1
        st["counters"][REVIEW_TO_ATYPE_COUNTER[atype]] = \
            st["counters"].get(REVIEW_TO_ATYPE_COUNTER[atype], 0) + 1

    # 5. accept: update pointer, freeze, clear invocation, reset failures
    st["current"][atype] = version
    st["counters"]["consecutive_failures"] = 0
    st["open_invocation"] = None
    S.save_state(root, wid, st)
    S.freeze_artifact(root, wid, run_rel, st["state"])
    verdict = artifact.get("verdict")
    ledger.log(root, wid, "artifact_accepted", type=atype, version=version,
               verdict=verdict)
    ok()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL CLOSED: freeze the workflow for human review
        try:
            r = S.find_root(os.getcwd())
            wid = S.active_id(r)
            if wid:
                _, st = S.load_state(r, wid)
                st["flags"]["integrity_hold"] = True
                S.save_state(r, wid, st)
                ledger.log(r, wid, "validator_error",
                           error=f"{type(e).__name__}: {e}")
                block(f"validator internal error (integrity hold set): "
                      f"{type(e).__name__}: {e}")
        except Exception:
            pass
        sys.exit(0)
