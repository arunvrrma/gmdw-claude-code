# ADR-001 Phase 2 — Implementation Plan (proposed)

- **Status:** Proposed (Phase 1 implemented; this is the follow-on)
- **Depends on:** ADR-001 (authoritative) and its Phase 1 (heartbeat + `wf init`
  liveness refusal), now landed.
- **Goal:** finish moving artifact-acceptance authority into the workflow core,
  extend liveness enforcement to mid-run transitions, and add a human-gated
  recovery path — so a governed run can never be *silently* ungoverned and a
  valid-but-unaccepted artifact is never stranded.

Phase 1 closed the **entry** (a workflow can no longer *start* without live
hooks). Phase 2 closes the **middle** (hooks going dark mid-run) and the
**recovery** (accepting an artifact that was validly produced while hooks were
absent), and pays down the root cause behind both: acceptance logic living only
in a hook.

---

## Work item 1 — Extract the accept pipeline into `gmdw/accept.py` (RC-1)

**Why:** today the accept sequence (existence → schema → blocked-status →
semantic cross-checks → counter bumps → pointer update → freeze → ledger) lives
*only* in `subagentstop_validate.py`. It is pure, disk-only logic owned by the
session-scoped plane. Relocating it makes it callable from both the hook and the
CLI, which is the precondition for `wf accept` (item 3).

**What:**
- Create `gmdw/accept.py` with a single entry point, e.g.
  `accept_artifact(root, st, atype, version, *, actor, rerun_tests) -> Result`,
  where `Result` carries `outcome ∈ {accepted, rejected, blocked, integrity_hold}`
  and a `reason`. It performs the full pipeline and returns; the **caller** saves
  state and emits the block/ok channel. Keep it side-effect-explicit (it may
  freeze + ledger-log, since those are part of acceptance, but must not touch the
  block/ok protocol — that is hook/CLI presentation).
- Move `_semantic` and the `_subject_matches` helper out of
  `subagentstop_validate.py` into `accept.py` (the guard in `state.py` has its own
  copy of `_subject_matches` — consider consolidating to one home to kill the
  duplication noted in the ADR).
- Reduce `subagentstop_validate.py:main` to a thin adapter: resolve the active
  invocation (`open_invocation` → `last_invocation` fallback, unchanged), call
  `accept_artifact`, then translate: `accepted` → `ok()`; `rejected` → bump
  `consecutive_failures`, retain `open_invocation`, `block(reason)`; `blocked` →
  clear `open_invocation`, `block(reason)`; `integrity_hold` → set flag,
  `block(reason)`. **Behavior must be byte-for-byte preserved** — the existing 26
  tests (esp. P3/P5/P6/13/13b) are the oracle.

**Risk / mitigation:** this is the most safety-critical code in the repo. Land it
as a **pure refactor with all existing + Phase-1 tests green before adding any new
capability**. Do not combine with item 3 in one change.

## Work item 2 — Liveness re-check in `wf transition` (RC-2, mid-run)

**Why:** Phase 1 only guards `init`. A session that switches environment *after*
init (or a future hook regression) would still deadlock with a misleading guard
message ("repo_analysis artifact required") instead of the true cause.

**What:**
- In `cmd_transition` (or at the top of `state.transition`), when a workflow is
  ACTIVE and the target is not `ESCALATED`/`FAILED`/`REJECTED`, verify liveness.
  If not live and not overridden, refuse with a message naming the real cause,
  parallel to the init refusal.
- **Predicate (deterministic, disk-only):** wall-clock heartbeat freshness alone
  is fragile across long subagent runs. Prefer *"a hook-sourced event
  (`invocation_requested`, `invocation_completed`, `artifact_accepted`,
  `artifact_written`, …) appears in this workflow's ledger since its last
  `transition` event"* — every legal transition is naturally preceded by hook
  firings — and fall back to heartbeat freshness. Leave the exact rule to
  implementation but keep it reconstructable from disk.
