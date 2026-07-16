#!/usr/bin/env python3
"""GMDW SubagentStop validator — artifact acceptance gate.

When a subagent finishes, this hook decides whether its artifact is ACCEPTED
(advancing the workflow) or REJECTED (a failed invocation the Orchestrator may
retry once). The decision itself — existence, schema, semantic cross-checks,
counter bumps, pointer update, freeze — lives in gmdw/accept.py, which `wf
accept` shares, so the hook and the human recovery path validate identically.

This hook is the session-plane adapter around that core: it resolves which
artifact the finished invocation owed, translates the core's Result into the
"block" reason channel the model reads, and owns the invocation lifecycle
(open_invocation, consecutive_failures) and the state save.

FAIL-CLOSED: on rejection it bumps consecutive_failures and logs the reason;
it does not advance any pointer. On internal error while a workflow is active
it records an integrity_hold.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmdw import accept, heartbeat, ledger  # noqa: E402
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

    res = accept.accept_artifact(root, st, atype, version)

    if res.outcome == "blocked":
        # The agent declared it could not finish: release the invocation so the
        # Orchestrator can escalate, but advance nothing.
        st["open_invocation"] = None
        S.save_state(root, wid, st)
        ledger.log(root, wid, res.event, **res.extra)
        block(res.reason)

    if not res.accepted:
        # rejected and integrity_hold take the same session-plane path; the
        # integrity flag and its integrity_violation event were already written
        # (and saved) inside the core.
        _fail(root, wid, st, res.reason, res.event, **res.extra)

    # accepted: the core advanced the pointer and froze the artifact; close out
    # the invocation and persist.
    st["counters"]["consecutive_failures"] = 0
    st["open_invocation"] = None
    S.save_state(root, wid, st)
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
