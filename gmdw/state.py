"""GMDW state engine — workflow lifecycle, transition table, guards, versioning.

state.json is mutated ONLY through this module (via the deterministic
orchestrator, the `wf` CLI, and the hooks). All writes are atomic (tmp file +
os.replace). Guards read artifacts from disk, never conversation memory, so
every decision is reconstructable.

v0.3.0 changes the shape of the machine, not its nature:

* **Three agent stages, not six.** ``ANALYSIS_PLANNING`` (Analyst-Planner),
  ``IMPLEMENTING`` (Implementor), ``REVIEW`` (Reviewer).
* **Two mandatory human gates.** ``PLAN_APPROVAL`` and ``FINAL_APPROVAL`` are
  the *only* edges into implementation and approval respectively. An LLM verdict
  can withhold approval; it can no longer grant it.
* **Markdown artifacts.** Paths end in ``.md`` and are parsed by
  :mod:`gmdw.parsers` into the same normalized dicts the guards always used.
"""
from __future__ import annotations

import datetime
import json
import os
import stat
from pathlib import Path

from . import ledger, parsers, policy


class GmdwError(Exception):
    """Raised when a governance rule rejects an operation."""


# --------------------------------------------------------------------------- #
# State machine definition
# --------------------------------------------------------------------------- #

STATES = [
    "RECEIVED", "ANALYSIS_PLANNING", "PLAN_APPROVAL", "IMPLEMENTING",
    "REVIEW", "FINAL_APPROVAL", "APPROVED",
    "ESCALATED", "COMPLETED", "FAILED", "REJECTED",
]

TERMINAL = {"COMPLETED", "FAILED", "REJECTED"}

# The two human gates are structural: there is deliberately NO edge from
# ANALYSIS_PLANNING to IMPLEMENTING, and none from REVIEW to APPROVED. Skipping
# a human is not a policy setting that could be misconfigured — it is an edge
# that does not exist.
TRANSITIONS = {
    "RECEIVED":          {"ANALYSIS_PLANNING", "REJECTED"},
    "ANALYSIS_PLANNING": {"PLAN_APPROVAL", "ESCALATED", "FAILED", "REJECTED"},
    "PLAN_APPROVAL":     {"IMPLEMENTING", "ANALYSIS_PLANNING", "ESCALATED",
                          "REJECTED"},
    "IMPLEMENTING":      {"REVIEW", "ESCALATED", "FAILED"},
    "REVIEW":            {"FINAL_APPROVAL", "IMPLEMENTING", "ESCALATED"},
    "FINAL_APPROVAL":    {"APPROVED", "IMPLEMENTING", "ESCALATED", "REJECTED"},
    "APPROVED":          {"COMPLETED", "FAILED", "ESCALATED"},
    "ESCALATED":         {"ANALYSIS_PLANNING", "IMPLEMENTING", "APPROVED",
                          "REJECTED"},
    "COMPLETED": set(), "FAILED": set(), "REJECTED": set(),
}

AGENT_FOR_STATE = {
    "ANALYSIS_PLANNING": "analyst-planner",
    "IMPLEMENTING":      "implementor",
    "REVIEW":            "reviewer",
}

# The human gate each approval state is waiting on.
GATE_FOR_STATE = {"PLAN_APPROVAL": "plan", "FINAL_APPROVAL": "final"}

# Artifact types a state's agent may produce. The first entry is the *primary*:
# the artifact whose acceptance gates the transition out of the state.
ARTIFACTS_FOR_STATE = {
    "ANALYSIS_PLANNING": ("plan", "classification", "repo_analysis"),
    "IMPLEMENTING":      ("implementation",),
    "REVIEW":            ("review",),
}

# On the FIRST pass through ANALYSIS_PLANNING the Analyst-Planner must ground the
# plan in a fresh analysis and classification. On a revision it re-plans only:
# re-deriving an unchanged analysis is the exact re-ingestion cost the 3-agent
# consolidation exists to remove.
FIRST_PASS_REQUIRED = {"ANALYSIS_PLANNING": ("plan", "classification",
                                             "repo_analysis")}

# state -> the single artifact type whose version the state is producing, for
# the callers that only care about the primary (hooks, `wf expected`).
ARTIFACT_FOR_STATE = {s: types[0] for s, types in ARTIFACTS_FOR_STATE.items()}

