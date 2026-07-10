# GMDW v0.1.0-mvp — Release Checklist

The gate for cutting the frozen MVP baseline. Every box must be checked (or an
exception recorded with sign-off) before tagging `v0.1.0-mvp`. This release is the
**MVP baseline** — code is frozen; live validation happens afterward on a separate
branch (see `GIT_STRATEGY.md`).

## 1. Final verification

- [ ] Working tree clean except the intended release files (`git status`).
- [ ] Python ≥ 3.9 available (`python3 --version`); Git available.
- [ ] `.workflow/bin/wf` is executable (`test -x .workflow/bin/wf`).
- [ ] No `__pycache__/`, `*.pyc`, `*.tmp`, or `.workflow/runs/*` artifacts staged
      (covered by `.gitignore`; `git status --ignored` to confirm).
- [ ] No secrets, tokens, or absolute machine paths in tracked files
      (`git grep -nE '(api[_-]?key|secret|token|BEGIN .*PRIVATE KEY)' -- . ':!docs' ':!*.md'`).

## 2. Files to verify present (the frozen surface)

- [ ] Runtime library — `.claude/hooks/gmdw/{__init__,state,policy,schemas,ledger}.py`
- [ ] Hooks — `.claude/hooks/{pretooluse_gate,posttooluse_ledger,subagentstop_validate}.py`
- [ ] CLI — `.workflow/bin/wf`
- [ ] Agents (6) — `.claude/agents/{repo-analyst,planner,plan-reviewer,implementor,security-reviewer,code-reviewer}.md`
- [ ] Orchestrator — `.claude/commands/gmdw.md`
- [ ] Schemas (9) — `.workflow/schemas/*.schema.json`
- [ ] Policies (3) — `.workflow/policies/{standard,strict,expedited}.json`
- [ ] Settings + config — `.claude/settings.json`, `.workflow/config.json`, `.workflow/index.json`
- [ ] Tests — `tests/test_governance.py`, `tests/README.md`
- [ ] Validation docs — `docs/{HANDOFF,KNOWN_LIMITATIONS,LIVE_VALIDATION_PLAN,SMOKE_TESTS,ADVERSARIAL_VALIDATION,PRODUCTION_READINESS,FIRST_LIVE_RUN_CHECKLIST}.md`
- [ ] Release docs — `RELEASE_CHECKLIST.md`, `RELEASE_NOTES_v0.1.0.md`, `CONTRIBUTING.md`, `GIT_STRATEGY.md`, `LIVE_VALIDATION_EXECUTION.md`
- [ ] Project docs — `CLAUDE.md`, `README.md`, `.gitignore`

Quick count check (expected: 5 gmdw lib + 3 hooks + 6 agents + 9 schemas + 3 policies):
- [ ] `ls .claude/hooks/gmdw/*.py | wc -l` → 5
- [ ] `ls .claude/agents/*.md | wc -l` → 6
- [ ] `ls .workflow/schemas/*.schema.json | wc -l` → 9
- [ ] `ls .workflow/policies/*.json | wc -l` → 3

## 3. Tests to execute (all must pass)

- [ ] Governance suite: `python3 tests/test_governance.py` → `OK` (23 tests).
- [ ] Syntax check every Python file:
      `for f in .claude/hooks/*.py .claude/hooks/gmdw/*.py .workflow/bin/wf; do
      python3 -c "import ast;ast.parse(open('$f').read())" && echo "ok $f"; done`
- [ ] Every schema is valid JSON:
      `for f in .workflow/schemas/*.json .workflow/policies/*.json; do
      python3 -c "import json;json.load(open('$f'))" && echo "ok $f"; done`
- [ ] Every policy profile passes floor validation:
      `python3 -c "import sys;sys.path.insert(0,'.claude/hooks');import json;from gmdw import policy;
      [print(n, policy.validate_profile(json.load(open(f'.workflow/policies/{n}.json'))) or 'OK')
      for n in ('standard','strict','expedited')]"`
- [ ] CLI smoke: `.workflow/bin/wf --help` exits 0; no `.workflow/ACTIVE` left behind.

## 4. Documentation checks

- [ ] `README.md` describes agents, state machine, hooks, and quick start accurately.
- [ ] `CLAUDE.md` invariants match the implemented code.
- [ ] `RELEASE_NOTES_v0.1.0.md` version = `0.1.0`, tag reference = `v0.1.0-mvp`.
- [ ] All internal doc cross-links resolve (no reference to a non-existent file).
- [ ] Known limitations in `docs/KNOWN_LIMITATIONS.md` reflect the current, unmodified
      runtime (no live-validation-only fixes leaked into the baseline).

## 5. Branch preparation

- [ ] Default branch is `main` (`git branch -m master main` if needed).
- [ ] All release files staged; `git status` shows nothing unintended.
- [ ] Commit message uses the baseline message from `GIT_STRATEGY.md`.
- [ ] `mvp-complete` branch will point at the baseline commit (frozen).

## 6. Tagging process

- [ ] Annotated tag `v0.1.0-mvp` created on the baseline commit
      (`git tag -a v0.1.0-mvp -m "GMDW MVP baseline — 23/23 governance tests"`).
- [ ] `git tag -v v0.1.0-mvp` (or `git show v0.1.0-mvp`) confirms the tag points at the
      baseline commit and carries the message.
- [ ] `live-validation` branch created from the tagged baseline.

## 7. Push (operator-run — requires the origin URL)

- [ ] `origin` added (`git remote add origin <URL>`).
- [ ] `main`, `mvp-complete`, and the tag pushed; then `live-validation` pushed.
- [ ] Remote shows all three branches and the tag; CI (if any) is green.

**Release is cut** when §1–§6 are complete and §7 is executed by the operator with the
real remote. Do not proceed to live validation until the tag exists — it is the
rollback anchor.
