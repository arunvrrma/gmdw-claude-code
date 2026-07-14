# ADR-001 Phase 1 — Implementation Review

- **Review date:** 2026-07-14
- **Reviewer role:** ADR author, acting as architectural reviewer
- **Scope:** Phase 1 only (heartbeat liveness attestation + `wf init` preflight).
  Phase 2 is unimplemented by design; its plan (`ADR-001-phase2-plan.md`) is
  assessed for readiness only.
- **Method:** full read of every changed file; independent re-run of the test
  suite; **empirical adversarial probes against the real code** (not just code
  reading) for the scenarios in findings F-1 and F-2.
- **Verdict: REQUEST CHANGES — narrowly.** One high-severity defect (F-1, a
  one-line fix plus one regression test) must land before merge. Everything else
  is faithful to the ADR and advisory-only. With F-1 fixed, this review converts
  to APPROVE.

---

## 1. Executive summary

The implementation faithfully realizes ADR-001 Phase 1: the three hooks
unconditionally attest liveness via a heartbeat, `wf init` fails fast without a
fresh attestation, the escape hatch is explicit, attributed, and
ledger-recorded, and the heartbeat is kept out of git and the tamper registry.
The incident scenario (`wf_20260713_003`: silent ungoverned start from an
out-of-project session) is closed at the entry point. The state core
(`state.py`) is untouched — the check lives in `cmd_init` — so all 26 legacy
governance tests pass unmodified and Phase 2's acceptance extraction is
unimpeded. 32/32 tests were independently re-run and pass.

One concrete defect survives: `heartbeat.status()` clamps negative ages to
zero, so a **future-dated heartbeat is treated as fresh forever** (verified
empirically). Combined with the fact that the heartbeat file is only
write-protected *while a workflow is ACTIVE* (also verified — the gate's
`if not wid: allow()` precedes the protected-path check), a misbehaving model
in a governed but workflow-inactive session can plant a `ts: 2099-…` beacon
that permanently vouches for any later hookless session on that clone. This
converts the design's deliberately *bounded* trust window (300 s) into an
unbounded one — a violation of the module's own documented trust property, and
of GMDW's standard that a misbehaving model must be stopped by code, not by
transcript review. The fix is one line (reject timestamps beyond a small
clock-skew allowance) plus one test.

## 2. ADR conformance assessment

| ADR Phase-1 requirement | Status | Notes |
|---|---|---|
| Hooks write heartbeat unconditionally, before any decision logic, even with no ACTIVE workflow | ✅ | All three hooks; placed immediately after `find_root`; verified in code and via test L2 |
| Heartbeat write must never break a live hook decision | ✅ | `touch()` is best-effort (never raises), atomic (`tmp` + `os.replace`); worst failure mode is a fail-safe init refusal |
| `wf init` refuses without fresh attestation, actionable message | ✅ | Message names the cause and the remedy (relaunch from project root); refusal occurs before any side effect (test L1 asserts no workflow created) |
| Minutes-scale freshness window | ⚠️ | 300 s default, env-tunable — conformant **as specified**, but the bound is defeated by future-dated timestamps (F-1) |
| Explicit escape hatch for CI/manual ops, attributed, ledger-visible | ✅ | `GMDW_ALLOW_UNGOVERNED_INIT`; `governance_liveness_bypassed` ledger event carries reason + `--by`; verified by test L5 |
| Heartbeat gitignored, excluded from tamper registry | ✅ | `.gitignore` entry; exclusion is structural (never registered), not a special case |
| Heartbeat governance-protected (ADR §9: "only hooks write it") | ⚠️ | Added to Write and Bash protected lists, **but the protection engages only while a workflow is ACTIVE** (F-2) — the claim "only hook processes may write it" overstates what is enforced |
| Existing 26 tests unaffected; hookless-init tests added | ✅ | Legacy tests bypass the CLI init path exactly as the ADR anticipated; 6 new tests; 32/32 re-verified by this review |
| Docs updated (KNOWN_LIMITATIONS R4/F1, gmdw.md §0) | ✅ | R4/F1 correctly say "closed at entry; mid-run is Phase 2" |

No implementation choice materially altered the design. The two ⚠️ rows are
defects/overstatements *within* the design, not departures from it.

## 3. Governance integrity assessment

- **No new fail-open path.** The change is purely additive refusal logic on
  `init` plus passive attestation writes. Hook fail-closed behavior, guard
  logic, acceptance, freezing, and budgets are untouched (diff inspected:
  hooks gained only an import and a `touch()` call each).