# artifact type -> the value its envelope's `artifact_type` must carry
ENVELOPE_TYPE = {atype: spec[0] for atype, spec in parsers.ARTIFACT_SPEC.items()}

REVIEW_TYPES = {"review"}

ARTIFACT_TYPES = ("classification", "repo_analysis", "plan", "implementation",
                  "review")

# resolution recorded on the pending escalation -> the ONLY state it unlocks
ESCALATION_TARGET = {
    "redirect": "ANALYSIS_PLANNING",
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
        return f"artifacts/implementation_v{version}/manifest.md"
    return f"artifacts/{atype}_v{version}.md"


def repo_relpath(wf_id: str, atype: str, version: int) -> str:
    return f".workflow/runs/{wf_id}/{artifact_relpath(atype, version)}"


def read_artifact_text(root, st: dict, atype: str, version: int | None = None):
    version = version if version is not None else st["current"].get(atype, 0)
    if version < 1:
        return None
    p = workflow_dir(root, st["workflow"]["id"]) / artifact_relpath(atype, version)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def load_artifact_doc(root, st: dict, atype: str, version: int | None = None):
    """(Document, parsed dict) for an artifact on disk, or (None, None)."""
    text = read_artifact_text(root, st, atype, version)
    if text is None:
        return None, None
    try:
        return parsers.parse_artifact(text, atype)
    except KeyError:
        return None, None


def load_artifact(root, st: dict, atype: str, version: int | None = None):
    """The parsed (normalized) artifact, or None. Guards use this; it is the
    v0.2 ``load_artifact`` contract with a Markdown source underneath."""
    _doc, parsed = load_artifact_doc(root, st, atype, version)
    return parsed


def artifact_envelope(root, st: dict, atype: str, version: int | None = None):
    doc, _ = load_artifact_doc(root, st, atype, version)
    return doc.envelope if doc else {}


def expected(root, st: dict):
    """What the current state is waiting for.

    Returns the producing agent, its policy-assigned model, and every artifact
    the stage may write — because the Analyst-Planner writes three. ``primary``
    is the one whose acceptance gates the transition; ``writable_paths`` is what
    the PreToolUse write gate allows.
    """
    s = st["state"]
    types = ARTIFACTS_FOR_STATE.get(s)
    if not types:
        return None
    first_pass = st["current"].get(types[0], 0) == 0
    required = set(FIRST_PASS_REQUIRED.get(s, types)) if first_pass else {types[0]}

    wf_id = st["workflow"]["id"]
    artifacts = []
    for atype in types:
        version = st["current"].get(atype, 0) + 1
        artifacts.append({
            "artifact_type": atype,
            "envelope_type": ENVELOPE_TYPE.get(atype, atype),
            "version": version,
            "path": repo_relpath(wf_id, atype, version),
            "run_rel": artifact_relpath(atype, version),
            "required": atype in required,
        })

    agent = AGENT_FOR_STATE.get(s)
    prof = st["policy"]["snapshot"]
    primary = artifacts[0]
    return {
        "state": s,
        "agent": agent,
        "model": policy.model_for_role(prof, agent) if agent else None,
        "effort": policy.effort_for_role(prof, agent) if agent else None,
        "artifacts": artifacts,
        "primary": primary,
        # v0.2 compatibility: the fields callers read for the primary artifact.
        "artifact_type": primary["artifact_type"],
        "envelope_type": primary["envelope_type"],
        "version": primary["version"],
        "path": primary["path"],
        "writable_paths": [a["path"] for a in artifacts],
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

def _accept():
    """The acceptance module, imported at call time.

    ``gmdw.accept`` imports this module, so a top-level import here would be
    circular. The guards below defer to it because
    ``accept.human_approval_satisfied`` is the single home of the human-approval
    decision — it verifies the frozen approval artifact on disk, not just the
    mirror in state.json, and having two copies of that check would be the one
    place a divergence really matters.
    """
    from . import accept
    return accept


def security_required(st: dict) -> bool:
    """Does this workflow need the Reviewer's security checklist? The dedicated
    security-reviewer agent is gone; the trigger it fired on now makes the
    unified Reviewer's `## Security Checklist` section mandatory."""
    prof = st["policy"]["snapshot"]
    if st["flags"].get("security") or st["flags"].get("data_model"):
        return True
    return bool(prof["security"].get("always_review_plans"))


def approval_subject(st: dict, gate: str) -> str | None:
    """The artifact a human gate signs off on.

    A gate is satisfied only by an approval that names the CURRENT version of
    its subject. Without that, a plan approved in an earlier cycle would keep
    vouching for a plan rewritten after an escalation — the approval has to be
    about something, not just about the workflow.
    """
    cur = st["current"]
    if gate == "plan" and cur.get("plan", 0) >= 1:
        return artifact_relpath("plan", cur["plan"])
    if gate == "final" and cur.get("review", 0) >= 1:
        return artifact_relpath("review", cur["review"])
    return None  # deployment signs off on the workflow as a whole


def has_approval(st: dict, gate: str,
                 decisions=("approved", "force_approved")) -> bool:
    return any(a["gate"] == gate and a["decision"] in decisions
               for a in st.get("approvals", []))


def latest_approval(st: dict, gate: str):
    for a in reversed(st.get("approvals", [])):
        if a["gate"] == gate:
            return a
    return None


def _subject_matches(envelope: dict, atype: str, version: int) -> bool:
    """Does a review's `subject_ref` point at the current implementation?"""
    subject = str(envelope.get("subject_ref", ""))
    if atype == "implementation":
        return f"implementation_v{version}/" in subject
    return subject.endswith(f"{atype}_v{version}.md")


def guard(root, st: dict, to: str):
    """Return (ok, reason). Assumes `to` is already in the transition table."""
    s = st["state"]
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

    if s == "ANALYSIS_PLANNING":
        if cur.get("plan", 0) < 1:
            return False, "plan artifact required"
        if cur.get("classification", 0) < 1:
            return False, "classification artifact required (it sets the route)"
        return True, ""

    if s == "PLAN_APPROVAL":
        if to == "IMPLEMENTING":
            ok, why = _accept().human_approval_satisfied(
                root, st, "plan", subject=approval_subject(st, "plan"))
            if not ok:
                return False, why
            return True, ""
        if to == "ANALYSIS_PLANNING":
            if not has_approval(st, "plan", decisions=("changes_requested",)):
                return False, "returning to planning requires decision=changes_requested"
            if c["plan_revisions"] >= lim["max_plan_revisions"]:
                return False, "plan revision limit reached; escalate"
            return True, ""

    if s == "IMPLEMENTING":
        if cur.get("implementation", 0) < 1:
            return False, "implementation artifact required"
        return True, ""

    if s == "REVIEW":
        rev = load_artifact(root, st, "review")
        if rev is None:
            return False, "review artifact required"
        env = artifact_envelope(root, st, "review")
        if not _subject_matches(env, "implementation", cur.get("implementation", 0)):
            return False, ("the latest review does not name the current "
                           "implementation in its subject_ref")
        if to == "IMPLEMENTING":
            if rev.get("verdict") != "revise":
                return False, (f"rework requires the review verdict to be "
                               f"'revise' (got {rev.get('verdict')!r}); a "
                               f"human may still send it back from FINAL_APPROVAL")
            if c["impl_revisions"] >= lim["max_impl_revisions"]:
                return False, "implementation revision limit reached; escalate"
            return True, ""
        return True, ""  # -> FINAL_APPROVAL: any verdict goes to the human

    if s == "FINAL_APPROVAL":
        approval = latest_approval(st, "final")
        if to == "IMPLEMENTING":
            if not approval or approval["decision"] != "changes_requested":
                return False, "rework requires a final decision=changes_requested"
            if c["impl_revisions"] >= lim["max_impl_revisions"]:
                return False, "implementation revision limit reached; escalate"
            return True, ""
        if to == "APPROVED":
            # THE gate the whole rework exists for: an LLM verdict is advisory,
            # a human record is authoritative.
            ok, why = _accept().human_approval_satisfied(
                root, st, "final", subject=approval_subject(st, "final"))
            if not ok:
                return False, ("human sign-off on the review and the diff is "
                               "required before APPROVED: " + why)
            forced = approval and approval["decision"] == "force_approved"
            rev = load_artifact(root, st, "review")
            if rev is None:
                return False, "review artifact required"
            if not forced:
                acs = rev.get("acceptance_criteria", [])
                if not acs:
                    return False, ("the review must evaluate the plan's "
                                   "acceptance criteria")
                bad = [a["id"] for a in acs if a.get("status") != "pass"]
                if bad:
                    return False, (f"acceptance criteria not passing: {bad}. "
                                   f"Fix them, or record an explicit override "
                                   f"(wf approve final --decision force_approved "
                                   f"--overridden-findings ...)")
                if security_required(st) and not rev.get("checked"):
                    return False, ("this workflow is security-flagged; the review "
                                   "must record a non-empty security checklist")
            return True, ""

    if s == "APPROVED":
        if to == "COMPLETED":
            ok, why = _accept().human_approval_satisfied(root, st, "deployment")
            if not ok:
                return False, "deployment approval required: " + why
            if st["policy"]["snapshot"]["verification"].get(
                    "tamper_check_on_finalize", True):
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

def sync_routing(root, st: dict) -> None:
    """Copy route + flags from the accepted classification into state. The
    artifact is the source of truth; state only caches it for the gates."""
    cls = load_artifact(root, st, "classification")
    if cls and cls.get("route"):
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

    if frm == "ANALYSIS_PLANNING":
        sync_routing(root, st)

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
                    profile_name: str | None = None,
                    auto_approve: bool = False) -> str:
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
        "schema_version": 2,
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
                     "reviews": 0, "agent_invocations": 0,
                     "consecutive_failures": 0},
        "limits": {k: prof["limits"][k] for k in policy.REQUIRED_LIMITS},
        "budget": {
            "token_budget": prof["budget"]["token_budget"],
            "tokens_consumed_estimate": 0,
            "usage_by_role": {},
            "cost_usd_estimate": 0.0,
            "cost_ceiling_usd": prof["budget"]["cost_ceiling_usd"],
            "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "current": {t: 0 for t in ARTIFACT_TYPES},
        # Human-in-the-loop record. Authoritative approval lives in the frozen
        # approval artifacts under approvals[]; this mirrors it for readers.
        "human_approved": {"plan": False, "final": False},
        "auto_approve": bool(auto_approve),
        "plan_approved": False,   # v0.2 field name, kept for audit readers
        "approvals": [],
        "escalations": [],
        "history": [{"ts": utcnow(), "from": "-", "to": "RECEIVED",
                     "trigger": "init", "actor": "orchestrator"}],
        "open_invocation": None,
        "last_invocation": None,
    }
    st["current"]["approval"] = 0

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
               profile_sha256=sha, created_by=created_by,
               auto_approve=bool(auto_approve))
    return wf_id


