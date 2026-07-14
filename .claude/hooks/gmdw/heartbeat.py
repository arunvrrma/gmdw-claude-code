"""GMDW governance-liveness heartbeat (ADR-001 Phase 1).

The three governance hooks (PreToolUse gate, PostToolUse ledger, SubagentStop
validator) touch a heartbeat file on every firing, before any decision logic.
The ``wf`` CLI reads it to prove the enforcement plane is actually live in the
current Claude Code session *before* creating a workflow.

Why this exists
---------------
GMDW has two planes that are meant to be inseparable:

* the **state plane** (``wf`` + ``state.py``) is repository-scoped — it finds the
  repo by walking up from the cwd, so it runs in any session, from anywhere;
* the **enforcement plane** (the three hooks) is session-scoped — it exists only
  when Claude Code loaded THIS project's ``.claude/settings.json``.

A workflow launched from a session rooted elsewhere gets the state plane but no
enforcement plane: invocation gating, write-scoping, budgets and — critically —
artifact acceptance all silently vanish, and the workflow either deadlocks or
(on the trivial route) runs entirely ungoverned. The heartbeat lets ``wf init``
fail fast with an actionable message instead.

Trust properties
----------------
The heartbeat is runtime state, not history. It is never registered in the
tamper index, is git-ignored, and is governance-protected (only the hook
processes themselves — not any model-issued tool call — may write it). A hookless
session cannot produce it; a governed session refreshes it on the very Bash call
that runs ``wf init`` (PreToolUse fires immediately before the command executes),
so the attestation is effectively atomic.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

# Run-dir-relative to the repo root, alongside ACTIVE / config.json.
HEARTBEAT_REL = ".workflow/.hooks_heartbeat"

# Freshness window. The init self-attestation is effectively instantaneous, so
# this generous default only bounds how long a prior governed session's heartbeat
# is trusted to vouch for the current one. Override via GMDW_HEARTBEAT_MAX_AGE_SEC.
DEFAULT_MAX_AGE_SEC = 300

# Tolerance for a heartbeat stamped slightly in the future (benign same-machine
# clock jitter). Anything beyond this is rejected: without an upper bound a
# future-dated beacon would satisfy the freshness check forever, turning the
# bounded trust window into an unbounded skeleton key (ADR-001 review F-1). This
# is a fixed guard, deliberately NOT env-tunable, so the bound cannot be widened.
MAX_FUTURE_SKEW_SEC = 60

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _path(root) -> Path:
    return Path(root) / HEARTBEAT_REL


def touch(root, event: str) -> None:
    """Record that a governance hook fired. Best-effort: never raises, so a
    heartbeat problem can only cause a fail-safe ``wf init`` refusal, never break
    or leak a live hook decision. Writes atomically (tmp + os.replace)."""
    try:
        payload = {"ts": _now().strftime(_TS_FMT), "event": event,
                   "pid": os.getpid()}
        p = _path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def max_age_sec() -> int:
    v = os.environ.get("GMDW_HEARTBEAT_MAX_AGE_SEC")
    if v:
        try:
            return max(1, int(v))
        except ValueError:
            pass
    return DEFAULT_MAX_AGE_SEC


def read(root) -> dict | None:
    p = _path(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def status(root) -> tuple[bool, str]:
    """Return (live, reason). ``live`` is True iff a governance hook fired within
    the freshness window — i.e. the enforcement plane is active in this session."""
    hb = read(root)
    if hb is None:
        return False, ("no governance-hook heartbeat found; the GMDW enforcement "
                       "hooks are not active in this session")
    try:
        when = datetime.datetime.strptime(hb.get("ts", ""), _TS_FMT).replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return False, "governance-hook heartbeat is unreadable (corrupt timestamp)"
    age = (_now() - when).total_seconds()
    if age < -MAX_FUTURE_SKEW_SEC:
        # Stamped implausibly far in the future: reject. A future timestamp can
        # never legitimately outrun a small clock skew, and treating it as fresh
        # would let a single planted beacon vouch for liveness indefinitely.
        return False, (f"governance-hook heartbeat is timestamped in the future "
                       f"({int(-age)}s ahead); refusing to trust it")
    if age < 0:
        age = 0  # benign same-machine clock jitter within MAX_FUTURE_SKEW_SEC
    if age > max_age_sec():
        return False, (f"governance-hook heartbeat is stale ({int(age)}s old, max "
                       f"{max_age_sec()}s); the enforcement hooks are not firing "
                       f"in this session")
    return True, f"governance hooks live (heartbeat {int(age)}s old)"