- **"Enforced by code, never by prompts":** preserved in governed sessions.
  The bypass is code-gated (explicit env var) and audited. In hookless
  sessions, enforcement is inherently impossible (the ADR's own analysis); the
  check delivers the maximum achievable there — converting *silent, accidental*
  ungoverned starts (the actual incident) into explicit refusals. A model
  *deliberately* forging a present-dated heartbeat in a hookless session
  remains possible and always will; that residual is inherent and was accepted
  in the ADR. What is **not** acceptable is F-1 extending a forgery's validity
  from ≤300 s to forever, and enabling it to be pre-planted from a governed
  session (F-2 window).
- **Bypass audit:** correctly written into the *newly created run's* ledger, so
  an ungoverned-but-bypassed run is permanently self-describing. Good.
- **Init refusals leave no durable trace** (no run exists yet; the refusal
  lives only in the session transcript). Acceptable for Phase 1; noted as F-7.

## 4. Risk assessment (liveness design, attestation model, edge cases)

**Mechanism & attestation model.** The self-attestation property is sound and
empirically demonstrated (test L3): PreToolUse fires on the very Bash call that
executes `wf init`, so a governed session refreshes the beacon
moments before `wf init` reads it; a hookless session has no writer. Storage at
`.workflow/.hooks_heartbeat` (repo-scoped, beside `ACTIVE`) is correct: no
cross-**repo** contamination, and Phase 2's transition-time check can reuse
`status()` unchanged.

**Verified risks:**

- **Future-dated timestamp = permanent liveness (F-1, high).** Probe:
  `{"ts":"2099-01-01T00:00:00Z"}` → `status()` returns
  `(True, "heartbeat 0s old")`. Root cause: `if age < 0: age = 0`, intended as
  clock-skew tolerance, has no upper bound.
- **Protection inert in the window that matters most (F-2, medium).** Probe:
  with no ACTIVE workflow, both the Write gate and the Bash gate **allow**
  mutation of `.workflow/.hooks_heartbeat` (the `if not wid: allow()` early
  exits precede the protected-path checks). This is consistent with the repo's
  documented "normal project while inactive" posture, so it is not a
  regression — but the heartbeat is the first governance file whose integrity
  matters *before* any workflow exists, and the implementation summary's claim
  that only hooks can write it is true only while ACTIVE. With F-1 fixed, the
  exposure is bounded by the freshness window.
- **Same-machine cross-session vouching (F-3, medium).** A *fresh* heartbeat
  from a concurrently-open (or ≤300 s-idle) governed session vouches for a
  hookless session on the same clone. The ADR accepted the *stale* variant of
  this; the *concurrent* variant is strictly stronger and is not currently
  documented. Detection hooks exist for Phase 2: Claude Code passes
  `session_id` in hook stdin, so the heartbeat could carry it for diagnostics
  (the CLI cannot verify its own session, but a refusal/warning can display
  provenance).
- **CI / non-Claude operation.** Correctly handled: refusal by default,
  explicit audited bypass. One footgun: the bypass tests only truthiness of the
  env var, so `GMDW_ALLOW_UNGOVERNED_INIT=0` *activates* the bypass (F-4, low).
- **Workspace edge cases (F-5, low).** The gate touches the heartbeat only when
  the tool call's `cwd` resolves inside the repo. A governed session driving
  the repo from outside (`cd /path/to/gmdw && wf init` from an
  additional-working-directory cwd, or a user-typed `!` command, which
  bypasses hooks entirely) can be **falsely refused**. Fail-safe direction, and
  self-healing (any in-repo tool call refreshes the beacon), but the refusal
  message diagnoses only the "wrong project root" cause and may mislead in
  this case.
- **Clock behavior.** Backwards clock steps within the window are tolerated by
  the (to-be-bounded) skew clamp; `touch()` failures degrade to refusal, never
  to false liveness. Correct failure direction throughout — except F-1.

## 5. Test assessment

**Adequate and honest for the happy/refusal paths.** The six new tests drive
the real hook binary and the real `wf` binary end-to-end (no mocks), including
the L3 self-attestation round trip and L5 bypass audit — this is the right
level for governance code. The heartbeat was also added to the existing
protected-paths test (Write variant, ACTIVE workflow).

**Gaps (F-6):**

| Gap | Priority |
|---|---|
| Future-dated heartbeat must be treated as not-live | **Required with the F-1 fix** |
| Corrupt/non-JSON heartbeat → refusal (verified manually in this review; branch untested in the suite) | Recommended |
| Bash-gate protection of the heartbeat while ACTIVE (only the Write gate variant is tested) | Recommended |
| PostToolUse and SubagentStop `touch()` coverage (only PreToolUse is exercised as the writer) | Nice-to-have — the calls are identical one-liners, but they are three separate edit sites that can drift |
| Bypass strictness (`=0` should not bypass) — pairs with F-4 if fixed | Nice-to-have |

No missing *regression* tests for pre-existing behavior: the 26 legacy tests
are untouched and still pin the acceptance pipeline, which is what Phase 2
needs.

## 6. Phase 2 readiness assessment

Phase 1 was implemented in the shape Phase 2 wants:

- **Acceptance extraction unimpeded.** `subagentstop_validate.py` changed by
  exactly two lines (import + `touch()`); the accept pipeline is byte-identical,
  so the planned pure-refactor extraction still has the full legacy suite as
  its oracle.
- **`heartbeat.status()` is directly reusable** for the transition-time check,
  and the Phase 2 plan already (correctly) flags that wall-clock freshness
  alone is too fragile mid-run and prescribes a ledger-event predicate — which
  also mitigates F-3 for the mid-run case.
- **State core untouched.** The liveness check lives in `cmd_init`, not
  `create_workflow`, preserving the CLI-as-policy / core-as-mechanism split the
  extraction depends on.
- **Minor debt (F-8, low):** the bypass variable is init-specific
  (`GMDW_ALLOW_UNGOVERNED_INIT`) while the Phase 2 plan names
  `GMDW_ALLOW_UNGOVERNED` for transitions — reconcile naming before two
  variables ossify. Also carry F-1's skew bound into whatever predicate
  Phase 2 adopts.

## 7. Findings

| ID | Severity | Finding | Recommendation |
|---|---|---|---|
| F-1 | **High** | `status()` clamps negative heartbeat age to 0, so a future-dated timestamp attests liveness **forever**, defeating the design's bounded trust window. Empirically confirmed. Plantable in advance from a governed, workflow-inactive session (see F-2) and trivially in any hookless session. | **Fix before merge.** Treat `ts > now + skew` (e.g. 60–120 s) as not-live with its own reason string ("heartbeat timestamp is in the future"). Add the regression test. One line plus one test. |
| F-2 | Medium | Heartbeat protected-path entries are inert while no workflow is ACTIVE — both Write and Bash gates allow mutation pre-init (empirically confirmed). Consistent with the documented inactive-freedom posture, but the file's integrity matters precisely pre-init, and the protection claim overstates enforcement. | Decide deliberately: either carve out an always-on deny for this single file (justifiable — it is machine-owned runtime state, like `.workflow/runs/`), or correct the claim in comments/docs and rely on the F-1 fix to bound the exposure to the freshness window. Not merge-blocking. |
| F-3 | Medium | Same-machine cross-session vouching: a fresh beacon from a concurrent/recently-idle governed session lets a hookless session pass `wf init` within the window. Stronger than the stale-heartbeat residual the ADR accepted, and currently undocumented. | Document in KNOWN_LIMITATIONS §B. Phase 2: record `session_id` (available in hook stdin) in the heartbeat for provenance display in refusals/warnings. |
| F-4 | Low | Bypass activates on any non-empty value: `GMDW_ALLOW_UNGOVERNED_INIT=0` bypasses. | Accept only `"1"`/`"true"`, or document the truthiness. |
| F-5 | Low | Governed sessions can be falsely refused when the initiating command's cwd is outside the repo (additional working directories, `cd`-into-repo one-liners, user-typed `!` commands that bypass hooks). Fail-safe, self-healing, but the refusal message diagnoses only the wrong-project-root cause. | Append a second hint to the refusal: "if hooks ARE registered, re-run from the repository root so the gate can attest." Cosmetic. |
| F-6 | Low | Test gaps: future-dated beacon (required with F-1), corrupt-JSON beacon, Bash-gate heartbeat deny while ACTIVE, PostToolUse/SubagentStop as heartbeat writers. | Add with the F-1 fix; others as time permits. |
| F-7 | Info | Init refusals leave no durable audit trace (no run dir exists to log into). | Acceptable for Phase 1. If wanted later: a repo-level ops log is a Phase 2+ decision, not urgent. |
| F-8 | Info | Bypass env-var naming will diverge from Phase 2's planned `GMDW_ALLOW_UNGOVERNED`. | Reconcile when Phase 2 lands. |

## 8. Final verdict

**REQUEST CHANGES — scoped to F-1 only** (the future-timestamp bound and its
regression test). Every other finding is advisory and must not block the merge.

Rationale for the strict verdict despite the small surface: GMDW's explicit
standard is that a misbehaving model is stopped by code. F-1 is a concrete,
demonstrated code path by which a misbehaving model converts the new
attestation from a 300-second-bounded control into a permanent skeleton key —
the exact class of silent, unbounded degradation ADR-001 was written to
eliminate. Because the fix is a single guard clause, the cost of holding the
merge is negligible and the precedent (liveness bounds are real bounds) is
worth setting.

With F-1 fixed and its test green: **APPROVE**, and Phase 2 should proceed per
its plan — Phase 1's shape (untouched state core, reusable `status()`, intact
legacy oracle) leaves the extraction path clean.

---

*Empirical probes run for this review (scratchpad, against the real modules):
(1) `status()` with `ts=2099-01-01` → `(True, "heartbeat 0s old")`;
(2) `status()` with corrupt JSON → not-live (fail-safe);
(3) PreToolUse gate with no ACTIVE workflow: Write to
`.workflow/.hooks_heartbeat` → allow; Bash `echo x > .workflow/.hooks_heartbeat`
→ allow. Suite re-run: 32/32 OK.*
