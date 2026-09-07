# Archived process reports

Point-in-time reports, validation plans and release gates from earlier GMDW
architectures, kept for provenance. They are historical records, not living
documentation — current guidance is in the top-level [`README.md`](../../README.md),
[`MIGRATION.md`](../../MIGRATION.md), [`docs/GETTING_STARTED.md`](../GETTING_STARTED.md),
[`docs/KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) and
[`docs/SMOKE_TESTS.md`](../SMOKE_TESTS.md).

Cross-references inside these files point at paths as they were when the files
were written; some of those paths have since moved. That is expected of an
archive and is not worth repairing.

## From v0.2.x — the six-agent, LLM-orchestrated architecture

Archived on the v0.3.0 rework, which replaced the LLM Orchestrator with a
deterministic Python program, consolidated six agents into three, moved artifacts
from JSON to structured Markdown, and made human approval mandatory. Everything
these documents validated was validated against the *previous* system; leaving
them in the live `docs/` tree would have asserted things about GMDW that are no
longer true.

- `ADVERSARIAL_VALIDATION.md` — the offline adversarial scenarios and their
  expected denials. The scenarios themselves live on, rewritten, as the `H`, `A`
  and `R` groups in `tests/test_governance.py` (see `tests/README.md`).
- `LIVE_VALIDATION_PLAN.md` — the V1–V8 live checks for the v0.2 runtime
  assumptions (hook order, `tool_response` token shape, header binding). Most of
  those assumptions no longer exist; the live questions that replaced them are
  §D of `docs/KNOWN_LIMITATIONS.md`, and the runs are `docs/SMOKE_TESTS.md`.
- `FIRST_LIVE_RUN_CHECKLIST.md` — the operator runbook for the first v0.2 live run.
- `PRODUCTION_READINESS.md` — the MVP-era readiness scoring.
- `RELEASE_READINESS.md` — the v0.2.0 release gate.

## From the MVP (v0.1.0)

- `HANDOFF.md` — implementation handoff to the engineer running the first live
  validation.
- `LIVE_VALIDATION_READINESS.md` — the readiness gate for the first live-validation
  run on the (since-merged) `live-validation` branch.
- `LIVE_VALIDATION_EXECUTION.md` — the operator-followable process used for it.
- `RELEASE_CHECKLIST.md` — the gate checklist for cutting the frozen `v0.1.0-mvp`
  baseline.
- `PRE_FLIGHT_VALIDATION_REPORT.md`, `PRE_FLIGHT_PUBLICATION_REPORT.md`,
  `PUBLICATION_REPORT.md`, `WORKFLOW_A_VALIDATION_REPORT.md`,
  `WORKFLOW_A_BLOCKED_PUBLICATION_REPORT.md` — the reports those gates produced.

One curated example run (`wf_20260714_001`) is committed for reference; new runs
stay local by default (see `.gitignore`). Other run IDs cited in ADRs and
historical docs were local runs and are intentionally not in the tree.
