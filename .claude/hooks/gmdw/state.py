"""GMDW state engine — workflow lifecycle, transition table, guards, versioning.

state.json is mutated ONLY through this module (via the wf CLI and the hooks).
All writes are atomic (tmp file + os.replace). Guards read artifacts from disk,
never conversation memory, so every decision is reconstructable.
"""
from __future__ import annotations

import datetime
import json
import os
import stat
from pathlib import Path

from . import ledger, policy, schemas


class GmdwError(Exception):
    """Raised when a governance rule rejects an operation."""


# --------------------------------------------------------------------------- #
# State machine definition
# --------------------------------------------------------------------------- #

STATES = [
    "RECEIVED", "CLASSIFYING", "REPOSITORY_ANALYSIS", "PLANNING", "PLAN_REVIEW",
    "PLAN_SECURITY_REVIEW", "PLAN_APPROVAL", "IMPLEMENTING", "SECURITY_REVIEW",
    "CODE_REVIEW", "APPROVED", "ESCALATED", "COMPLETED", "FAILED", "REJECTED",
]

TERMINAL = {"COMPLETED", "FAILED", "REJECTED"}

TRANSITIONS = {
    "RECEIVED":             {"CLASSIFYING"},
    "CLASSIFYING":          {"REPOSITORY_ANALYSIS", "IMPLEMENTING", "REJECTED"},
    "REPOSITORY_ANALYSIS":  {"PLANNING", "IMPLEMENTING", "ESCALATED"},
    "PLANNING":             {"PLAN_REVIEW", "PLAN_APPROVAL", "IMPLEMENTING",
                             "ESCALATED", "FAILED"},
    "PLAN_REVIEW":          {"PLANNING", "PLAN_SECURITY_REVIEW", "PLAN_APPROVAL",
                             "IMPLEMENTING", "ESCALATED"},
    "PLAN_SECURITY_REVIEW": {"PLANNING", "PLAN_APPROVAL", "IMPLEMENTING", "ESCALATED"},
    "PLAN_APPROVAL":        {"IMPLEMENTING", "PLANNING", "REJECTED"},
    "IMPLEMENTING":         {"SECURITY_REVIEW", "CODE_REVIEW", "ESCALATED", "FAILED"},
    "SECURITY_REVIEW":      {"CODE_REVIEW", "IMPLEMENTING", "ESCALATED"},
    "CODE_REVIEW":          {"APPROVED", "IMPLEMENTING", "ESCALATED"},
    "APPROVED":             {"COMPLETED", "FAILED", "ESCALATED"},
    "ESCALATED":            {"PLANNING", "IMPLEMENTING", "APPROVED", "REJECTED"},
    "COMPLETED": set(), "FAILED": set(), "REJECTED": set(),
}

AGENT_FOR_STATE = {
    "REPOSITORY_ANALYSIS":  "repo-analyst",
    "PLANNING":             "planner",
    "PLAN_REVIEW":          "plan-reviewer",
    "PLAN_SECURITY_REVIEW": "security-reviewer",
    "IMPLEMENTING":         "implementor",
    "SECURITY_REVIEW":      "security-reviewer",
    "CODE_REVIEW":          "code-reviewer",
}

# state -> artifact type produced in that state
ARTIFACT_FOR_STATE = {
    "CLASSIFYING":          "classification",
    "REPOSITORY_ANALYSIS":  "repo_analysis",
    "PLANNING":             "plan",
    "PLAN_REVIEW":          "plan_review",
    "PLAN_SECURITY_REVIEW": "security_review_plan",
    "IMPLEMENTING":         "implementation",
    "SECURITY_REVIEW":      "security_review_code",
    "CODE_REVIEW":          "code_review",
}

# artifact type -> value the artifact's envelope `artifact_type` must carry
ENVELOPE_TYPE = {
    "implementation": "implementation_manifest",
}

REVIEW_TYPES = {"plan_review", "security_review_plan", "security_review_code",
                "code_review"}
REVIEW_COUNTER = {
    "plan_review": "plan_reviews",
    "security_review_plan": "security_reviews",
    "security_review_code": "security_reviews",
    "code_review": "code_reviews",
}

# resolution recorded on the pending escalation -> the ONLY state it unlocks
ESCALATION_TARGET = {
    "redirect": "PLANNING",
    "override": "IMPLEMENTING",
    "force_approve": "APPROVED",
    "abandon": "REJECTED",
}


