#!/usr/bin/env python3
"""GMDW PreToolUse gate — THE deterministic governance boundary.

Intercepts Agent/Task (subagent invocations), Write/Edit/MultiEdit (file
writes), and Bash (shell commands). Validates every action against
state.json, the pinned policy profile, and the artifact registry.

FAIL-CLOSED: any internal error while a workflow is ACTIVE denies the action.

Deny protocol: emit the PreToolUse hookSpecificOutput JSON with
permissionDecision=deny; the reason is surfaced to the model so its only
legal next moves are explicit (usually: escalate).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmdw import heartbeat, ledger, policy  # noqa: E402
from gmdw import state as S      # noqa: E402
from gmdw.state import GmdwError  # noqa: E402


def deny(reason: str):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"[GMDW] {reason}",
    }}))
    sys.exit(0)


def allow():
    sys.exit(0)


# --------------------------------------------------------------------------- #
# Protected paths (never writable by any tool while a workflow is active)
# --------------------------------------------------------------------------- #

_PROTECTED_PREFIXES = (
    ".workflow/bin/", ".workflow/schemas/", ".workflow/policies/",
    ".claude/hooks/", ".claude/agents/", ".claude/commands/",
)
_PROTECTED_EXACT = (
    ".claude/settings.json", ".workflow/config.json",
    ".workflow/index.json", ".workflow/ACTIVE",
    ".workflow/.hooks_heartbeat",  # governance-liveness beacon; only hooks write it
)
_PROTECTED_BASENAMES = ("state.json", "decision_log.jsonl", "artifacts_index.json")


def _is_protected(rel: str) -> bool:
    if rel in _PROTECTED_EXACT:
        return True
    if any(rel.startswith(p) for p in _PROTECTED_PREFIXES):
        return True
    if rel.startswith(".workflow/runs/") and \
            os.path.basename(rel) in _PROTECTED_BASENAMES:
        return True
    return False


def _relpath(path: str, root: Path, cwd: str) -> str | None:
    """Repo-relative posix path, or None if outside the repo."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Agent invocation gate
# --------------------------------------------------------------------------- #

def _parse_header(prompt: str) -> dict | None:
    if "GMDW-INVOCATION" not in prompt:
        return None
    hdr, seen = {}, False
    for line in prompt.splitlines():
        line = line.strip()
        if line == "GMDW-INVOCATION":
            seen = True
            continue
        if not seen:
            continue
        m = re.match(r"^(workflow_id|agent|state|output_artifact):\s*(\S+)$", line)
        if m:
            hdr[m.group(1)] = m.group(2)
            continue
        if line.startswith("inputs:") or line == "":
            break
    return hdr if {"workflow_id", "agent", "output_artifact"} <= set(hdr) else None


