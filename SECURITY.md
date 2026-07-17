# Security Policy

GMDW is a governance framework whose entire purpose is that its guarantees hold
even when a model misbehaves. A defect that lets a workflow bypass a hook, escape a
write scope, exceed a budget, or advance state on an unvalidated artifact is a
**security** issue, not just a bug — please report it privately.

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| 0.1.x (`v0.1.0-mvp`) | ❌ (superseded) |

Fixes land on `main` and ship in the next tagged release.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it privately through GitHub:

1. Go to the repository's **Security** tab → **Report a vulnerability** (GitHub
   Private Vulnerability Reporting), or
2. contact the maintainer directly if you cannot use that channel.

Please include: the affected component (a hook, a guard, the `wf` CLI, a schema, or
a policy), the governance guarantee you believe is broken, and a minimal
reproduction — ideally expressed as a failing case in the style of
`tests/test_governance.py` (each guarantee there is proven against a misbehaving
model, so that is the natural form for a report).

You can expect an acknowledgement within a few days and an assessment of scope and
a fix plan thereafter. We will credit reporters who wish to be named once a fix is
released.

## What counts as in scope

- Any way to make a subagent invoke another agent, write outside its per-state
  scope, or mutate `state.json` / the ledger outside the CLI and hooks.
- Bypassing a counter, budget, deadline, single-flight, or liveness check.
- Advancing a version pointer without schema + semantic validation, or turning a
  fail-closed path into fail-open.
- Loosening a policy floor (e.g. the blocking deployment gate) at runtime.

## Known and accepted boundaries

Some limitations are documented and intentional — see
[`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) (e.g. the Bash gate is
pattern-based; protected *files* stay safe at the Write layer). Reports that
restate a documented, accepted boundary are welcome as hardening ideas but are not
treated as vulnerabilities.
