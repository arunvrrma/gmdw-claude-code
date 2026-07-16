# GMDW v0.2.0 — Release Notes

**First general-use release.** GMDW runs a controlled, state-machine-driven software
delivery workflow with specialized Claude Code subagents, where every guardrail —
isolation, loop/budget limits, write-scope, artifact integrity, human gates — is
enforced by deterministic Python hooks and the `wf` CLI, not by asking the model to
behave.

New here? Start with **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**.

## Why this release

The MVP (`v0.1.0-mvp`) was complete and offline-verified, but it carried one
fail-open risk its own readiness review flagged as "the one exception to watch": a
session where the governance hooks silently don't fire (wrong project root) would run
*ungoverned*. v0.2.0 closes that, and pays down the acceptance-authority and recovery
gaps behind it.

## Highlights

- **You can no longer run ungoverned by accident.** Hooks attest liveness; `wf init`
  refuses to start, and `wf transition` refuses to advance, when the enforcement plane
  isn't actually firing. `wf doctor` tells you the state up front, read-only.
- **Acceptance authority moved into the core.** The artifact-acceptance pipeline now
  lives in `gmdw/accept.py`, shared verbatim by the SubagentStop hook and the new
  human recovery path — the hook is a thin adapter.
- **In-governance recovery.** `wf accept` (a stranded artifact) and
  `wf clear-invocation` (an orphaned invocation lock) replace the old manual
  `state.json` edits — human-attributed, audited, and confined to hooks-dark sessions.
- **A real onboarding path.** `docs/GETTING_STARTED.md`, including how to get the
  project root right in VS Code / IDEs.

A privilege-escalation risk in the two new recovery commands (SF1) was caught and
fixed **by the workflow's own security review** before any code shipped.

## Compatibility / upgrade

- No artifact-schema changes; no state-shape changes. Existing run directories are
  unaffected.
- New env var `GMDW_ALLOW_UNGOVERNED` (affirmative values only) is the canonical
  liveness bypass for CI / manual ops; `GMDW_ALLOW_UNGOVERNED_INIT` still works.
- Run directories under `.workflow/runs/` are now git-ignored by default.

## Validation status

- **50/50** governance tests pass (`python3 tests/test_governance.py`), driving the
  real hooks and the real `wf` CLI (26 → 50 since the MVP).
- The MVP was validated with live end-to-end runs; a fresh completing run on this
  release's code is recorded in the release-readiness assessment
  (`docs/RELEASE_READINESS.md`).

## Known limitations

Unchanged in spirit from the MVP and honestly tracked in
**[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)**: single-active-workflow, the
Bash gate is pattern-based (protected *files* remain safe at the Write layer), the
token budget is an estimate, and a few recovery conveniences (`wf extend-deadline`,
budget adjustment) are not yet in-governance. None is a governance hole; the failure
mode remains fail-closed.

## Requirements

Python 3.9+ (standard library only) and Git. No third-party dependencies.
