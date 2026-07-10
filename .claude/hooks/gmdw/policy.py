"""GMDW policy — profile loading, validation, limit/model policy access.

Profiles live in .workflow/policies/<name>.json and are COPIED (pinned) into
state.json at `wf init`. Enforcement always reads the pinned snapshot, never
the live profile file, so editing a profile can never change a running workflow.

Profile floors (validate_profile) are non-negotiable: a profile that tries to
disable the deployment gate or loosen security-review triggers beyond the
standard baseline fails validation and cannot start a workflow.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REQUIRED_SECTIONS = ["routing", "limits", "budget", "gates", "security",
                     "verification", "models", "finalization"]

REQUIRED_LIMITS = ["max_plan_revisions", "max_impl_revisions", "max_review_cycles",
                   "max_agent_invocations", "per_invocation_timeout_min",
                   "wall_clock_hours"]

AGENT_ROLES = ["repo-analyst", "planner", "plan-reviewer", "implementor",
               "security-reviewer", "code-reviewer"]

ROUTES = ["trivial", "medium", "full"]

# Security-review triggers may be tightened (lower score threshold => reviews
# fire more often) but never loosened past the standard baseline of 2.
SECURITY_TRIGGER_FLOOR = 2


def load_profile(root, name: str):
    """Return (profile_dict, sha256_of_file)."""
    p = Path(root) / ".workflow" / "policies" / f"{name}.json"
    raw = p.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def validate_profile(profile: dict) -> list:
    errors = []
    for s in REQUIRED_SECTIONS:
        if s not in profile:
            errors.append(f"missing section: {s}")
    if errors:
        return errors

    # --- limits ---
    for k in REQUIRED_LIMITS:
        v = profile["limits"].get(k)
        if not isinstance(v, int) or v < 1:
            errors.append(f"limits.{k} must be a positive integer")

    # --- budget ---
    for k in ("token_budget", "soft_threshold_pct"):
        if not isinstance(profile["budget"].get(k), int):
            errors.append(f"budget.{k} must be an integer")

    # --- gates: floors ---
    gates = profile["gates"]
    if gates.get("deployment_approval") != "blocking":
        errors.append("gates.deployment_approval must be 'blocking' (floor; not overridable)")
    pa = gates.get("plan_approval")
    if not isinstance(pa, dict) or not all(r in pa for r in ROUTES):
        errors.append("gates.plan_approval must map every route to blocking|notify|off")
    elif any(pa[r] not in ("blocking", "notify", "off") for r in ROUTES):
        errors.append("gates.plan_approval values must be blocking|notify|off")

    # --- security: floors ---
    sec = profile["security"]
    for k in ("plan_review_trigger", "code_review_trigger"):
        v = sec.get(k)
        if not isinstance(v, int):
            errors.append(f"security.{k} must be an integer")
        elif v > SECURITY_TRIGGER_FLOOR:
            errors.append(f"security.{k} may not exceed {SECURITY_TRIGGER_FLOOR} "
                          f"(profiles can tighten security, never loosen it)")

    # --- verification ---
    for k in ("rerun_tests", "path_existence_checks", "tamper_check_on_finalize"):
        if not isinstance(profile["verification"].get(k), bool):
            errors.append(f"verification.{k} must be a boolean")

    # --- models ---
    models = profile["models"]
    for role in AGENT_ROLES + ["orchestrator"]:
        m = models.get(role)
        if not isinstance(m, dict) or m.get("model") not in ("opus", "sonnet", "haiku"):
            errors.append(f"models.{role} must define model: opus|sonnet|haiku")

    # --- routing ---
    r = profile["routing"]
    if not isinstance(r.get("t1"), int) or not isinstance(r.get("t2"), int):
        errors.append("routing.t1 and routing.t2 must be integers")

    return errors


def model_for_role(profile: dict, role: str) -> str | None:
    m = (profile.get("models") or {}).get(role)
    return m.get("model") if isinstance(m, dict) else None


def effort_for_role(profile: dict, role: str) -> str | None:
    m = (profile.get("models") or {}).get(role)
    return m.get("effort") if isinstance(m, dict) else None


def gate_mode(profile: dict, route: str) -> str:
    """Plan-approval gate mode for a route: blocking|notify|off."""
    g = profile["gates"]["plan_approval"]
    return g.get(route, "off") if isinstance(g, dict) else str(g)
