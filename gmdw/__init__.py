"""GMDW runtime — Governed Multi-Agent Delivery Workflow.

Python standard library only. This package is BOTH the deterministic
orchestrator (``python -m gmdw.orchestrator``) and the governance runtime the
three Claude Code hooks import. It is the ONLY code path that may mutate
``.workflow/runs/<id>/state.json``, the decision log, and the artifact registry.

Layout
------
``orchestrator``  the deterministic router: reads state, invokes one agent,
                  parses + validates its Markdown artifacts, advances state.
``runner``        the ``claude`` CLI adapter (the only place an LLM is called).
``parsers``       structured-Markdown artifact parsing and validation.
``state``         the state machine, transition table and guards.
``policy``        policy profiles: limits, budgets, gates, model tiers.
``accept``        the artifact-acceptance pipeline + human-approval gate.
``ledger``        the audit log, artifact hash registry, cost estimates.
``heartbeat``     governance-liveness attestation written by the hooks.
``schemas``       JSON-Schema subset validation (approvals only since v0.3.0).
"""

__all__ = ["state", "policy", "schemas", "ledger", "heartbeat", "accept",
           "parsers", "runner", "orchestrator"]
__version__ = "0.3.0"
