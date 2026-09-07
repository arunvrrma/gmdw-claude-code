#!/usr/bin/env python3
"""GMDW PostToolUse ledger — artifact registration and audit.

Runs after Write/Edit/MultiEdit inside an agent's session and registers (hashes)
any artifact written under the run's artifacts/ directory, so the acceptance
pipeline and the finalize tamper check can trust it. The ``artifact_written``
event it emits is also one of the hook-sourced liveness signals the orchestrator
requires from every agent invocation.

Since v0.3.0 this hook no longer accounts token usage or closes invocations. The
deterministic orchestrator owns the invocation lifecycle — it opens the
single-flight lock, reads the child session's reported usage, and closes the
record — so doing it here too would double-count every stage.

PostToolUse cannot deny a completed action; it only records. It never raises to
the model — on internal error it exits 0 (the SubagentStop validator and the
PreToolUse gate remain the enforcing layers).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gmdw import heartbeat, ledger  # noqa: E402
from gmdw import state as S  # noqa: E402
from gmdw.state import GmdwError  # noqa: E402


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
    ledger.log(root, wid, "artifact_written", path=run_rel, state=st["state"],
               role=os.environ.get("GMDW_ROLE"))


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
    if data.get("tool_name", "") in ("Write", "Edit", "MultiEdit"):
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
