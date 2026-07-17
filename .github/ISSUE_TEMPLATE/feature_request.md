---
name: Feature request
about: Propose a change or addition to GMDW
title: "[feat] "
labels: enhancement
---

## Problem / motivation
What are you trying to do that GMDW doesn't support today?

## Proposed change
What you'd like to happen.

## Governance impact
GMDW's rule is **governance is enforced by code, never by prompts.** If this touches
enforcement (`state.py`, the hooks, schemas, policies, or the CLI guards):
- Which guarantee does it affect, and does it *strengthen* or risk *weakening* it?
- How would it be enforced by a hook/guard and proven by a test in
  `tests/test_governance.py`?

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for the non-negotiables.

## Alternatives considered
Anything you've ruled out, and why.
