"""GMDW artifact acceptance and human-approval core.

Two decisions live here, and nowhere else.

**Artifact acceptance** — the pipeline that turns a file an agent just wrote into
an accepted, frozen, pointer-advancing part of the record:

  1. expected-artifact existence
  2. envelope + structured-Markdown section validation (:mod:`gmdw.parsers`)
  3. blocked-status detection ("agent could not complete")
  4. type-specific semantic cross-checks (path existence, subject match, verdict
     consistency, finding coverage, deviation scope, test re-verification)
  5. counter bumps (plan_revisions / impl_revisions / review_cycles / …)
  6. current-pointer update + freeze (chmod a-w + hash registry)
  7. the ``artifact_accepted`` ledger event

**Human approval** — ``record_approval`` writes the frozen approval artifact
carrying ``human_approved: true``, and ``human_approval_satisfied`` is the check
``state.guard`` calls before it will permit ``FINAL_APPROVAL -> APPROVED``. This
is the v0.3.0 correction to the system's one genuinely unsound guarantee: before,
an LLM reviewer's ``verdict: approve`` was sufficient for the machine to call
work approved. Now the reviewer's verdict is advisory input to a person, and only
a person's signed, frozen record opens that edge.

Three callers share the acceptance core: the deterministic orchestrator (the
normal path since v0.3.0), the SubagentStop validator hook (for anything still
run as a subagent), and ``wf accept`` (human recovery for a stranded artifact).
All three therefore apply *identical* validation — no path can accept anything
another would refuse.

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

from . import ledger, parsers, schemas
from . import state as S

# Directories an Implementor may always touch, plan or no plan: the plan names
# the production files, tests and docs follow from them.
ALWAYS_IN_SCOPE_PREFIXES = ("tests/", "test/", "docs/")

# Gates a human must sign in person. `deployment` is a policy floor and is
# never auto-approvable; `plan` and `final` may be waived only by an explicit
# --auto-approve on an eligible route (see policy.auto_approve_allowed).
HUMAN_GATES = ("plan", "final", "deployment")


@dataclass
class Result:
    """Outcome of one acceptance attempt.

    outcome:  accepted | rejected | blocked | integrity_hold
    reason:   human-readable explanation (fed back to the agent verbatim on a
              retry; printed by the CLI)
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

def _check_repo_analysis(root, st, parsed, verify) -> str | None:
    if not verify.get("path_existence_checks", True):
        return None
    missing = [p for p in parsed.get("paths", [])
               if "/" in p and not (Path(root) / p).exists()]
    if missing:
        return (f"'## File Tree' cites paths that do not exist: "
                f"{sorted(missing)[:5]}. List only files you opened.")
    for note in parsed.get("architecture_notes", []):
        ev = str(note.get("evidence", "")).split(":")[0].strip()
        if ev and "/" in ev and not (Path(root) / ev).exists():
            return f"'## Architecture Notes' evidence path does not exist: {ev}"
    return None


def _check_plan(root, st, parsed, verify) -> str | None:
    if verify.get("path_existence_checks", True):
        for f in parsed.get("files", []):
            if f.get("action") == "modify" and not (Path(root) / f["path"]).exists():
                return (f"'## Files In Scope' marks a non-existent file as "
                        f"'modify': {f['path']} (use 'create', or fix the path)")
    return None


def _check_review(root, st, parsed, verify, version) -> str | None:
    cur_impl = st["current"].get("implementation", 0)
    env = S.artifact_envelope(root, st, "review", version)
    if not S._subject_matches(env, "implementation", cur_impl):
        return (f"envelope subject_ref must name the current implementation "
                f"({S.artifact_relpath('implementation', cur_impl)}); "
                f"got {env.get('subject_ref')!r}")
    verdict = parsed.get("verdict")
    open_bm = [f for f in parsed.get("findings", [])
               if f.get("severity") in ("blocker", "major")]
    if verdict == "approve" and open_bm:
        return (f"'## Verdict' is 'approve' with open blocker/major findings: "
                f"{[f['id'] for f in open_bm]}. Downgrade the findings or the "
                f"verdict — not both readings can be true.")
    if S.security_required(st) and not parsed.get("checked"):
        return ("this workflow is security-flagged, so '## Security Checklist' "
                "must record the areas actually checked (an empty checklist is "
                "not an approval)")
    return None


