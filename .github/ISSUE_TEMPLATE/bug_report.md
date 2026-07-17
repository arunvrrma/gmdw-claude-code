---
name: Bug report
about: A governance guarantee, hook, guard, or CLI behaves incorrectly
title: "[bug] "
labels: bug
---

<!-- SECURITY: if this is a way to BYPASS a governance guarantee (escape a scope,
     skip a check, mutate state outside the CLI/hooks), do NOT file it here — report
     it privately via the Security tab. See SECURITY.md. -->

## What happened
A clear description of the incorrect behavior.

## Which component
- [ ] PreToolUse gate (`pretooluse_gate.py`)
- [ ] PostToolUse ledger (`posttooluse_ledger.py`)
- [ ] SubagentStop validator / `accept.py`
- [ ] `wf` CLI / `state.py` guards
- [ ] Schema / policy
- [ ] Docs
- [ ] Other:

## Reproduction
Steps, ideally as a failing case in the style of `tests/test_governance.py`.

## Expected behavior
What the guarantee/behavior should be.

## Environment
- GMDW version (`.workflow/bin/wf --version`):
- Python version (`python3 --version`):
- OS:

## Governance suite
Output of `python3 tests/test_governance.py` (does it pass on your checkout?).
