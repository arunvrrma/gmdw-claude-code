# GMDW — Release Readiness (v0.2.0, general use)

- **Date:** 2026-07-17
- **Scope:** readiness of the `release-v0.2.0` line for a general-use (public) release.
- **Supersedes** (for v0.2.0) the MVP-era [`PRODUCTION_READINESS.md`](PRODUCTION_READINESS.md).

## Verdict

**Ready for general use, pending one operator gate: a pre-public git-history scrub.**
The governance substance is GA-grade and materially stronger than the MVP; the
release-hygiene blockers are resolved; and the current code has been validated
end-to-end by a real governed run to `COMPLETED`. The only remaining item is
infrastructure the maintainer owns (see §5).

## 1. Dimension re-score (vs the MVP)

| Dimension | MVP | v0.2.0 | Basis |
|---|---|---|---|
| Operational | 3/5 | **4/5** | Full CLI lifecycle exercised live to COMPLETED (`wf_20260717_001`); `wf doctor` gives a read-only preflight. Still single-active-workflow (by design). |
| Governance | 4/5 | **5/5** | The MVP's one flagged fail-open — a session where hooks silently don't fire — is closed at init *and* mid-run, with `wf doctor` to check. 51/51 tests. |
| Security | 3/5 | **4/5** | Recovery commands confined by the gate (SF1, found+fixed by the workflow's own review); future-skew heartbeat bound. Bash gate remains pattern-based (accepted). |
| Audit | 4/5 | **4/5** | Unchanged: complete, append-only, hash-anchored per-run trail. |
| Recovery | 3/5 | **4/5** | Orphaned-invocation and stranded-artifact recovery are now in-governance (`wf clear-invocation`, `wf accept`) instead of manual state edits. |

## 2. Evidence of readiness

- **Tests:** `python3 tests/test_governance.py` → **51 passing**, driving the real
  hooks and the real `wf` CLI (26 at MVP → 51). Every governance guarantee is proven
  against a misbehaving model.
- **Live end-to-end validation on this code:** run **`wf_20260717_001`** (trivial
  route) went `RECEIVED → CLASSIFYING → IMPLEMENTING → CODE_REVIEW → APPROVED →
  COMPLETED` and closed with the finalize tamper-check passing. Critically it
  exercised the Phase-2 **acceptance extraction (`gmdw/accept.py`)** with a real
  implementor: the manifest was accepted only after the validator **re-ran the suite**
  (`declared: pass, observed: pass`), then a real code review approved it. This is the
  integration proof the unit suite alone cannot give.
- **Security:** a genuine privilege-escalation in the new recovery commands (SF1) was
  caught and fixed by the workflow's own plan security review before any code shipped.
- **Disclosure:** the tracked tree was scrubbed of a private third-party repo's
  content and of local absolute paths (`git grep` verified clean); run directories are
  git-ignored by default going forward.

## 3. Release-hygiene status

| Item | Status |
|---|---|
| Private-repo content removed from tracked tree | ✅ done |
| Local `/home` absolute paths removed | ✅ done (verified) |
| Broken internal doc links | ✅ fixed |
| Version consistent (`__version__` = tag = `0.2.0`) | ✅ done |
| `CHANGELOG.md` + `RELEASE_NOTES_v0.2.0.md` | ✅ added |
| Docs current (README, GETTING_STARTED, KNOWN_LIMITATIONS, tests/README) | ✅ refreshed |
| MVP process reports archived (`docs/history/`) | ✅ done |
| Working tree clean of unintended files | ✅ (run dirs git-ignored) |
| Live-validation run to COMPLETED on current code | ✅ `wf_20260717_001` |

## 4. Known limitations (unchanged in spirit; fail-closed)

Tracked honestly in [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md): single-active-workflow;
the Bash gate is pattern-based (protected *files* stay safe at the Write layer); the
token budget is an estimate; a few recovery conveniences (`wf extend-deadline`, budget
adjustment) are not yet in-governance. None is a governance hole.

## 5. Remaining gate — before the repo is made public

The private-repo content was removed from the tracked tree, **but those blobs remain
in git *history*.** The repository is currently private, so nothing is exposed yet.
Before flipping it to public, purge the history (e.g. `git filter-repo` to drop the
removed run-bundle and review-directory paths, then re-tag and force-push) **or**
publish via a fresh import from the clean tree. This is a deliberate maintainer
operation, not done as part of this release prep.

## 6. Recommendation

Tag **v0.2.0** on the private repo now (the release is complete and validated); do the
§5 history scrub as the final step immediately before making the repository public.
