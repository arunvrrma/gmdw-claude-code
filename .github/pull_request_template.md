<!-- Read CONTRIBUTING.md before opening. GMDW's value is that guarantees hold even
     when a model misbehaves, so enforcement changes get extra scrutiny. -->

## What & why
Describe the change and the motivation. Link any issue.

## Governance impact
- [ ] This change does **not** touch enforcement (docs/tests/tooling only).
- [ ] This change touches enforcement (`state.py`, hooks, `accept.py`, schemas,
      policies, or the `wf` CLI guards). If checked, complete the box below.

<!-- If enforcement is touched: -->
- **Guarantee(s) affected:**
- **Strengthens or weakens?** (a change that weakens an enforcement path is a
  regression even if tests pass — see CONTRIBUTING.md "non-negotiables")
- **Fail-closed preserved?** (no `except: pass` turning a failure into a silent allow)

## Tests
- [ ] `python3 tests/test_governance.py` passes locally (state the test count).
- [ ] New enforcement behavior has both a **positive** (legal action allowed) and a
      **negative** (violation denied/blocked) test.
- [ ] No existing governance test was deleted or weakened to make this pass.

## Docs
- [ ] Updated `README.md` / `CLAUDE.md` / `docs/*` if behavior or invariants changed.
- [ ] Updated `CHANGELOG.md`.

## Checklist
- [ ] Standard library only (no new third-party imports).
- [ ] No secrets, tokens, or absolute machine paths in tracked files.
- [ ] No `.workflow/runs/` bundles committed (they are git-ignored).
