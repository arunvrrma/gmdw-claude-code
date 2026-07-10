"""GMDW runtime library — Governed Multi-Agent Delivery Workflow.

Python standard library only. This package is imported by the three Claude
Code hooks and by the `wf` CLI. It is the ONLY code path that may mutate
`.workflow/runs/<id>/state.json`, the decision log, and the artifact registry.
"""

__all__ = ["state", "policy", "schemas", "ledger"]
__version__ = "1.0.0"