def _check_implementation(root, st, parsed, verify, version) -> str | None:
    base = parsed.get("base_commit", "")
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"],
                           cwd=root, capture_output=True, timeout=15)
        if r.returncode != 0:
            return (f"base_commit {base[:12]} is not an ancestor of HEAD "
                    f"(stale workspace / concurrent change)")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass  # no git or timeout: skip ancestry, other checks still apply

    # scope: changed files subset of plan files ∪ tests ∪ docs ∪ declared deviations
    plan = S.load_artifact(root, st, "plan")
    planned = {f["path"] for f in (plan or {}).get("files", [])}
    devs = {d["what"] for d in parsed.get("deviations", [])}
    for ch in parsed.get("changes", []):
        p = ch["path"]
        if p in planned or p.startswith(ALWAYS_IN_SCOPE_PREFIXES):
            continue
        if any(p in d or d in p for d in devs):
            continue
        return (f"'## Changes' lists a file outside the plan's '## Files In "
                f"Scope' and undeclared as a deviation: {p}")

    # rework coverage: v2+ must say what it did about each blocker/major finding
    if version > 1:
        review = S.load_artifact(root, st, "review")
        must = {f["id"] for f in (review or {}).get("findings", [])
                if f.get("severity") in ("blocker", "major")}
        covered = {r["finding_id"].upper()
                   for r in parsed.get("findings_addressed", [])}
        missing = {m for m in must if m.upper() not in covered}
        if missing:
            return (f"this is a rework, so '## Findings Addressed' must have a "
                    f"row for every blocker/major finding; missing: "
                    f"{sorted(missing)}")

    # test re-verification (profile-gated) — the highest-severity check
    if verify.get("rerun_tests", True):
        declared = parsed.get("tests", {}).get("result")
        cmd = (parsed.get("tests", {}).get("command") or "").strip()
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