# --------------------------------------------------------------------------- #
# Filesystem primitives
# --------------------------------------------------------------------------- #

def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_root(start=None) -> Path:
    p = Path(start or os.getcwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".workflow").is_dir():
            return cand
    raise GmdwError(f"not inside a GMDW repository (no .workflow/ above {p})")


def workflow_dir(root, wf_id: str) -> Path:
    return Path(root) / ".workflow" / "runs" / wf_id


def active_id(root) -> str | None:
    f = Path(root) / ".workflow" / "ACTIVE"
    if f.exists():
        v = f.read_text(encoding="utf-8").strip()
        return v or None
    return None


def atomic_write_json(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def load_state(root, wf_id: str | None = None):
    wf_id = wf_id or active_id(root)
    if not wf_id:
        raise GmdwError("no ACTIVE workflow")
    p = workflow_dir(root, wf_id) / "state.json"
    if not p.exists():
        raise GmdwError(f"state.json missing for {wf_id}")
    return wf_id, json.loads(p.read_text(encoding="utf-8"))


def save_state(root, wf_id: str, st: dict) -> None:
    atomic_write_json(workflow_dir(root, wf_id) / "state.json", st)


# --------------------------------------------------------------------------- #
# Artifact addressing & versioning
# --------------------------------------------------------------------------- #

def artifact_relpath(atype: str, version: int) -> str:
    """Run-dir-relative path of an artifact version."""
    if atype == "implementation":
        return f"artifacts/implementation_v{version}/manifest.json"
    return f"artifacts/{atype}_v{version}.json"


def repo_relpath(wf_id: str, atype: str, version: int) -> str:
    return f".workflow/runs/{wf_id}/{artifact_relpath(atype, version)}"


def load_artifact(root, st: dict, atype: str, version: int | None = None):
    version = version if version is not None else st["current"].get(atype, 0)
    if version < 1:
        return None
    p = workflow_dir(root, st["workflow"]["id"]) / artifact_relpath(atype, version)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def expected(root, st: dict):
    """What the current state is waiting for: artifact type, next version,
    repo-relative path, producing agent, and the policy-assigned model."""
    s = st["state"]
    atype = ARTIFACT_FOR_STATE.get(s)
    if not atype:
        return None
    version = st["current"].get(atype, 0) + 1
    agent = AGENT_FOR_STATE.get(s)
    prof = st["policy"]["snapshot"]
    return {
        "state": s,
        "artifact_type": atype,
        "envelope_type": ENVELOPE_TYPE.get(atype, atype),
        "version": version,
        "path": repo_relpath(st["workflow"]["id"], atype, version),
        "agent": agent,
        "model": policy.model_for_role(prof, agent) if agent else None,
        "effort": policy.effort_for_role(prof, agent) if agent else None,
        "schema": schemas.SCHEMA_FOR_TYPE[atype],
    }


def freeze_artifact(root, wf_id: str, run_rel: str, state_name: str) -> None:
    """Make an accepted artifact immutable: registry-freeze + chmod a-w."""
    full = workflow_dir(root, wf_id) / run_rel
    ledger.freeze(root, wf_id, run_rel, state_name=state_name)
    try:
        mode = full.stat().st_mode
        full.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass  # registry hash remains the authoritative tamper check


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def _sec_required(st: dict, prof: dict, stage: str) -> bool:
    if st["flags"].get("security") or st["flags"].get("data_model"):
        return True
    if stage == "plan" and prof["security"].get("always_review_plans"):
        return True
    return False


def _has_approval(st: dict, gate: str,
                  decisions=("approved", "force_approved")) -> bool:
    return any(a["gate"] == gate and a["decision"] in decisions
               for a in st.get("approvals", []))


def _subject_matches(review: dict, atype: str, version: int) -> bool:
    subject = str(review.get("subject_ref", ""))
    if atype == "implementation":
        return f"implementation_v{version}/" in subject or \
               subject.endswith(f"implementation_v{version}")
    return subject.endswith(f"{atype}_v{version}.json")


def _classification(root, st: dict):
    cls = load_artifact(root, st, "classification")
    if cls is None:
        return None, "classification artifact required"
    errs = schemas.validate_artifact(root, cls, "classification")
    if errs:
        return None, "classification artifact invalid: " + "; ".join(errs[:3])
    return cls, ""


def guard(root, st: dict, to: str):
    """Return (ok, reason). Assumes `to` is already in the transition table."""
    s = st["state"]
    prof = st["policy"]["snapshot"]
    c, lim, cur = st["counters"], st["limits"], st["current"]

    # ---- global rules -----------------------------------------------------
    if st.get("open_invocation"):
        return False, "an agent invocation is still open; cannot transition"
    if st["flags"].get("integrity_hold") and to not in ("ESCALATED", "FAILED"):
        return False, "integrity hold in effect; only ESCALATED/FAILED reachable"
    if to == "ESCALATED":
        return True, ""
    if to in ("FAILED", "REJECTED") and s != "ESCALATED":
        return True, ""

    # ---- per-state rules ----------------------------------------------------
    if s == "RECEIVED":
        return True, ""

    if s == "CLASSIFYING":
        cls, why = _classification(root, st)
        if cls is None:
            return False, why
        if to == "IMPLEMENTING":
            if cls["route"] != "trivial":
                return False, "direct implementation requires route=trivial"
            if cls["override_flags"]:
                return False, "flagged workflows may not take the trivial route"
        return True, ""

    if s == "REPOSITORY_ANALYSIS":
        if cur.get("repo_analysis", 0) < 1:
            return False, "repo_analysis artifact required"
        if to == "IMPLEMENTING":
            cls, why = _classification(root, st)
            if cls is None:
                return False, why
            if cls["route"] != "trivial":
                return False, "route is not trivial"
            if not _has_approval(st, "route_downgrade"):
                return False, "route downgrade to trivial requires human approval " \
                              "(wf approve route_downgrade)"
        return True, ""

    if s == "PLANNING":
        if cur.get("plan", 0) < 1:
            return False, "plan artifact required"
        if to == "PLAN_REVIEW":
            return True, ""
        # skipping agent plan review is a medium-route privilege only
        if st["route"] != "medium":
            return False, "route requires PLAN_REVIEW before implementation"
        mode = policy.gate_mode(prof, st["route"])
        if to == "IMPLEMENTING":
            return (mode != "blocking",
                    "plan approval gate is blocking; go to PLAN_APPROVAL")
        if to == "PLAN_APPROVAL":
            return (mode == "blocking",
                    "plan gate not blocking on this route; go to IMPLEMENTING")
        return True, ""

    if s == "PLAN_REVIEW":
        rev = load_artifact(root, st, "plan_review")
        if rev is None:
            return False, "plan_review artifact required"
        if not _subject_matches(rev, "plan", cur.get("plan", 0)):
            return False, "latest plan_review does not review the current plan version"
        verdict = rev.get("verdict")
        if to == "PLANNING":
            if verdict != "revise":
                return False, f"re-planning requires verdict=revise (got {verdict})"
            if c["plan_revisions"] >= lim["max_plan_revisions"]:
                return False, "plan revision limit reached; escalate"
            return True, ""
        if verdict != "approve":
            return False, f"cannot advance: plan_review verdict is {verdict}"
        sec = _sec_required(st, prof, "plan")
        if to == "PLAN_SECURITY_REVIEW":
            return (sec, "security review of the plan is not required")
        if sec:
            return False, "security-flagged plan must pass PLAN_SECURITY_REVIEW"
        mode = policy.gate_mode(prof, st["route"])
        if to == "PLAN_APPROVAL":
            return (mode == "blocking", "plan gate not blocking; go to IMPLEMENTING")
        if to == "IMPLEMENTING":
            return (mode != "blocking", "plan gate is blocking; go to PLAN_APPROVAL")

    if s == "PLAN_SECURITY_REVIEW":
        rev = load_artifact(root, st, "security_review_plan")
        if rev is None:
            return False, "security_review_plan artifact required"
        if not _subject_matches(rev, "plan", cur.get("plan", 0)):
            return False, "latest plan security review does not review the current plan"
        verdict = rev.get("verdict")
        if to == "PLANNING":
            if verdict != "revise":
                return False, f"re-planning requires verdict=revise (got {verdict})"
            if c["plan_revisions"] >= lim["max_plan_revisions"]:
                return False, "plan revision limit reached; escalate"
            return True, ""
        if verdict != "approve":
            return False, f"cannot advance: security verdict is {verdict}"
        mode = policy.gate_mode(prof, st["route"])
        if to == "PLAN_APPROVAL":
            return (mode == "blocking", "plan gate not blocking; go to IMPLEMENTING")
        if to == "IMPLEMENTING":
            return (mode != "blocking", "plan gate is blocking; go to PLAN_APPROVAL")

    if s == "PLAN_APPROVAL":
        if to == "IMPLEMENTING":
            return (_has_approval(st, "plan"),
                    "plan approval record required (wf approve plan)")
        if to == "PLANNING":
            if not _has_approval(st, "plan", decisions=("changes_requested",)):
                return False, "returning to PLANNING requires decision=changes_requested"
            if c["plan_revisions"] >= lim["max_plan_revisions"]:
                return False, "plan revision limit reached; escalate"
            return True, ""

    if s == "IMPLEMENTING":
        if cur.get("implementation", 0) < 1:
            return False, "implementation artifact required"
        sec = _sec_required(st, prof, "code")
        if to == "SECURITY_REVIEW":
            return (sec, "security review not required for this workflow")
        if to == "CODE_REVIEW":
            return (not sec,
                    "security-flagged implementation must pass SECURITY_REVIEW first")

    if s == "SECURITY_REVIEW":
        rev = load_artifact(root, st, "security_review_code")
        if rev is None:
            return False, "security_review_code artifact required"
        if not _subject_matches(rev, "implementation", cur.get("implementation", 0)):
            return False, "latest security review does not review the current implementation"
        verdict = rev.get("verdict")
        if to == "CODE_REVIEW":
            return (verdict == "approve", f"security verdict is {verdict}")
        if to == "IMPLEMENTING":
            if verdict != "revise":
                return False, f"rework requires verdict=revise (got {verdict})"
            if c["impl_revisions"] >= lim["max_impl_revisions"]:
                return False, "implementation revision limit reached; escalate"
            return True, ""

    if s == "CODE_REVIEW":
        rev = load_artifact(root, st, "code_review")
        if rev is None:
            return False, "code_review artifact required"
        if not _subject_matches(rev, "implementation", cur.get("implementation", 0)):
            return False, "latest code_review does not review the current implementation"
        verdict = rev.get("verdict")
        if to == "IMPLEMENTING":
            if verdict != "revise":
                return False, f"rework requires verdict=revise (got {verdict})"
            if c["impl_revisions"] >= lim["max_impl_revisions"]:
                return False, "implementation revision limit reached; escalate"
            return True, ""
        if to == "APPROVED":
            if verdict != "approve":
                return False, f"code_review verdict is {verdict}"
            acs = rev.get("acceptance_criteria", [])
            if st["route"] != "trivial":
                if not acs:
                    return False, "code_review must evaluate acceptance criteria"
                bad = [a["id"] for a in acs if a.get("status") != "pass"]
                if bad:
                    return False, f"acceptance criteria not passing: {bad}"
            if _sec_required(st, prof, "code"):
                sec = load_artifact(root, st, "security_review_code")
                if not sec or sec.get("verdict") != "approve" or \
                        not _subject_matches(sec, "implementation",
                                             cur.get("implementation", 0)):
                    return False, "security approval for the current implementation required"
            return True, ""

    if s == "APPROVED":
        if to == "COMPLETED":
            if not _has_approval(st, "deployment"):
                return False, "deployment approval required (wf approve deployment)"
            if prof["verification"].get("tamper_check_on_finalize", True):
                bad = ledger.verify(root, st["workflow"]["id"])
                if bad:
                    return False, f"artifact tamper check failed: {bad}"
            return True, ""

    if s == "ESCALATED":
        if not st["escalations"]:
            return False, "no escalation on record"
        last = st["escalations"][-1]
        res = last.get("resolution", "pending")
        if res == "pending":
            return False, "escalation unresolved (wf approve escalation --resolution ...)"
        allowed = ESCALATION_TARGET.get(res)
        return (to == allowed,
                f"escalation resolution '{res}' only permits transition to {allowed}")

    return False, f"no guard permits {s} -> {to}"


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #

def _sync_routing(root, st: dict) -> None:
    """After classification/analysis, copy route + flags from the latest
    classification artifact into state (the artifact is the source of truth)."""
    cls = load_artifact(root, st, "classification")
    if cls:
        st["route"] = cls["route"]
        st["flags"]["security"] = "security" in cls.get("override_flags", [])
        st["flags"]["data_model"] = "data_model" in cls.get("override_flags", [])


def transition(root, to: str, trigger: str, actor: str = "orchestrator",
               reason: str | None = None, detail: str | None = None) -> dict:
    wf_id, st = load_state(root)
    frm = st["state"]
    if to not in TRANSITIONS.get(frm, set()):
        raise GmdwError(f"illegal transition {frm} -> {to} (not in transition table)")
    ok, why = guard(root, st, to)
    if not ok:
        raise GmdwError(f"guard rejected {frm} -> {to}: {why}")

    if frm in ("CLASSIFYING", "REPOSITORY_ANALYSIS"):
        _sync_routing(root, st)

    st["state"] = to
    st["state_entered_at"] = utcnow()
    st["history"].append({"ts": utcnow(), "from": frm, "to": to,
                          "trigger": trigger, "actor": actor})
    if to == "ESCALATED":
        st["escalations"].append({
            "at": utcnow(), "from_state": frm,
            "reason": reason or "other", "detail": detail or "",
            "resolution": "pending", "resolved_at": None,
        })
    save_state(root, wf_id, st)
    ledger.log(root, wf_id, "transition", from_state=frm, to_state=to,
               trigger=trigger, actor=actor,
               counters=dict(st["counters"]))
    return st


# --------------------------------------------------------------------------- #
# Workflow creation
# --------------------------------------------------------------------------- #

def create_workflow(root, request_text: str, title: str, created_by: str,
                    profile_name: str | None = None) -> str:
    root = Path(root)
    if active_id(root):
        raise GmdwError(f"workflow {active_id(root)} is already ACTIVE; "
                        f"close it before starting another")
    cfg = json.loads((root / ".workflow" / "config.json").read_text(encoding="utf-8"))
    pname = profile_name or cfg.get("policy_profile", "standard")
    prof, sha = policy.load_profile(root, pname)
    errs = policy.validate_profile(prof)
    if errs:
        raise GmdwError(f"policy profile '{pname}' failed validation: {errs}")

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    runs = root / ".workflow" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    seq = 1 + sum(1 for d in runs.iterdir() if d.name.startswith(f"wf_{today}_"))
    wf_id = f"wf_{today}_{seq:03d}"

    deadline = (datetime.datetime.now(datetime.timezone.utc) +
                datetime.timedelta(hours=prof["limits"]["wall_clock_hours"]))

    st = {
        "schema_version": 1,
        "workflow": {
            "id": wf_id, "title": title, "request_ref": "request.md",
            "created_at": utcnow(), "created_by": created_by,
            "parent_workflow_id": None,
        },
        "policy": {"profile_name": pname, "profile_snapshot_sha256": sha,
                   "snapshot": prof},
        "state": "RECEIVED",
        "state_entered_at": utcnow(),
        "route": "full",  # safe default until classification says otherwise
        "flags": {"security": False, "data_model": False,
                  "budget_warning": False, "integrity_hold": False},
        "counters": {"plan_revisions": 0, "impl_revisions": 0, "review_cycles": 0,
                     "plan_reviews": 0, "security_reviews": 0, "code_reviews": 0,
                     "agent_invocations": 0, "consecutive_failures": 0},
        "limits": {k: prof["limits"][k] for k in policy.REQUIRED_LIMITS},
        "budget": {
            "token_budget": prof["budget"]["token_budget"],
            "tokens_consumed_estimate": 0,
            "usage_by_role": {},
            "cost_usd_estimate": 0.0,
            "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "current": {"classification": 0, "repo_analysis": 0, "plan": 0,
                    "plan_review": 0, "security_review_plan": 0,
                    "implementation": 0, "security_review_code": 0,
                    "code_review": 0, "approval": 0},
        "plan_approved": False,
        "approvals": [],
        "escalations": [],
        "history": [{"ts": utcnow(), "from": "-", "to": "RECEIVED",
                     "trigger": "init", "actor": "orchestrator"}],
        "open_invocation": None,
        "last_invocation": None,
    }

    wdir = workflow_dir(root, wf_id)
    (wdir / "artifacts" / "approvals").mkdir(parents=True, exist_ok=True)
    (wdir / "request.md").write_text(request_text, encoding="utf-8")
    save_state(root, wf_id, st)

    idx_p = root / ".workflow" / "index.json"
    idx = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p.exists() else {}
    idx[wf_id] = {"title": title, "state": "RECEIVED",
                  "created_at": st["workflow"]["created_at"], "closed_at": None}
    atomic_write_json(idx_p, idx)

    (root / ".workflow" / "ACTIVE").write_text(wf_id + "\n", encoding="utf-8")
    ledger.log(root, wf_id, "workflow_created", title=title, profile=pname,
               profile_sha256=sha, created_by=created_by)
    return wf_id
