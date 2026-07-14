#!/usr/bin/env python3
"""GMDW PostToolUse ledger — usage accounting, artifact registration, audit.

Runs after Agent/Task completions and after Write/Edit/MultiEdit. It:
  - closes the open_invocation (moving it to last_invocation) and accounts
    token usage per role + budget;
  - increments/resets consecutive_failures on invocation error/success;
  - registers (hashes) any artifact written under the run's artifacts/ dir,
    so the SubagentStop validator and the finalize tamper check can trust it.

PostToolUse cannot deny a completed action; it only records. It never raises
to the model — on internal error it logs and exits 0 (the SubagentStop
validator and PreToolUse gate remain the enforcing layers).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmdw import heartbeat, ledger  # noqa: E402
from gmdw import state as S  # noqa: E402
from gmdw.state import GmdwError  # noqa: E402


def _extract_tokens(resp) -> tuple[int, bool]:
    """Best-effort token count from a tool_response. Returns (tokens, exact)."""
    if isinstance(resp, dict):
        for key in ("usage", "total_usage"):
            u = resp.get(key)
            if isinstance(u, dict):
                it = u.get("input_tokens", 0) or 0
                ot = u.get("output_tokens", 0) or 0
                ct = u.get("cache_read_input_tokens", 0) or 0
                cc = u.get("cache_creation_input_tokens", 0) or 0
                total = int(it) + int(ot) + int(ct) + int(cc)
                if total:
                    return total, True
        for key in ("total_tokens", "tokens"):
            if isinstance(resp.get(key), int):
                return resp[key], True
    # Fallback: rough estimate from serialized size.
    text = json.dumps(resp) if resp is not None else ""
    return max(1, len(text) // 4), False


def handle_agent(root: Path, wid: str, data: dict):
    _, st = S.load_state(root, wid)
    inv = st.get("open_invocation")
    resp = data.get("tool_response")
    errored = bool(data.get("is_error")) or (
        isinstance(resp, dict) and resp.get("is_error"))

    agent = (inv or {}).get("agent") or (data.get("tool_input") or {}).get(
        "subagent_type", "unknown")
    tokens, exact = _extract_tokens(resp)

    b = st["budget"]
    b["tokens_consumed_estimate"] += tokens
    b["usage_by_role"][agent] = b["usage_by_role"].get(agent, 0) + tokens
    model = st["policy"]["snapshot"]["models"].get(agent, {}).get("model")
    b["cost_usd_estimate"] = round(
        b["cost_usd_estimate"] + ledger.est_cost(model, tokens), 4)

    if errored:
        st["counters"]["consecutive_failures"] += 1
        ledger.log(root, wid, "invocation_failed", agent=agent,
                   tokens=tokens, estimated=not exact,
                   consecutive_failures=st["counters"]["consecutive_failures"])
    else:
        st["counters"]["consecutive_failures"] = 0
        ledger.log(root, wid, "invocation_completed", agent=agent,
                   tokens=tokens, estimated=not exact,
                   expected=(inv or {}).get("expected_artifact"))

    st["last_invocation"] = {**(inv or {"agent": agent}),
                             "closed_at": S.utcnow(),
                             "errored": errored, "tokens": tokens}
    st["open_invocation"] = None
    S.save_state(root, wid, st)


def handle_write(root: Path, wid: str, data: dict):
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ""
    if not path:
        return
    p = Path(path)
    if not p.is_absolute():
        p = Path(data.get("cwd") or os.getcwd()) / p
    try:
        rel = p.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return
    run_prefix = f".workflow/runs/{wid}/"
    if not rel.startswith(run_prefix + "artifacts/"):
        return
    if not p.exists():
        return
    run_rel = rel[len(run_prefix):]
    # Register (hash) the written artifact. Frozen artifacts are skipped by the
    # ledger. This is a live draft registration, not a freeze.
    _, st = S.load_state(root, wid)
    ledger.register(root, wid, run_rel, state_name=st["state"])
    ledger.log(root, wid, "artifact_written", path=run_rel, state=st["state"])


def main():
    data = json.load(sys.stdin)
    cwd = data.get("cwd") or os.getcwd()
    try:
        root = S.find_root(cwd)
    except GmdwError:
        sys.exit(0)
    heartbeat.touch(root, "PostToolUse")  # attest the enforcement plane is live
    wid = S.active_id(root)
    if not wid:
        sys.exit(0)
    tool = data.get("tool_name", "")
    if tool in ("Agent", "Task"):
        handle_agent(root, wid, data)
    elif tool in ("Write", "Edit", "MultiEdit"):
        handle_write(root, wid, data)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # PostToolUse never blocks; enforcement lives in the other two hooks.
        sys.exit(0)
