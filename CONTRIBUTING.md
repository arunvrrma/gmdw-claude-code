# Contributing to gmdw-claude-code

GMDW is a **governance framework**: its value is that guarantees hold even when a model
misbehaves. That makes contribution standards stricter than a typical repo — a change
that weakens an enforcement path is a regression even if all tests pass. Read this
before touching anything under `.claude/` or `.workflow/`.

## Golden rule

**Governance is enforced by code, never by prompts.** Any behavior a contributor wants
to guarantee must be enforced by a hook or a guard and covered by a test in
`tests/test_governance.py`. Strengthening an agent's system prompt is never a
substitute for an enforcement check.

## Development workflow

1. Branch off `main` for your change.
2. Make the change. If it touches enforcement (`state.py`, `policy.py`, the hooks, the
   schemas, or the CLI guards), add or update a governance test in the same PR.
3. Run the full verification (see Testing) — it must pass locally.
4. Update the relevant doc (`README.md`, `CLAUDE.md`, or `docs/*`) in the same PR if
   behavior or invariants change.
5. Open a PR against the base branch. PRs that change enforcement require a second
   reviewer and an explicit note on which guarantee is affected.

## Branch strategy

- `main` — the integration branch; releases are tagged here (`vMAJOR.MINOR.PATCH`).
- Work on short-lived feature branches off `main` and open a PR back into it.

## Code standards

- **Python: standard library only.** No third-party dependencies, ever — the hooks run
  in the Claude Code environment with nothing installed. A PR that adds an `import` of a
  non-stdlib package will be rejected.
- Target Python ≥ 3.9. Keep the `from __future__ import annotations` header where
  present so newer typing syntax stays 3.9-safe.
- Atomic writes for any state mutation (tmp file + `os.replace`), as in `state.py` and
  `ledger.py`. Never write `state.json` in place.
- Hooks must **fail closed**: any internal error while a workflow is ACTIVE must deny
  (PreToolUse) or set an integrity hold (SubagentStop). Do not add an `except: pass`
  that turns a failure into a silent allow.
- Deterministic serialization where it affects hashing/registry (sorted keys where the
  existing code sorts).
- Match the existing style: no reformatting churn, no renaming for taste.

## Governance requirements (the non-negotiables)

A change may **not**, without an accompanying architecture decision and sign-off:

- Give any subagent the `Agent` tool, or otherwise let an agent invoke an agent.
- Add a write path that lets an agent write outside its per-state scope.
- Make `state.json` writable by anything other than the CLI / runtime library.
- Remove or weaken a counter/budget/deadline check, or make a limit bypassable.
- Loosen a policy floor (deployment gate must stay `blocking`; security triggers may be
  tightened, never loosened — `policy.validate_profile` enforces this).
- Make artifact acceptance advance a version pointer without schema + semantic validation.
- Turn a fail-closed path into fail-open.

Schemas, hooks, and agent definitions are part of the enforcement surface — treat edits
to them with the same scrutiny as runtime code.

## Testing requirements

Before any PR:

```bash
python3 tests/test_governance.py            # must print OK (51 tests)
for f in .claude/hooks/*.py .claude/hooks/gmdw/*.py .workflow/bin/wf; do \
  python3 -c "import ast;ast.parse(open('$f').read())" || echo "SYNTAX: $f"; done
for f in .workflow/schemas/*.json .workflow/policies/*.json; do \
  python3 -c "import json;json.load(open('$f'))" || echo "JSON: $f"; done
```

- Any new enforcement behavior needs a **positive** test (legal action allowed) and a
  **negative** test (violation denied/blocked) — the suite's pattern.
- Do not delete or weaken an existing governance test to make a change pass. If a test
  is genuinely wrong, fix it in a separate, clearly-justified commit.
- New adversarial scenarios go in `tests/test_governance.py` and are documented in
  `tests/README.md` and `docs/ADVERSARIAL_VALIDATION.md`.

## What not to do in this repo

- Do not hand-edit anything under `.workflow/runs/` — it is machine-owned.
- Do not commit `__pycache__/`, `*.pyc`, `*.tmp`, or run directories (`.gitignore`
  covers them).
- Do not add secrets, tokens, or absolute machine paths to tracked files.
- Do not commit while a workflow is ACTIVE (finish or close it first).

## Commit messages

Imperative subject; body explains *why* and which guarantee is affected if enforcement
changes. Reference the governance test that proves the change where applicable.

## License of contributions

By contributing to GMDW you agree that your contributions are licensed under the
project's [Apache License 2.0](LICENSE) (inbound = outbound), and you confirm you
have the right to submit the work under that license.