# --------------------------------------------------------------------------- #
# Finalization
# --------------------------------------------------------------------------- #

def close_workflow(root, wf_id: str, st: dict) -> dict:
    """Finalize a terminal workflow: re-verify every frozen artifact, write the
    index entry, clear ACTIVE. Shared by ``wf close`` and the orchestrator so a
    run finalized either way leaves the identical record."""
    if st["state"] not in TERMINAL:
        raise GmdwError(f"cannot close: state {st['state']} is not terminal "
                        f"({sorted(TERMINAL)})")
    mismatches = ledger.verify(root, wf_id)
    if mismatches:
        ledger.log(root, wf_id, "integrity_violation", detail=mismatches,
                   phase="close")
        raise GmdwError(f"tamper check failed; refusing to close: {mismatches}")

    idx_p = Path(root) / ".workflow" / "index.json"
    idx = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p.exists() else {}
    idx[wf_id] = {
        "title": st["workflow"]["title"],
        "state": st["state"],
        "created_at": st["workflow"]["created_at"],
        "closed_at": utcnow(),
        "cost_usd_estimate": st["budget"]["cost_usd_estimate"],
        "tokens": st["budget"]["tokens_consumed_estimate"],
    }
    atomic_write_json(idx_p, idx)

    active = Path(root) / ".workflow" / "ACTIVE"
    if active.exists() and active.read_text(encoding="utf-8").strip() == wf_id:
        active.unlink()
    ledger.log(root, wf_id, "workflow_closed", final_state=st["state"])
    return {"closed": wf_id, "final_state": st["state"]}