def gate_agent(root: Path, wid: str | None, ti: dict):
    if not wid:
        cfg_p = root / ".workflow" / "config.json"
        cfg = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.exists() else {}
        if cfg.get("allow_non_gmdw_subagents"):
            allow()
        deny("no active GMDW workflow; subagent invocation is prohibited by "
             "policy (start one with /gmdw or set allow_non_gmdw_subagents)")

    _, st = S.load_state(root, wid)
    prof = st["policy"]["snapshot"]

    hdr = _parse_header(ti.get("prompt", "") or "")
    if not hdr:
        deny("invocation prompt lacks a valid GMDW-INVOCATION header "
             "(workflow_id, agent, state, output_artifact)")
    if hdr["workflow_id"] != wid:
        deny(f"header workflow_id {hdr['workflow_id']} != ACTIVE workflow {wid}")

    sub = ti.get("subagent_type", "") or ""
    if hdr["agent"] != sub:
        deny(f"header agent '{hdr['agent']}' != subagent_type '{sub}'")

    exp = S.expected(root, st)
    if not exp or not exp.get("agent"):
        deny(f"state {st['state']} does not permit any agent invocation")
    if sub != exp["agent"]:
        deny(f"state {st['state']} requires agent '{exp['agent']}', not '{sub}'")
    if hdr["output_artifact"] != exp["path"]:
        deny(f"output_artifact must be {exp['path']} (next {exp['artifact_type']} "
             f"version is v{exp['version']})")

    c, lim, b = st["counters"], st["limits"], st["budget"]
    if c["agent_invocations"] >= lim["max_agent_invocations"]:
        deny(f"invocation limit reached ({lim['max_agent_invocations']}); "
             f"the only legal move is escalation (wf transition ESCALATED)")
    if c["consecutive_failures"] >= 2:
        deny("two consecutive agent failures; escalate to a human")
    if st["state"] == "PLANNING" and exp["version"] > 1 and \
            c["plan_revisions"] >= lim["max_plan_revisions"]:
        deny("plan revision limit reached; escalate")
    if st["state"] == "IMPLEMENTING" and exp["version"] > 1 and \
            c["impl_revisions"] >= lim["max_impl_revisions"]:
        deny("implementation revision limit reached; escalate")
    if exp["artifact_type"] in S.REVIEW_TYPES and \
            c["review_cycles"] >= lim["max_review_cycles"]:
        deny(f"review cycle limit reached ({lim['max_review_cycles']}); escalate")

    if b["tokens_consumed_estimate"] >= b["token_budget"]:
        deny(f"token budget exhausted ({b['tokens_consumed_estimate']}/"
             f"{b['token_budget']}); escalate")
    deadline = datetime.datetime.strptime(
        b["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    if datetime.datetime.now(datetime.timezone.utc) >= deadline:
        deny(f"wall-clock deadline {b['deadline']} passed; escalate")

    soft = b["token_budget"] * prof["budget"]["soft_threshold_pct"] // 100
    if b["tokens_consumed_estimate"] >= soft and not st["flags"]["budget_warning"]:
        st["flags"]["budget_warning"] = True
        ledger.log(root, wid, "budget_warning",
                   consumed=b["tokens_consumed_estimate"], budget=b["token_budget"])

    want = policy.model_for_role(prof, sub)
    got = ti.get("model")
    if got and want and got != want:
        deny(f"policy assigns model '{want}' to {sub}; requested '{got}'")

    if st.get("open_invocation"):
        deny(f"another invocation is already open "
             f"({st['open_invocation'].get('agent')}); single-flight only")

    st["open_invocation"] = {
        "agent": sub, "started_at": S.utcnow(),
        "expected_artifact": exp["path"], "expected_type": exp["artifact_type"],
        "version": exp["version"], "retries": 0,
    }
    st["counters"]["agent_invocations"] += 1
    S.save_state(root, wid, st)
    ledger.log(root, wid, "invocation_requested", agent=sub,
               expected=exp["path"],
               invocations=st["counters"]["agent_invocations"])
    allow()


# --------------------------------------------------------------------------- #
# Write gate
# --------------------------------------------------------------------------- #

def gate_write(root: Path, wid: str | None, ti: dict, cwd: str):
    path = ti.get("file_path") or ti.get("path") or ""
    if not path:
        allow()
    rel = _relpath(path, root, cwd)
    if rel is None:
        allow()  # outside the repo (scratch space) — not governed
    if not wid:
        allow()  # no active workflow: normal Claude Code behavior

    if _is_protected(rel):
        deny(f"'{rel}' is governance-protected and never writable while a "
             f"workflow is active")

    _, st = S.load_state(root, wid)
    run_prefix = f".workflow/runs/{wid}/"

    if rel.startswith(".workflow/runs/") and not rel.startswith(run_prefix):
        deny("writes to other workflows' run directories are prohibited")

    if rel.startswith(run_prefix + "artifacts/"):
        run_rel = rel[len(run_prefix):]
        if run_rel.startswith("artifacts/approvals/"):
            deny("approval artifacts are written only by 'wf approve'")
        if ledger.is_frozen(root, wid, run_rel):
            deny(f"'{run_rel}' is an accepted artifact version and is immutable; "
                 f"produce the next version instead")
        exp = S.expected(root, st)
        if not exp:
            deny(f"state {st['state']} does not accept artifact writes")
        if exp["artifact_type"] == "implementation":
            allowed_dir = os.path.dirname(exp["path"]) + "/"
            if rel == exp["path"] or rel.startswith(allowed_dir):
                allow()
            deny(f"in {st['state']}, artifact writes are limited to {allowed_dir}")
        if rel != exp["path"]:
            deny(f"in {st['state']}, the only writable artifact is {exp['path']}")
        allow()

    if rel.startswith(run_prefix):
        deny(f"'{rel}' is not a writable location inside the run directory")

    # ---- source-tree writes ----
    s = st["state"]
    if s != "IMPLEMENTING":
        deny(f"source writes are not permitted in state {s} "
             f"(only the Implementor, in IMPLEMENTING, edits the source tree)")

    if st["route"] == "trivial":
        allow()  # trivial route: no plan; protected paths still apply

    plan = S.load_artifact(root, st, "plan")
    if not plan:
        deny("no plan on record; cannot scope writes")
    planned = {f["path"] for f in plan.get("files", [])
               if f.get("action") in ("create", "modify")}
    if rel in planned or rel.startswith(("tests/", "test/", "docs/")):
        allow()
    deny(f"write outside the approved plan's scope: '{rel}'. Declare a "
         f"deviation in the manifest and escalate, or stay within: "
         f"{sorted(planned)} + tests/ + docs/")


# --------------------------------------------------------------------------- #
# Bash gate
# --------------------------------------------------------------------------- #

_MUTATION_TOKENS = ("rm ", "rm\t", "mv ", "cp ", "sed -i", "chmod",
                    "chattr", "truncate", "dd ", "unlink", "shred", "> ", ">>")
_PROTECTED_SUBSTRINGS = ("state.json", "decision_log.jsonl", "artifacts_index.json",
                         ".workflow/bin", ".workflow/schemas", ".workflow/policies",
                         ".workflow/ACTIVE", ".workflow/config.json",
                         ".workflow/index.json", ".workflow/.hooks_heartbeat",
                         ".claude/hooks", ".claude/agents",
                         ".claude/settings", ".claude/commands")


def gate_bash(root: Path, wid: str | None, ti: dict):
    cmd = ti.get("command", "") or ""
    if not wid:
        allow()

    if any(s in cmd for s in _PROTECTED_SUBSTRINGS) and \
            any(t in cmd for t in _MUTATION_TOKENS):
        deny("command touches governance-protected files with a mutating "
             "operation; state/policy/hooks are mutated only via the wf CLI")

    _, st = S.load_state(root, wid)
    s = st["state"]

    if re.search(r"\bgit\s+push\b|\bgh\s+pr\s+(merge|create)\b", cmd):
        has_deploy = any(a["gate"] == "deployment" and
                         a["decision"] in ("approved", "force_approved")
                         for a in st["approvals"])
        if s != "APPROVED" or not has_deploy:
            deny("push/PR actions require state APPROVED and a recorded "
                 "deployment approval (wf approve deployment)")

    if re.search(r"\bgit\s+commit\b", cmd) and s not in ("IMPLEMENTING", "APPROVED"):
        deny(f"git commit is only permitted in IMPLEMENTING or APPROVED (state: {s})")

    if re.search(r"\b(pip3?|npm|pnpm|yarn|apt(-get)?|brew|cargo|gem)\s+(install|add)\b",
                 cmd) and s != "IMPLEMENTING":
        deny(f"package installation is only permitted in IMPLEMENTING (state: {s})")

    allow()


# --------------------------------------------------------------------------- #

def main():
    data = json.load(sys.stdin)
    cwd = data.get("cwd") or os.getcwd()
    try:
        root = S.find_root(cwd)
    except GmdwError:
        allow()  # not a GMDW repo — nothing to govern
    heartbeat.touch(root, "PreToolUse")  # attest the enforcement plane is live
    wid = S.active_id(root)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}

    if tool in ("Agent", "Task"):
        gate_agent(root, wid, ti)
    elif tool in ("Write", "Edit", "MultiEdit"):
        gate_write(root, wid, ti, cwd)
    elif tool == "Bash":
        gate_bash(root, wid, ti)
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL CLOSED while a workflow is active
        try:
            r = S.find_root(os.getcwd())
            active = S.active_id(r)
        except Exception:
            active = None
        if active:
            deny(f"gate internal error (failing closed): {type(e).__name__}: {e}")
        sys.exit(0)
