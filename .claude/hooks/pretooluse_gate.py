#!/usr/bin/env python3
"""GMDW PreToolUse gate — THE deterministic governance boundary.

Intercepts Agent/Task (subagent invocations), Write/Edit/MultiEdit (file
writes), and Bash (shell commands). Validates every action against
state.json, the pinned policy profile, and the artifact registry.

Since v0.3.0 this hook runs *inside the agent's own Claude Code session*, which
the deterministic orchestrator started. The orchestrator decides which stage runs
and refuses to accept an ungoverned one; this gate decides what that stage may
actually do while it runs. The division is the point: routing is a loop, and
confinement is a boundary, and neither should be asked to do the other's job.

FAIL-CLOSED: any internal error while a workflow is ACTIVE denies the action.

Deny protocol: emit the PreToolUse hookSpecificOutput JSON with
permissionDecision=deny; the reason is surfaced to the model so its only
legal next moves are explicit (usually: write the artifact it was asked for,
or declare itself blocked).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gmdw import heartbeat, ledger  # noqa: E402
from gmdw import state as S      # noqa: E402
from gmdw.state import GmdwError  # noqa: E402

# Set by main() once the repo and workflow are known, so deny() can leave an
# audit record. A denial nobody can find later is only half a guardrail.
_CTX = {"root": None, "wid": None, "tool": None}


def deny(reason: str):
    root, wid = _CTX.get("root"), _CTX.get("wid")
    if root and wid:
        try:
            ledger.log(root, wid, "gate_denied", tool=_CTX.get("tool"),
                       reason=reason[:800])
        except OSError:
            pass
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
    "gmdw/",  # the orchestrator + runtime library itself
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


def _role() -> str | None:
    """The role the orchestrator started this session as, if any. Set in the
    child's environment by gmdw.runner; absent in an ordinary session."""
    return os.environ.get("GMDW_ROLE") or None


def _check_role(st: dict) -> None:
    """An agent session may only act in the stage it was started for.

    The orchestrator sets GMDW_ROLE when it launches a child. If that role does
    not match the state's agent, something is out of step — a stale session
    resumed against a workflow that has since moved on — and everything it does
    from here would be scoped against the wrong plan.
    """
    role = _role()
    if not role:
        return
    want = S.AGENT_FOR_STATE.get(st["state"])
    if want and role != want:
        deny(f"this session was started as '{role}' but the workflow is in "
             f"{st['state']}, which belongs to '{want}'. The session is stale; "
             f"stop and let the orchestrator start the right stage.")


# --------------------------------------------------------------------------- #
# Agent invocation gate
# --------------------------------------------------------------------------- #

def gate_agent(root: Path, wid: str | None, ti: dict):
    """Subagent invocation.

    Under the v0.2 architecture an LLM Orchestrator invoked subagents and this
    gate cross-checked a GMDW-INVOCATION header against the state machine.
    Since v0.3.0 the orchestrator is a Python process that starts agents
    directly, so *nothing inside a governed session has any business spawning a
    subagent*. There is no header to check any more, and no legitimate caller to
    check it for: the answer is simply no.
    """
    if not wid:
        cfg_p = root / ".workflow" / "config.json"
        cfg = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.exists() else {}
        if cfg.get("allow_non_gmdw_subagents"):
            allow()
        deny("no active GMDW workflow; subagent invocation is prohibited by "
             "policy (start one with /gmdw or set allow_non_gmdw_subagents)")

    sub = ti.get("subagent_type", "") or "?"
    deny(f"agents may not invoke subagents (requested '{sub}'). The deterministic "
         f"orchestrator is the only component that starts an agent, one at a "
         f"time, with the stage's scope. If you cannot finish inside this stage, "
         f"write your artifact with `status: blocked` and a precise "
         f"blocked_reason, and stop.")


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
    _check_role(st)
    run_prefix = f".workflow/runs/{wid}/"

    if rel.startswith(".workflow/runs/") and not rel.startswith(run_prefix):
        deny("writes to other workflows' run directories are prohibited")

    if rel.startswith(run_prefix + "artifacts/"):
        run_rel = rel[len(run_prefix):]
        if run_rel.startswith("artifacts/approvals/"):
            deny("approval artifacts record a human decision and are written "
                 "only by 'wf approve' / the orchestrator's gate")
        if ledger.is_frozen(root, wid, run_rel):
            deny(f"'{run_rel}' is an accepted artifact version and is immutable; "
                 f"produce the next version instead")
        exp = S.expected(root, st)
        if not exp:
            deny(f"state {st['state']} does not accept artifact writes")
        # The Analyst-Planner writes three artifacts, so the allowlist is a set.
        if rel in exp["writable_paths"]:
            allow()
        # An implementation is a directory (manifest.md, diff.patch, test_output).
        for spec in exp["artifacts"]:
            if spec["artifact_type"] == "implementation" and \
                    rel.startswith(os.path.dirname(spec["path"]) + "/"):
                allow()
        deny(f"in {st['state']}, the writable artifacts are "
             f"{exp['writable_paths']} — not '{rel}'")

    if rel.startswith(run_prefix):
        deny(f"'{rel}' is not a writable location inside the run directory")

    # ---- source-tree writes ----
    s = st["state"]
    if s != "IMPLEMENTING":
        deny(f"source writes are not permitted in state {s} "
             f"(only the Implementor, in IMPLEMENTING, edits the source tree)")

    plan = S.load_artifact(root, st, "plan")
    if not plan:
        deny("no accepted plan on record; cannot scope writes")
    planned = {f["path"] for f in plan.get("files", [])
               if f.get("action") in ("create", "modify")}
    if rel in planned or rel.startswith(("tests/", "test/", "docs/")):
        allow()
    deny(f"write outside the approved plan's scope: '{rel}'. The plan's "
         f"'## Files In Scope' is the allowlist. Declare a deviation in the "
         f"manifest and let the orchestrator escalate, or stay within: "
         f"{sorted(planned)} + tests/ + docs/")