- **Escape hatch:** consistent with init (`GMDW_ALLOW_UNGOVERNED` or a
  `--allow-ungoverned` transition flag), **ledger-logged on every use while
  ACTIVE** (item stronger than init because a workflow now exists).

**Test-suite impact (must handle):** the existing `self.wf("transition", …)`
tests in `TestLimits`/`TestArtifacts`/`TestSecurityAndDeploy` seed state directly
and fire no hooks, so a naive check would fail them. Resolve by **seeding a fresh
heartbeat in `Base.setUp`** (those tests represent a *governed* session, which is
what they mean to test) and add dedicated *hookless-transition* tests separately.
Document this in the test file.

## Work item 3 — `wf accept`: human-gated recovery command

**Why:** recover the exact `wf_20260713_003` situation — a schema-valid artifact
on disk that no hook ever accepted — without inventing an orchestrator-invocable
second acceptance authority (rejected alternatives B/C in the ADR).

**What:**
- New subparser mirroring `wf approve`: `wf accept --by <human> [--workflow <id>]`.
- Accepts **only** the exact artifact `wf expected` names for the current state
  (its `artifact_type` + next `version`); refuse otherwise. Refuse if already
  accepted/frozen. Require `--by` (human attribution).
- Run the **identical** `accept_artifact` pipeline from item 1 (schema +
  semantics + test-rerun where applicable) — an invalid artifact is rejected
  exactly as the hook would.
- On success: advance the pointer, freeze, ledger `artifact_accepted` with a
  **distinct actor `human_recovery`** and `by`, so audits always separate
  hook-accepted from human-recovered artifacts. This human attestation is the
  compensating control for the invocation-lifecycle checks (GMDW-INVOCATION
  header, model-pin, budget) that cannot be reconstructed after the fact.
- **Do not** make this orchestrator-friendly. It is a recovery gate; keep it in
  the same category as `wf approve`.

## Work item 4 — Regression tests (Phase 2)

- **Refactor safety:** all 26 existing + 6 Phase-1 tests stay green (they now
  drive the extracted core through the thin hook — this *is* the refactor test).
- **`wf accept` happy path:** reproduce `wf_20260713_003` — write a valid
  `repo_analysis_v1.json`, `current.repo_analysis == 0`, `wf accept` → pointer
  advances to 1, artifact frozen, ledger shows `actor: human_recovery`, and the
  previously-blocked `REPOSITORY_ANALYSIS → PLANNING` now passes.
- **`wf accept` rejects** invalid / wrong-version / already-frozen artifacts with
  no state change.
- **`wf accept` requires `--by`.**
- **Mid-run transition refusal:** ACTIVE workflow, hooks dark (no heartbeat / no
  hook events since last transition) → `wf transition` refuses with the liveness
  diagnostic; with the override it proceeds and logs the bypass.
- **(Optional) unit tests** of `accept_artifact` per artifact-type semantic branch.

## Work item 5 — Docs & status

- `KNOWN_LIMITATIONS.md` R4/F1 → "closed end-to-end (init + mid-run + recovery)".
- `.claude/commands/gmdw.md` §Notes → mention `wf accept` as the human recovery
  path when an artifact is stranded, and that transitions verify hook liveness.
- ADR-001 status → Accepted / Implemented.

## Sequencing & migration

1. Item 1 (pure refactor) → green.
2. Item 3 (`wf accept`) on top of the extracted core → green.
3. Item 2 (transition liveness + `Base.setUp` heartbeat seeding) → green.
4. Item 5 docs.

No schema changes. No state-shape changes except a test-only heartbeat seed in
`Base.setUp`. Closed runs are immutable history and are not migrated.

## Out of scope (file separately — from ADR §9)

Run-ID recycling when run dirs are pruned; the GIT_STRATEGY vs LIVE_VALIDATION_*
doc drift about committing run folders; the trivial-route human-gate question.
