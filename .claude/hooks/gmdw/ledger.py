"""GMDW ledger — audit log, artifact registry (hashes), budget helpers.

The decision log is append-only JSONL. The artifact registry maps each
artifact's run-relative path to its sha256; entries marked frozen=True are
immutable and are re-verified by `wf close` and the APPROVED->COMPLETED guard.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

# Blended $/MTok estimates (assumes ~80/20 input/output mix) used only for the
# advisory cost_usd_estimate field. Budgets are enforced in tokens, not dollars.
MODEL_RATES = {"opus": 9.0, "sonnet": 5.4, "haiku": 1.8}


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_dir(root, wf_id) -> Path:
    return Path(root) / ".workflow" / "runs" / wf_id


def log(root, wf_id: str, event: str, **fields) -> None:
    """Append one event to the workflow's decision_log.jsonl."""
    entry = {"ts": utcnow(), "event": event}
    entry.update(fields)
    p = _run_dir(root, wf_id) / "decision_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _registry_path(root, wf_id) -> Path:
    return _run_dir(root, wf_id) / "artifacts_index.json"


def _load_registry(root, wf_id) -> dict:
    p = _registry_path(root, wf_id)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_registry(root, wf_id, reg: dict) -> None:
    p = _registry_path(root, wf_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def register(root, wf_id: str, rel: str, state_name: str | None = None,
             frozen: bool = False) -> dict:
    """Record (or update) an artifact's hash. `rel` is run-dir-relative,
    e.g. 'artifacts/plan_v1.json'. Returns the entry."""
    full = _run_dir(root, wf_id) / rel
    reg = _load_registry(root, wf_id)
    prev = reg.get(rel)
    if prev and prev.get("frozen") and not frozen:
        # A frozen artifact is never silently re-registered.
        return prev
    entry = {
        "sha256": sha256_file(full),
        "bytes": full.stat().st_size,
        "ts": utcnow(),
        "state": state_name,
        "frozen": bool(frozen or (prev or {}).get("frozen", False)),
    }
    reg[rel] = entry
    _save_registry(root, wf_id, reg)
    return entry


def freeze(root, wf_id: str, rel: str, state_name: str | None = None) -> dict:
    """Finalize an artifact: record its hash and mark it immutable."""
    return register(root, wf_id, rel, state_name=state_name, frozen=True)


def entry(root, wf_id: str, rel: str) -> dict | None:
    return _load_registry(root, wf_id).get(rel)


def is_frozen(root, wf_id: str, rel: str) -> bool:
    e = entry(root, wf_id, rel)
    return bool(e and e.get("frozen"))


def verify(root, wf_id: str) -> list:
    """Re-hash every frozen artifact; return a list of mismatch records."""
    mismatches = []
    reg = _load_registry(root, wf_id)
    for rel, e in sorted(reg.items()):
        if not e.get("frozen"):
            continue
        full = _run_dir(root, wf_id) / rel
        if not full.exists():
            mismatches.append({"path": rel, "problem": "missing"})
            continue
        actual = sha256_file(full)
        if actual != e["sha256"]:
            mismatches.append({"path": rel, "problem": "hash_mismatch",
                               "expected": e["sha256"], "actual": actual})
    return mismatches


def est_cost(model: str | None, tokens: int) -> float:
    return round(MODEL_RATES.get(model or "", 5.4) * tokens / 1_000_000, 4)