# --------------------------------------------------------------------------- #
# Bash gate
# --------------------------------------------------------------------------- #

_MUTATION_TOKENS = ("rm ", "rm\t", "mv ", "cp ", "sed -i", "chmod",
                    "chattr", "truncate", "dd ", "unlink", "shred", "> ", ">>")
# The wf recovery authorities, matched like the git-push / git-commit rules
# below. \bwf\b also matches an invoked path ('.workflow/bin/wf accept'), while
# leaving every other wf subcommand ('wf status', 'wf expected') untouched.
_RECOVERY_CMD_RE = re.compile(r"\bwf\s+(accept|clear-invocation|approve)\b")

_PROTECTED_SUBSTRINGS = ("state.json", "decision_log.jsonl", "artifacts_index.json",
                         ".workflow/bin", ".workflow/schemas", ".workflow/policies",
                         ".workflow/ACTIVE", ".workflow/config.json",
                         ".workflow/index.json", ".workflow/.hooks_heartbeat",
                         ".claude/hooks", ".claude/agents",
                         ".claude/settings", ".claude/commands",
                         "gmdw/")


def gate_bash(root: Path, wid: str | None, ti: dict):
    cmd = ti.get("command", "") or ""
    if not wid:
        allow()

    # 'wf accept' / 'wf clear-invocation' are human recovery commands for a
    # session whose hooks are NOT firing, and 'wf approve' records a HUMAN
    # decision. This gate only executes when the hooks ARE firing, inside an
    # agent session, so none of the three can be legitimate here: an agent
    # running them would be self-accepting its own artifact, releasing the
    # single-flight lock on itself, or signing its own approval gate. Denying
    # loses no legitimate function — a human reaches these commands from an
    # ordinary terminal, and the orchestrator calls them in-process.
    if _RECOVERY_CMD_RE.search(cmd):
        deny("'wf accept', 'wf clear-invocation' and 'wf approve' record human "
             "decisions or recover a hooks-dark session. This gate is running, "
             "which proves neither case applies: you are an agent inside a "
             "governed session and may not sign your own gate. If you are stuck, "
             "write your artifact with status: blocked and stop.")

    if any(s in cmd for s in _PROTECTED_SUBSTRINGS) and \
            any(t in cmd for t in _MUTATION_TOKENS):
        deny("command touches governance-protected files with a mutating "
             "operation; state/policy/hooks are mutated only via the wf CLI")

    _, st = S.load_state(root, wid)
    _check_role(st)
    s = st["state"]

    if re.search(r"\bgit\s+push\b|\bgh\s+pr\s+(merge|create)\b", cmd):
        has_deploy = any(a["gate"] == "deployment" and
                         a["decision"] in ("approved", "force_approved")
                         for a in st["approvals"])
        if s != "APPROVED" or not has_deploy:
            deny("push/PR actions require state APPROVED and a recorded "
                 "deployment approval (wf approve deployment). APPROVED itself "
                 "now requires a human sign-off on the review — an LLM verdict "
                 "cannot reach this point on its own.")

    if re.search(r"\bgit\s+commit\b", cmd) and s not in ("IMPLEMENTING", "APPROVED"):
        deny(f"git commit is only permitted in IMPLEMENTING or APPROVED (state: {s})")

    if re.search(r"\b(pip3?|npm|pnpm|yarn|apt(-get)?|brew|cargo|gem)\s+(install|add)\b",
                 cmd) and s != "IMPLEMENTING":
        deny(f"package installation is only permitted in IMPLEMENTING (state: {s})")

    allow()


# --------------------------------------------------------------------------- #

def _attest_liveness(root: Path, wid: str) -> None:
    """Leave one hook-sourced event per workflow leg.

    This is what proves the enforcement plane fired *for this workflow, since it
    last moved* — the signal the orchestrator checks after every agent
    invocation and `wf transition` checks before every move. Once per leg keeps
    it free: the check reads backwards to the last transition, so a second event
    would tell nobody anything new.
    """
    try:
        if not ledger.hook_event_since_last_transition(root, wid):
            ledger.log(root, wid, "hook_liveness", hook="PreToolUse",
                       pid=os.getpid(), role=_role())
    except OSError:
        pass


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
    _CTX.update({"root": root, "wid": wid, "tool": tool})
    if wid:
        _attest_liveness(root, wid)

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
