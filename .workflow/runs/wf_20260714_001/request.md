Implement ADR-001 Phase 2 per docs/adr/ADR-001-phase2-plan.md

ADR-001 Phase 1 (governance-hook liveness attestation + `wf init` refusal) is
merged. Now implement Phase 2, which finishes moving artifact-acceptance
authority into the workflow core and extends liveness enforcement beyond init.

Source of truth: docs/adr/ADR-001-phase2-plan.md (authoritative), grounded in
docs/adr/ADR-001-artifact-acceptance-and-hook-liveness.md (the ADR) and
docs/adr/ADR-001-phase1-implementation-review.md (findings F-2..F-8 are
advisory; F-1 is already fixed).

Scope (the four Phase 2 work items):

1. Extract the artifact-acceptance pipeline out of the SubagentStop hook into a
   pure, repository-scoped workflow-core module (e.g. .claude/hooks/gmdw/accept.py):
   existence -> schema -> blocked-status -> semantic cross-checks -> counter bumps
   -> current-pointer update -> freeze -> ledger. subagentstop_validate.py becomes a
   thin adapter that resolves the active invocation and translates the core result to
   the block/ok protocol. This MUST be behavior-preserving: the existing governance
   suite is the oracle (P3/P5/P6/13/13b especially). Consolidate the duplicated
   _subject_matches / _semantic logic into one home.

2. Add a transition-time liveness re-check in `wf transition` (mid-run), so a
   session that loses its hooks after init fails fast with a diagnostic naming the
   real cause instead of a misleading guard message. Use a deterministic, disk-only
   predicate (prefer: a hook-sourced ledger event since the workflow's last
   transition, falling back to heartbeat freshness). Provide an explicit,
   ledger-logged escape hatch consistent with init. Seed a fresh heartbeat in the
   test Base.setUp so existing transition tests represent a governed session.

3. Add a human-gated `wf accept` recovery command (mirroring `wf approve`): accepts
   ONLY the exact artifact `wf expected` names for the current state, runs the
   identical core acceptance pipeline, requires --by (human attribution), and records
   a distinct ledger actor (human_recovery). This recovers the wf_20260713_003
   scenario (a valid artifact stranded because no hook accepted it).

4. Add regression tests: refactor-safety (existing suite green through the thin
   hook), `wf accept` happy path (stranded repo_analysis -> accepted -> transition
   unblocks), `wf accept` rejects invalid/wrong-version/already-frozen, `wf accept`
   requires --by, and mid-run transition refusal when hooks go dark.

Constraints / invariants that must hold:
- Governance enforced by code, never by prompts.
- state.json mutated ONLY via wf/state.py/hooks; artifacts immutable once accepted.
- No schema changes. No state-shape changes except a test-only heartbeat seed.
- All existing governance tests must remain green.
- Reconcile the bypass env-var naming with Phase 1 (GMDW_ALLOW_UNGOVERNED_INIT)
  and carry the F-1 future-skew bound into any new liveness predicate.

Out of scope (do not bundle): run-ID recycling on pruned run dirs; GIT_STRATEGY vs
LIVE_VALIDATION doc drift; the trivial-route human-gate question.