def _semantic(root: Path, st: dict, atype: str, version: int,
              parsed: dict) -> str | None:
    """Return an error string if the artifact is semantically invalid."""
    verify = st["policy"]["snapshot"]["verification"]
    if atype == "repo_analysis":
        return _check_repo_analysis(root, st, parsed, verify)
    if atype == "plan":
        return _check_plan(root, st, parsed, verify)
    if atype == "review":
        return _check_review(root, st, parsed, verify, version)
    if atype == "implementation":
        return _check_implementation(root, st, parsed, verify, version)
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
    on the acceptance event only when supplied, so an orchestrator acceptance,
    a hook acceptance and a human recovery via ``wf accept`` stay distinguishable
    in the audit trail.

    An ``accepted`` result always implies the artifact was frozen.
    """
    wid = st["workflow"]["id"]
    run_rel = S.artifact_relpath(atype, version)
    full = S.workflow_dir(root, wid) / run_rel

    # 1. existence
    if not full.exists():
        return Result("rejected", f"expected artifact not produced: {run_rel}",
                      "artifact_missing", {"path": run_rel})

    # 2. envelope + sections
    try:
        text = full.read_text(encoding="utf-8")
    except OSError as e:
        return Result("rejected", f"artifact is unreadable: {e}",
                      "artifact_invalid", {"path": run_rel})

    options = {}
    if atype == "review":
        options = {"require_acceptance_criteria": True,
                   "require_security_checklist": S.security_required(st)}
    errs = parsers.validate_artifact(text, atype, workflow_id=wid,
                                     version=version, **options)
    if errs:
        return Result("rejected",
                      "artifact validation failed:\n  - " + "\n  - ".join(errs[:6]),
                      "artifact_invalid", {"path": run_rel, "errors": errs[:8]})

    doc, parsed = parsers.parse_artifact(text, atype)

    # 3. blocked-status artifacts are a clean "agent could not complete" signal
    if parsers.is_blocked(doc.envelope):
        why = doc.envelope.get("blocked_reason", "")
        return Result("blocked",
                      f"agent reported blocked: {why}. "
                      f"Escalate (wf transition ESCALATED).",
                      "artifact_blocked", {"path": run_rel, "reason": why})

    # 4. semantics. _semantic sets integrity_hold itself on a test-rerun forgery;
    # report that distinctly (callers still treat it as a rejection) but only when
    # THIS check raised it, not when the workflow was already held.
    held_before = bool(st["flags"].get("integrity_hold"))
    sem = _semantic(root, st, atype, version, parsed)
    if sem:
        outcome = "integrity_hold" if (st["flags"].get("integrity_hold")
                                       and not held_before) else "rejected"
        return Result(outcome, sem, "artifact_rejected", {"path": run_rel})

    # 5. counters
    if atype == "plan" and version > 1:
        st["counters"]["plan_revisions"] += 1
    if atype == "implementation" and version > 1:
        st["counters"]["impl_revisions"] += 1
    if atype in S.REVIEW_TYPES:
        st["counters"]["review_cycles"] += 1
        st["counters"]["reviews"] = st["counters"].get("reviews", 0) + 1

    # 6. accept: advance the pointer, freeze, record. A raise anywhere here leaves
    # the caller's unsaved st on the floor and trips its fail-closed wrapper.
    st["current"][atype] = version
    S.freeze_artifact(root, wid, run_rel, st["state"])
    fields = {"type": atype, "version": version, "path": run_rel,
              "verdict": parsed.get("verdict")}
    if actor is not None:
        fields["actor"] = actor
    if by is not None:
        fields["by"] = by
    ledger.log(root, wid, "artifact_accepted", **fields)
    return Result("accepted", "", None,
                  {"path": run_rel, "type": atype, "version": version})


def accept_stage(root, st: dict, exp: dict, *, actor: str | None = None,
                 by: str | None = None) -> Result:
    """Accept every artifact the current stage owes.

    The Analyst-Planner writes three artifacts in one invocation, so acceptance
    is per-stage, not per-artifact. Optional artifacts (an unchanged analysis on
    a re-plan) are accepted when present and skipped when absent; a required one
    that is missing or invalid fails the whole stage, and nothing is left
    half-accepted for the guard to trip over — the required primary is accepted
    last, so a rejection anywhere leaves the state's own pointer unmoved.
    """
    # A stage that declares itself blocked is answering the question, not
    # failing to. Report that before demanding the other artifacts it is
    # explaining away — "you forgot classification_v1.md" is a useless thing to
    # say to an agent that just told you the request cannot be planned.
    primary = exp["primary"]
    full = S.workflow_dir(root, st["workflow"]["id"]) / primary["run_rel"]
    if full.exists():
        try:
            envelope = parsers.parse_document(
                full.read_text(encoding="utf-8")).envelope
        except OSError:
            envelope = {}
        if parsers.is_blocked(envelope):
            why = envelope.get("blocked_reason", "")
            return Result("blocked",
                          f"agent reported blocked: {why}. "
                          f"Escalate (wf transition ESCALATED).",
                          "artifact_blocked",
                          {"path": primary["run_rel"], "reason": why})

    optional = [a for a in exp["artifacts"] if not a["required"]]
    required = [a for a in exp["artifacts"] if a["required"]]
    accepted = []

    for spec in optional:
        full = S.workflow_dir(root, st["workflow"]["id"]) / spec["run_rel"]
        if not full.exists():
            continue
        res = accept_artifact(root, st, spec["artifact_type"], spec["version"],
                              actor=actor, by=by)
        if not res.accepted:
            return res
        accepted.append(res.extra)

    # The primary is required and goes last: its pointer is what the guard reads.
    for spec in sorted(required, key=lambda a: a["artifact_type"] ==
                       exp["primary"]["artifact_type"]):
        res = accept_artifact(root, st, spec["artifact_type"], spec["version"],
                              actor=actor, by=by)
        if not res.accepted:
            return res
        accepted.append(res.extra)

    return Result("accepted", "", None, {"accepted": accepted})


# --------------------------------------------------------------------------- #
# Human approval
# --------------------------------------------------------------------------- #

def approval_relpath(gate: str, index: int) -> str:
    if gate == "escalation":
        return f"artifacts/approvals/escalation_{index}.json"
    return f"artifacts/approvals/{gate}_approval_{index}.json"


def record_approval(root, st: dict, *, gate: str, decision: str, by: str,
                    subject: str = "", notes: str = "",
                    resolution: str | None = None,
                    overridden_findings=None, auto: bool = False) -> Result:
    """Write and freeze one human-decision artifact, and mirror it into state.

    ``human_approved`` is stamped true only for a decision that actually grants
    permission, and ``auto_approved`` records when the permission came from an
    explicit ``--auto-approve`` rather than a person at a prompt. The two fields
    are separate on purpose: an auditor must be able to ask "did a human look at
    this?" and get a truthful answer, not one inferred from the decision word.
    """
    wid = st["workflow"]["id"]
    now = S.utcnow()
    grants = decision in ("approved", "force_approved")
    index = 1 + sum(1 for a in st["approvals"] if a["gate"] == gate)
    rel = approval_relpath(gate, index)

    artifact = {
        "artifact_type": "approval",
        "workflow_id": wid,
        "version": index,
        "produced_by": "human",
        "input_artifact_refs": [subject] if subject else [],
        "created_at": now,
        "status": "complete",
        "gate": gate,
        "decision": decision,
        "human_approved": bool(grants and not auto),
        "auto_approved": bool(auto),
        "by": by,
        "at": now,
        "subject_ref": subject or "",
        "notes": notes or "",
    }
    if resolution:
        artifact["resolution"] = resolution
    if overridden_findings:
        artifact["overridden_findings"] = list(overridden_findings)

    errs = schemas.validate_artifact(root, artifact, "approval")
    if errs:
        return Result("rejected", "approval artifact invalid: " + "; ".join(errs),
                      "approval_invalid", {"path": rel, "errors": errs})

    full = S.workflow_dir(root, wid) / rel
    if full.exists():
        return Result("rejected",
                      f"approval artifact already exists: {rel} "
                      f"(approvals are immutable)", "approval_invalid",
                      {"path": rel})
    S.atomic_write_json(full, artifact)
    S.freeze_artifact(root, wid, rel, st["state"])

    st["approvals"].append({"gate": gate, "decision": decision, "by": by,
                            "at": now, "artifact_ref": rel,
                            "human_approved": artifact["human_approved"],
                            "auto_approved": artifact["auto_approved"],
                            "notes": notes or ""})
    if gate in st.get("human_approved", {}):
        st["human_approved"][gate] = grants
    if gate == "plan" and grants:
        st["plan_approved"] = True
    if gate == "escalation" and st["escalations"]:
        st["escalations"][-1]["resolution"] = resolution
        st["escalations"][-1]["resolved_at"] = now
    st["current"]["approval"] = st["current"].get("approval", 0) + 1

    ledger.log(root, wid, "human_approval", gate=gate, decision=decision, by=by,
               human_approved=artifact["human_approved"],
               auto_approved=artifact["auto_approved"],
               resolution=resolution, artifact=rel)
    return Result("accepted", "", None, {"gate": gate, "decision": decision,
                                         "artifact": rel})


def human_approval_satisfied(root, st: dict, gate: str,
                             subject: str | None = None) -> tuple:
    """(ok, reason) — is there a frozen, permission-granting record for ``gate``?

    ``state.guard`` calls this before ``FINAL_APPROVAL -> APPROVED`` and
    ``PLAN_APPROVAL -> IMPLEMENTING``. It reads the artifact on disk, not the
    state mirror, so a workflow whose state.json was edited out from under the
    registry still cannot conjure an approval: the record has to exist, carry
    ``human_approved`` or ``auto_approved``, and match its frozen hash.
    """
    entries = [a for a in st.get("approvals", [])
               if a["gate"] == gate
               and a["decision"] in ("approved", "force_approved")]
    if not entries:
        return False, (f"no {gate} approval on record; a human must sign this "
                       f"gate (wf approve {gate} --decision approved --by <you>)")
    latest = entries[-1]
    rel = latest.get("artifact_ref", "")
    wid = st["workflow"]["id"]
    full = S.workflow_dir(root, wid) / rel
    if not rel or not full.exists():
        return False, (f"the {gate} approval record names {rel or '<nothing>'}, "
                       f"which is not on disk; approvals must be reconstructable")
    entry = ledger.entry(root, wid, rel)
    if not entry or not entry.get("frozen"):
        return False, f"the {gate} approval artifact {rel} is not frozen"
    if ledger.sha256_file(full) != entry["sha256"]:
        return False, f"the {gate} approval artifact {rel} has been modified"
    try:
        record = json.loads(full.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"the {gate} approval artifact {rel} is unreadable: {e}"
    if not (record.get("human_approved") or record.get("auto_approved")):
        return False, (f"the {gate} approval artifact {rel} does not carry "
                       f"human_approved: true")
    if subject and not str(record.get("subject_ref", "")).endswith(subject):
        return False, (f"the {gate} approval on record signs "
                       f"{record.get('subject_ref') or '<nothing>'}, not the "
                       f"current {subject}; this version has not been approved")
    return True, ""
