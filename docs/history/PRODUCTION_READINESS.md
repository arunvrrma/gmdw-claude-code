# GMDW — Production Readiness Review

> **Historical (v0.2.x).** This document describes the six-agent,
> LLM-orchestrated architecture that v0.3.0 replaced, and the validation
> done against it. It is kept for provenance. For what GMDW is now, see
> [`MIGRATION.md`](../../MIGRATION.md), [`docs/KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md)
> and [`docs/SMOKE_TESTS.md`](../SMOKE_TESTS.md).

> **Historical (MVP / v0.1.0).** This is the MVP-era assessment. For the current
> general-use (v0.2.0) re-score — several gaps below are now closed (the silent
> hook-non-firing risk, the manual orphan-clear) — see
> [`RELEASE_READINESS.md`](RELEASE_READINESS.md).

Assessment of the MVP against five readiness dimensions. Scores are `0–5`
(0 = absent, 3 = works but unproven live, 5 = proven and operable). The dominant theme:
the **logic** is complete and verified offline; **live wiring** is the gating unknown.
No score can exceed 3 until `LIVE_VALIDATION_PLAN.md` passes.

| Dimension | Score | One-line justification |
|---|---|---|
| Operational readiness | 3 / 5 | CLI + hooks work offline; single-active-workflow; no live run yet |
| Governance readiness | 4 / 5 | Enforcement is code-based and test-proven; only model-policy is runtime-dependent |
| Security readiness | 3 / 5 | Dedicated security review + secret/push gating; Bash gate is pattern-based |
| Audit readiness | 4 / 5 | Complete, append-only, hash-anchored trail; no external log shipping yet |
| Recovery readiness | 3 / 5 | State-file recovery is sound; orphaned-invocation cleanup is manual |

**Overall: not yet production-ready; ready for a controlled first live run.** Clear the
live plan and the two model/token open items, then re-score.

---

## Operational readiness — 3/5

- **Justification:** `wf` covers the full lifecycle; hooks are stdlib-only (no install);
  atomic writes prevent partial state; the offline suite + a full CLI lifecycle walk
  pass. The system runs one workflow at a time, which is appropriate for the MVP.
- **Remaining gaps:** never executed inside Claude Code (R4, V1); no operator dashboard
  (status is CLI-only); `wf` is not on `PATH` by default; no log rotation for
  long-lived repos.
- **Recommendations:** run V1 pre-flight first; add a shell alias or `PATH` entry for
  `.workflow/bin/wf`; treat `decision_log.jsonl` growth as per-workflow (it is), so no
  rotation needed at MVP scale.

## Governance readiness — 4/5

- **Justification:** every guarantee in `docs/history/HANDOFF.md` §3 is enforced by a hook or guard
  and covered by an adversarial test. Structural isolation (no `Agent` tool on
  subagents) does not depend on runtime behavior at all. Counters are checked in two
  places (gate + guard).
- **Remaining gaps:** the per-role **model policy** is the one guarantee that depends on
  the runtime passing `model` to the hook (R3, Q3) — until V3, treat it as advisory.
  `trivial_max_files` is unenforced (accepted).
- **Recommendations:** confirm V3; if the runtime does not surface `model`, document
  model policy as advisory in the release notes rather than claiming enforcement.

## Security readiness — 3/5

- **Justification:** a dedicated Security Reviewer runs on both plan and code for
  flagged work; flagged workflows cannot skip the security states (guard-enforced);
  secrets/push/PR/installs are state-gated; approval artifacts are write-locked to
  `wf approve`; the false-test-pass integrity trap is proven.
- **Remaining gaps:** the Bash gate is pattern-based and could be evaded by obfuscated
  commands (protected *files* remain safe via the Write layer + permissions); no
  dependency-vuln scanning beyond the reviewer's manual lockfile check; security
  classification depends on the Orchestrator scoring the security dimension correctly
  (human override exists).
- **Recommendations:** during the live run, attempt a few obfuscated Bash evasions
  (env indirection, unusual quoting) against protected paths and record results; keep
  the human override at classification as the backstop for mis-scored security.

## Audit readiness — 4/5

- **Justification:** every transition, invocation, artifact, approval, and denial is
  logged append-only; artifacts are hash-anchored in `artifacts_index.json` and frozen;
  the run directory is a self-contained, diff-able evidence bundle; provenance of any
  output is walkable via `input_artifact_refs`.
- **Remaining gaps:** logs are local only (no SIEM/log-shipping); no signing of the
  decision log (tamper of the *log itself* is detectable only by git, not by an
  internal MAC); no retention policy.
- **Recommendations:** commit the run directory alongside the code change (git becomes
  the external anchor); if compliance requires it, add out-of-band log shipping at the
  integration layer — no code change to GMDW needed.

## Recovery readiness — 3/5

- **Justification:** `state.json` + artifacts fully describe position; atomic writes
  guarantee no torn state; `base_commit` ancestry blocks stale-workspace merges;
  integrity hold safely freezes on tamper/false-pass.
- **Remaining gaps:** an invocation in flight at crash leaves `open_invocation` set and
  requires a **manual** escalate-to-clear (no automatic orphan reaper, R-C1); no
  automated un-hold; resume is "re-run `/gmdw`", not a dedicated `wf resume`.
- **Recommendations:** validate V8 explicitly; document the manual orphan-clear step in
  the runbook (it is in `FIRST_LIVE_RUN_CHECKLIST.md` §Rollback); consider the manual
  step acceptable for the MVP.

---

## Release gate

Promote from "controlled first run" to "team production use" when:
1. `LIVE_VALIDATION_PLAN.md` V1–V8 pass (or deviations are accepted-mode + recorded).
2. All 10 adversarial cases + the integrity bonus reproduce their denials live.
3. Q1–Q5 answered; model policy confirmed enforcing or documented advisory.
4. Smoke Tests A/B/C reproduce their documented state paths.
5. One recovery-after-interruption drill (V8) completes with a clean audit trail.

Until then the system is safe to run **because failure is fail-closed** — the worst
live outcome is that a hook does not fire and work is *blocked*, not that a violation
is *allowed* (the one exception to watch, per Adversarial §protocol, is a hook silently
not firing — V1 is the guard against exactly that).
