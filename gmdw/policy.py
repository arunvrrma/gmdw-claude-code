"""GMDW policy — profile loading, validation, limit/model/gate policy access.

Profiles live in .workflow/policies/<name>.json and are COPIED (pinned) into
state.json at `wf init`. Enforcement always reads the pinned snapshot, never the
live profile file, so editing a profile can never change a running workflow.

Profile floors (validate_profile) are non-negotiable. A profile that tries to
disable the deployment gate, loosen security-review triggers beyond the standard
baseline, or make the final human gate skippable on a non-trivial route fails
validation and cannot start a workflow.
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

# The 3-agent model (v0.3.0). repo-analyst + planner + plan-reviewer collapsed
# into analyst-planner; security-reviewer + code-reviewer into reviewer.
AGENT_ROLES = ["analyst-planner", "implementor", "reviewer"]

ROUTES = ["trivial", "medium", "full"]

GATE_MODES = ("blocking", "notify", "off")

MODELS = ("opus", "sonnet", "haiku")

# Security-review triggers may be tightened (lower score threshold => reviews
# fire more often) but never loosened past the standard baseline of 2.
SECURITY_TRIGGER_FLOOR = 2

# Routes on which a human gate may be waived by an explicit `--auto-approve`.
# Anything richer than `trivial` always faces a human, whatever the profile says.
AUTO_APPROVE_ELIGIBLE_ROUTES = ("trivial",)


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
    ceiling = profile["budget"].get("cost_ceiling_usd")
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) \
            or ceiling <= 0:
        errors.append("budget.cost_ceiling_usd must be a positive number")

    # --- gates: floors ---
    gates = profile["gates"]
    if gates.get("deployment_approval") != "blocking":
        errors.append("gates.deployment_approval must be 'blocking' (floor; not overridable)")
    for gate in ("plan_approval", "final_approval"):
        g = gates.get(gate)
        if not isinstance(g, dict) or not all(r in g for r in ROUTES):
            errors.append(f"gates.{gate} must map every route to "
                          f"{'|'.join(GATE_MODES)}")
            continue
        if any(g[r] not in GATE_MODES for r in ROUTES):
            errors.append(f"gates.{gate} values must be {'|'.join(GATE_MODES)}")
            continue
        # The human-in-the-loop floor: only the trivial route may waive a gate,
        # and only then with an explicit --auto-approve at launch.
        off_routes = [r for r in ROUTES
                      if g[r] == "off" and r not in AUTO_APPROVE_ELIGIBLE_ROUTES]
        if off_routes:
            errors.append(f"gates.{gate} may only be 'off' on "
                          f"{list(AUTO_APPROVE_ELIGIBLE_ROUTES)}; got 'off' on "
                          f"{off_routes} (profiles may tighten human gates, "
                          f"never remove them)")

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
    for role in AGENT_ROLES:
        m = models.get(role)
        if not isinstance(m, dict) or m.get("model") not in MODELS:
            errors.append(f"models.{role} must define model: {'|'.join(MODELS)}")

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


def gate_mode(profile: dict, gate: str, route: str) -> str:
    """Mode of a human gate on a route: blocking|notify|off.

    ``blocking`` and ``notify`` both stop for a human — they differ only in how
    much context the orchestrator prints. ``off`` marks the gate as waivable by
    an explicit ``--auto-approve``; it never waives itself.
    """
    g = (profile.get("gates") or {}).get(gate)
    if isinstance(g, dict):
        return g.get(route, "blocking")
    return str(g or "blocking")


def auto_approve_allowed(profile: dict, route: str, gate: str) -> tuple:
    """(allowed, reason) — may ``--auto-approve`` waive ``gate`` on ``route``?

    Two independent conditions, both required: the route must be one the system
    considers low-risk at all, and the pinned profile must mark the gate 'off'
    for it. A profile can therefore refuse auto-approval outright (strict does),
    and no profile can enable it for medium/full work.
    """
    if route not in AUTO_APPROVE_ELIGIBLE_ROUTES:
        return False, (f"--auto-approve applies only to "
                       f"{list(AUTO_APPROVE_ELIGIBLE_ROUTES)} routes; this "
                       f"workflow classified as '{route}'")
    mode = gate_mode(profile, f"{gate}_approval", route)
    if mode != "off":
        return False, (f"policy profile '{profile.get('name')}' sets the {gate} "
                       f"gate to '{mode}' on the {route} route; it cannot be "
                       f"auto-approved")
    return True, ""


def cost_ceiling(profile: dict) -> float:
    return float((profile.get("budget") or {}).get("cost_ceiling_usd", 0) or 0)
