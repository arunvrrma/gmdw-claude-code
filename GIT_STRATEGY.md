# GMDW — Git & Branch Strategy

Three branches, one purpose each. The strategy keeps the frozen MVP baseline
permanently distinguishable from live-validation work, so a validation finding can
never silently mutate what was released.

## Branches

### `main`
- **Purpose:** the integration branch and the source of releases. Always in a
  known-good state (governance suite green). Release tags live here.
- **Receives:** merges from `live-validation` (after validation completes) and, later,
  feature branches.
- **Protection intent:** no direct commits for substantive change; changes arrive by
  reviewed merge. The initial baseline commit is the one exception (repository genesis).

### `mvp-complete`
- **Purpose:** an **immutable pointer** to the v0.1.0 MVP baseline — the exact tree that
  passed 23/23 offline before any live validation. It exists so "what did we actually
  ship as the MVP?" always has a precise, unmovable answer.
- **Receives:** nothing. It is frozen. Never commit to it, never merge into it.
- **Relationship to the tag:** `mvp-complete` and the tag `v0.1.0-mvp` point at the same
  commit. The branch is the human-readable handle; the tag is the durable anchor.

### `live-validation`
- **Purpose:** all live-run work — executing Workflows A/B/C in Claude Code, recording
  results, and folding runtime findings **into the documentation** (metadata shapes,
  hook order, model-policy confirmation). Per the freeze, this branch does **not** change
  runtime code, schemas, hooks, or agents; it adds validation evidence and doc updates.
- **Branches from:** the tagged baseline (`v0.1.0-mvp` / `mvp-complete`).
- **Merges to:** `main`, once validation is complete and the results are recorded.

```
            v0.1.0-mvp (tag)
                 │
  baseline ●─────�┴───── mvp-complete   (frozen pointer)
           │
           ├──────────── main            (integration; future releases tagged here)
           │
           └──────────── live-validation (live-run evidence + doc updates)
                                │
                                └── (merge back to main after validation)
```

## Merge strategy

- **`live-validation` → `main`:** a **`--no-ff` merge** (preserve the validation history
  as a distinct merge commit) once V1–V8 pass or deviations are accepted-mode and
  recorded. The merge commit message summarizes the validation outcome and links the
  evidence bundle.
- **Never merge anything into `mvp-complete`.** If you catch yourself wanting to, you
  actually want a new tag on `main` (a new release), not a change to the baseline.
- **Future features → `main`:** short-lived feature branches off `main`, squash- or
  no-ff-merged after review + green suite. Out of scope for v0.1.0.

## Tagging strategy

- **`v0.1.0-mvp`** — annotated tag on the baseline commit. The rollback anchor for the
  entire live-validation phase; if a live run corrupts a sandbox or you need the pristine
  MVP, `git checkout v0.1.0-mvp`.
- **Format:** `vMAJOR.MINOR.PATCH[-qualifier]`. The `-mvp` qualifier marks this as the
  pre-live-validation baseline. The next release after successful validation is
  `v0.2.0` (no `-mvp`), tagged on `main`.
- **Annotated, not lightweight** (`git tag -a`), so the tag carries a message and author
  and is a first-class object (`git show`, `git tag -v`).
- **Push tags explicitly** (`git push origin v0.1.0-mvp`) — tags are not pushed by a
  plain `git push`.

## Exact git commands

> Run from the repo root. These do the **local** preparation (reversible). The
> `origin`/push steps require your real remote URL and are outward-facing — run them
> last, deliberately.

### 0. Normalize the default branch (fresh `git init` created `master`)
```bash
git branch -m master main
```

### 1. Stage everything and make the baseline commit on `main`
```bash
git add -A
git status                       # confirm nothing unintended (no runs/, no __pycache__)
git commit -m "GMDW MVP baseline: runtime, hooks, agents, schemas, policies, CLI, 23/23 governance tests, validation + release docs"
```

### 2. Create the frozen baseline branch (points at the commit you just made)
```bash
git branch mvp-complete
```
> Note: your requested sequence was `git checkout -b mvp-complete` *before* the commit.
> On a fresh repo with no commits that would leave `main` unborn. Committing on `main`
> first, then branching `mvp-complete`, yields all branches at the same baseline commit —
> the intended result. If you prefer the literal order, run
> `git checkout -b mvp-complete` first, commit, then `git branch main` — the tree is
> identical.

### 3. Create the annotated tag on the baseline
```bash
git tag -a v0.1.0-mvp -m "GMDW MVP baseline — 23/23 governance tests, pre-live-validation"
git show v0.1.0-mvp | head -20         # verify the tag points at the baseline commit
```

### 4. Add origin (operator supplies the URL)
```bash
git remote add origin <YOUR_REMOTE_URL>      # e.g. git@github.com:you/gmdw-claude-code.git
git remote -v                                 # confirm
```

### 5. Push `main`, `mvp-complete`, and the tag
```bash
git push -u origin main
git push origin mvp-complete
git push origin v0.1.0-mvp
```

### 6. Create the live-validation branch from the tagged baseline
```bash
git checkout -b live-validation v0.1.0-mvp
```

### 7. Push the live-validation branch
```bash
git push -u origin live-validation
```

After this, `git branch -a` shows `main`, `mvp-complete`, `live-validation` and the tag
`v0.1.0-mvp`; live validation proceeds on `live-validation` per
`LIVE_VALIDATION_EXECUTION.md`.
