"""GMDW structured-Markdown artifacts: parsing and validation.

Replaces the JSON-Schema artifact contracts (v0.2.x) with **structured
Markdown**: a small `---` front-matter envelope followed by `## ` sections whose
bodies are bulleted lists, Markdown tables, or fenced blocks. Agents write prose
they would write anyway; this module extracts the machine-readable parts.

Why not JSON
------------
Forcing a model to emit a deep JSON document costs output tokens on syntax
(quoting, escaping, brace bookkeeping) and turns one bad character into a whole-
artifact regeneration. Markdown sections are *independently* parseable, so a
validation failure names one missing section or one malformed table row and the
deterministic orchestrator can ask for exactly that much again.

Shape of the API
----------------
``parse_document`` turns raw text into a :class:`Document` (envelope + sections).
The ``parse_*`` functions project a Document onto the same normalized dicts the
v0.2 JSON artifacts used, so the governance layer (guards, write-scoping,
semantic cross-checks) is unchanged by the format switch. The ``validate_*``
functions return a list of short, actionable error strings — never a stack trace
and never the whole artifact echoed back.

Everything here is pure and stdlib-only: no I/O, no model calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Vocabularies (carried over verbatim from the v0.2 JSON schemas)
# --------------------------------------------------------------------------- #

ARTIFACT_TYPES = ("classification", "repo_analysis", "plan", "review",
                  "implementation_manifest", "approval")
PRODUCERS = ("orchestrator", "analyst-planner", "implementor", "reviewer", "human")
STATUSES = ("complete", "blocked", "error")

ROUTES = ("trivial", "medium", "full")
FLAGS = ("security", "data_model")
SCORE_DIMENSIONS = ("files_impacted", "architectural", "security", "data_model",
                    "integrations", "testing")

FILE_ACTIONS = ("create", "modify", "delete")
CHANGE_ACTIONS = ("created", "modified", "deleted")
VERDICTS = ("approve", "revise", "reject")
SEVERITIES = ("blocker", "major", "minor", "info")
CATEGORIES = ("correctness", "security", "maintainability", "scope", "testing",
              "performance", "feasibility", "completeness", "dependencies")
AC_METHODS = ("unit_test", "integration_test", "manual", "static_check")
AC_STATUSES = ("pass", "fail", "not_verifiable")
TEST_RESULTS = ("pass", "fail", "not_run")
SECURITY_AREAS = ("authn_authz", "input_handling", "secrets", "web_flows",
                  "data_exposure", "dependencies", "crypto", "other")

# Bullet-field separators. The em dash is the documented form; ` :: ` is an ASCII
# fallback so a terminal or editor that mangles em dashes cannot cost a retry.
_SEP_RE = re.compile(r"\s+—\s+|\s+::\s+")

_FRONT_MATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
                              re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*\s*$")
_FENCE_RE = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]*)[ \t]*$")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _/-]*?)[ \t]*:[ \t]*(.*)$")


def _norm_key(text: str) -> str:
    """Column/field name -> snake_case identifier ('Verification Method' ->
    'verification_method'). Keeps table headers human-writable."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.strip().lower())).strip("_")


def _clean(text: str) -> str:
    """Strip the decorations a model reaches for: bold/italic markers, inline
    code backticks, and surrounding whitespace. Values are compared against
    enums, so `modify` and **modify** must mean the same thing.

    Only a value that is *entirely* one span is unwrapped. Stripping the outer
    characters unconditionally corrupts ordinary prose that happens to end in
    code: "wire it into `main()`" would lose its closing backtick, which is
    exactly what the first live run produced in a plan briefing.
    """
    t = text.strip()
    m = re.fullmatch(r"\*{1,3}(.+?)\*{1,3}", t)
    if m and "**" not in m.group(1):
        t = m.group(1).strip()
    m = re.fullmatch(r"`([^`]*)`", t)
    if m:
        t = m.group(1).strip()
    return t.strip()


def _split_lines(text: str) -> list:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


# --------------------------------------------------------------------------- #
# Document model
# --------------------------------------------------------------------------- #

@dataclass
class Section:
    """One ``## Heading`` and the raw text beneath it (up to the next heading of
    the same or higher level). Subsections are retained for nested structures."""
    title: str
    level: int
    body: str = ""
    subsections: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return _norm_key(self.title)


@dataclass
class Document:
    envelope: dict = field(default_factory=dict)
    sections: dict = field(default_factory=dict)   # normalized title -> Section
    raw: str = ""

    def section(self, *names):
        """First present section among ``names`` (normalized), else None."""
        for n in names:
            s = self.sections.get(_norm_key(n))
            if s is not None:
                return s
        return None

    def has(self, *names) -> bool:
        return self.section(*names) is not None

    def body(self, *names) -> str:
        s = self.section(*names)
        return s.body if s else ""


def _mask_fenced(lines: list) -> list:
    """Return a parallel list marking which lines are inside a fenced block.

    Headings, bullets and table pipes inside a ``` fence are content, not
    structure — an artifact that documents Markdown must not be re-parsed by its
    own examples.
    """
    inside, fence, indent = [], None, ""
    for line in lines:
        m = _FENCE_RE.match(line)
        if fence is None and m:
            fence, indent = m.group(2)[0] * 3, m.group(1)
            inside.append(True)
            continue
        if fence is not None:
            closing = _FENCE_RE.match(line)
            inside.append(True)
            if closing and closing.group(2).startswith(fence) and \
                    not closing.group(3) and len(closing.group(1)) <= len(indent) + 3:
                fence = None
            continue
        inside.append(False)
    return inside


def parse_front_matter(text: str) -> tuple:
    """Split leading ``---`` front matter from the body. Returns (envelope, body).

    The envelope is deliberately flat: ``key: value`` scalars plus list values
    written as indented ``- item`` lines. Nothing here needs nesting, and a flat
    grammar cannot be got subtly wrong.
    """
    m = _FRONT_MATTER_RE.match(text or "")
    if not m:
        return {}, text or ""
    env, key = {}, None
    for line in _split_lines(m.group(1)):
        if not line.strip():
            continue
        b = _BULLET_RE.match(line)
        if b and key:
            env.setdefault(key, [])
            if isinstance(env[key], list):
                env[key].append(_clean(b.group(2)))
            continue
        kv = _KV_RE.match(line.strip())
        if kv:
            key = _norm_key(kv.group(1))
            value = _clean(kv.group(2))
            env[key] = value if value else []
    return env, text[m.end():]


def parse_document(text: str) -> Document:
    """Parse an artifact into envelope + sections. Never raises on bad input:
    a malformed document simply yields missing sections, which the validators
    report as named, individually fixable errors."""
    envelope, body = parse_front_matter(text or "")
    doc = Document(envelope=envelope, raw=text or "")

    lines = _split_lines(body)
    fenced = _mask_fenced(lines)
    stack = []          # [(level, Section)]
    buf, current = [], None

    def flush():
        if current is not None:
            current.body = "\n".join(buf).strip("\n")

    for line, in_fence in zip(lines, fenced):
        m = None if in_fence else _HEADING_RE.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        level, title = len(m.group(1)), _clean(m.group(2))
        section = Section(title=title, level=level)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].subsections[section.key] = section
        # Every section is addressable from the top for a flat lookup, and the
        # first one at a given name wins (a duplicate heading is a defect the
        # validator reports rather than silently overwriting).
        doc.sections.setdefault(section.key, section)
        stack.append((level, section))
        buf, current = [], section
    flush()
    return doc


# --------------------------------------------------------------------------- #
# Block extractors
# --------------------------------------------------------------------------- #

def bullets(body: str) -> list:
    """Top-level bullet items, each with its own indented sub-bullets stripped
    off. Returns a list of the item texts (cleaned)."""
    out = []
    for text, _subs in nested_bullets(body):
        out.append(text)
    return out


def nested_bullets(body: str) -> list:
    """Top-level bullets paired with their nested ``key: value`` sub-bullets.

    ``- **M1** — do the thing`` with indented ``- files: a.py, b.py`` yields
    ``("**M1** — do the thing", {"files": "a.py, b.py"})``. Sub-bullets without a
    colon are collected under the ``_items`` key so nothing is silently dropped.
    """
    lines = _split_lines(body)
    fenced = _mask_fenced(lines)
    items, base = [], None
    # Where an indented continuation line belongs: the current bullet's own
    # text, a named sub-field, or the last unnamed sub-item.
    target = None
    for line, in_fence in zip(lines, fenced):
        if in_fence:
            continue
        m = _BULLET_RE.match(line)
        if m:
            indent = len(m.group(1).expandtabs(4))
            text = m.group(2).strip()
            if not text:
                continue
            if base is None or indent <= base:
                base = indent
                items.append([text, {}])
                target = ("text", None)
                continue
            if not items:
                continue
            kv = _KV_RE.match(text)
            if kv:
                key = _norm_key(kv.group(1))
                items[-1][1][key] = _clean(kv.group(2))
                target = ("field", key)
            else:
                items[-1][1].setdefault("_items", []).append(_clean(text))
                target = ("item", None)
            continue

        # Not a bullet. An INDENTED non-empty line continues whatever bullet we
        # are inside — a wrapped sentence is one sentence, and dropping its tail
        # silently truncated a plan's rationale in the first live run. An
        # unindented line ends the list; a blank line does not (loose lists).
        stripped = line.strip()
        if not stripped:
            continue
        if not line[:1].isspace() or not items or target is None:
            base, target = None if not line[:1].isspace() else base, None
            continue
        if stripped.startswith(("|", "#", "```", "~~~")):
            target = None
            continue
        if target[0] == "text":
            items[-1][0] = f"{items[-1][0]} {stripped}"
        elif target[0] == "field":
            key = target[1]
            items[-1][1][key] = _clean(
                f"{items[-1][1].get(key, '')} {stripped}".strip())
        elif target[0] == "item":
            sub = items[-1][1]["_items"]
            sub[-1] = _clean(f"{sub[-1]} {stripped}")
    return [(t, s) for t, s in items]


def table(body: str) -> list:
    """Rows of the first Markdown table in ``body`` as dicts keyed by
    snake_cased column name. Non-table lines around it are ignored, so a table
    may be introduced by a sentence."""
    lines = _split_lines(body)
    fenced = _mask_fenced(lines)
    header, rows = None, []
    for line, in_fence in zip(lines, fenced):
        stripped = line.strip()
        if in_fence or "|" not in stripped:
            if header is not None and not stripped:
                continue
            if header is not None and "|" not in stripped and stripped:
                break  # the table ended and prose resumed
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = [_norm_key(c) for c in cells]
            continue
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c != ""):
            continue  # the |---|---| separator row
        row = {}
        for i, cell in enumerate(cells):
            if i < len(header) and header[i]:
                row[header[i]] = _clean(cell)
        if any(v for v in row.values()):
            rows.append(row)
    return rows


def fenced_blocks(body: str, lang: str | None = None) -> list:
    """Contents of fenced code blocks, optionally filtered by language tag.
    This is where JSON genuinely belongs: small, closed, tightly scoped data
    (a dependency list) that would be clumsy as prose."""
    out, lines = [], _split_lines(body)
    fence, tag, buf, indent = None, None, [], ""
    for line in lines:
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence, tag, buf, indent = m.group(2)[0] * 3, m.group(3).lower(), [], m.group(1)
            continue
        if m and m.group(2).startswith(fence) and not m.group(3):
            if lang is None or tag == lang.lower():
                out.append("\n".join(buf))
            fence, tag, buf = None, None, []
            continue
        buf.append(line[len(indent):] if line.startswith(indent) else line)
    return out


def key_values(body: str) -> dict:
    """``key: value`` lines in a section body (bullets or bare lines alike).
    Used for the short scalar sections — verdict, route, base commit."""
    out = {}
    lines = _split_lines(body)
    fenced = _mask_fenced(lines)
    for line, in_fence in zip(lines, fenced):
        if in_fence:
            continue
        text = line.strip()
        b = _BULLET_RE.match(line)
        if b:
            text = b.group(2).strip()
        m = _KV_RE.match(text)
        if m:
            out.setdefault(_norm_key(m.group(1)), _clean(m.group(2)))
    return out


def first_scalar(body: str) -> str:
    """The first non-empty, non-bullet line of a section — for sections whose
    entire content is one value (``## Verdict`` -> ``approve``)."""
    for line in _split_lines(body):
        text = line.strip()
        if not text or text.startswith(("|", ">", "#")):
            continue
        b = _BULLET_RE.match(line)
        if b:
            text = b.group(2).strip()
        kv = _KV_RE.match(text)
        if kv:
            return _clean(kv.group(2)) or _clean(kv.group(1))
        return _clean(text)
    return ""


def csv_list(value) -> list:
    """A comma- or semicolon-separated inline list. Accepts an already-split
    list unchanged so callers need not care which form the agent used."""
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v)]
    if not value:
        return []
    parts = re.split(r"[,;]", str(value))
    return [_clean(p) for p in parts if _clean(p)]


def _split_fields(text: str, count: int) -> list:
    """Split a bullet line into up to ``count`` em-dash/`::`-separated fields."""
    parts = _SEP_RE.split(text.strip(), maxsplit=count - 1)
    return [_clean(p) for p in parts]


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #

_WF_ID_RE = re.compile(r"^wf_[0-9]{8}_[0-9]{3}$")


def validate_envelope(env: dict, *, artifact_type: str, workflow_id: str,
                      version: int) -> list:
    """Check the front matter against what the orchestrator asked for. The
    envelope is the one place a mismatch means 'this is the wrong artifact',
    so it is checked before any section is looked at."""
    errs = []
    if not env:
        return ["missing '---' front matter (envelope) at the top of the file"]
    got_type = env.get("artifact_type")
    if got_type != artifact_type:
        errs.append(f"envelope artifact_type: expected '{artifact_type}', "
                    f"got {got_type!r}")
    wid = env.get("workflow_id")
    if wid != workflow_id:
        errs.append(f"envelope workflow_id: expected '{workflow_id}', got {wid!r}")
    elif not _WF_ID_RE.match(str(wid)):
        errs.append(f"envelope workflow_id {wid!r} is malformed")
    try:
        got_version = int(str(env.get("version", "")).strip())
    except (TypeError, ValueError):
        got_version = None
    if got_version != version:
        errs.append(f"envelope version: expected {version}, "
                    f"got {env.get('version')!r}")
    if env.get("produced_by") not in PRODUCERS:
        errs.append(f"envelope produced_by must be one of {list(PRODUCERS)}, "
                    f"got {env.get('produced_by')!r}")
    status = env.get("status")
    if status not in STATUSES:
        errs.append(f"envelope status must be one of {list(STATUSES)}, "
                    f"got {status!r}")
    if status == "blocked" and not env.get("blocked_reason"):
        errs.append("envelope status is 'blocked' but blocked_reason is empty")
    if not env.get("created_at"):
        errs.append("envelope created_at is required")
    return errs


def is_blocked(env: dict) -> bool:
    return (env or {}).get("status") == "blocked"


# --------------------------------------------------------------------------- #
# Section presence
# --------------------------------------------------------------------------- #

def require_sections(doc: Document, required) -> list:
    """One error per missing section, naming the exact heading to add. The point
    of the Markdown format is that this is the whole remediation instruction —
    the orchestrator can ask for one section, not a whole regenerated file."""
    return [f"missing required section '## {name}'"
            for name in required if not doc.has(name)]


def _enum_err(where: str, value, allowed) -> str:
    return f"{where}: {value!r} is not one of {list(allowed)}"


def _require_columns(rows: list, columns, where: str) -> list:
    errs = []
    for i, row in enumerate(rows, 1):
        for col in columns:
            if not row.get(col):
                errs.append(f"{where} row {i}: column '{col}' is missing or empty")
    return errs


# --------------------------------------------------------------------------- #
# classification_vN.md
# --------------------------------------------------------------------------- #

CLASSIFICATION_SECTIONS = ("Complexity", "Risk Level", "Governance Profile")


def parse_classification(doc: Document) -> dict:
    """`## Complexity` (score table) + `## Risk Level` (route/flags) +
    `## Governance Profile` (rationale). Projects onto the v0.2 classification
    shape so routing logic is untouched by the format change."""
    scores = {}
    for row in table(doc.body("Complexity")):
        dim = _norm_key(row.get("dimension") or row.get("axis") or "")
        raw = row.get("score", "")
        m = re.search(r"-?\d+", str(raw))
        if dim and m:
            scores[dim] = int(m.group(0))
    if not scores:
        scores = {k: int(v) for k, v in key_values(doc.body("Complexity")).items()
                  if re.fullmatch(r"-?\d+", str(v))}

    risk = key_values(doc.body("Risk Level"))
    flags = [f for f in csv_list(risk.get("flags") or risk.get("override_flags"))
             if f and f.lower() not in ("none", "-")]
    total = risk.get("total")
    if total is None:
        total = key_values(doc.body("Complexity")).get("total")
    try:
        total = int(re.search(r"-?\d+", str(total)).group(0))
    except (AttributeError, TypeError, ValueError):
        total = sum(v for k, v in scores.items() if k in SCORE_DIMENSIONS)

    return {
        "scores": scores,
        "total": total,
        "route": (risk.get("route") or "").lower(),
        "override_flags": [f.lower() for f in flags],
        "route_rationale": doc.body("Governance Profile").strip(),
        "evidence": bullets(doc.body("Evidence")),
    }


def validate_classification(doc: Document) -> list:
    errs = require_sections(doc, CLASSIFICATION_SECTIONS)
    if errs:
        return errs
    c = parse_classification(doc)
    missing = [d for d in SCORE_DIMENSIONS if d not in c["scores"]]
    if missing:
        errs.append(f"'## Complexity' table is missing dimensions: {missing} "
                    f"(one row per dimension, columns: dimension | score)")
    for dim, val in c["scores"].items():
        if dim in SCORE_DIMENSIONS and not 0 <= val <= 3:
            errs.append(f"complexity score '{dim}' = {val}; must be 0-3")
    if c["route"] not in ROUTES:
        errs.append(_enum_err("'## Risk Level' route", c["route"], ROUTES))
    bad = [f for f in c["override_flags"] if f not in FLAGS]
    if bad:
        errs.append(_enum_err("'## Risk Level' flags", bad, FLAGS))
    if not c["route_rationale"]:
        errs.append("'## Governance Profile' must explain the route choice")
    return errs


# --------------------------------------------------------------------------- #
# repo_analysis_vN.md
# --------------------------------------------------------------------------- #

REPO_ANALYSIS_SECTIONS = ("Summary", "File Tree", "Dependencies",
                          "Architecture Notes")


def parse_repo_analysis(doc: Document) -> dict:
    """`## Summary` / `## File Tree` / `## Dependencies` / `## Architecture
    Notes`, plus the optional `## Test Infrastructure` and `## Unverified`.

    Evidence paths are collected from the file tree and from any `path — note`
    bullet in the notes, because path-existence checking is a governance
    verification the profile can require.
    """
    tree = doc.body("File Tree")
    paths = []
    for block in fenced_blocks(tree):
        for line in _split_lines(block):
            token = _clean(line.split("#", 1)[0])
            if token and "/" in token or (token and "." in token):
                paths.append(token.lstrip("|`-+ ").rstrip("/"))
    for item in bullets(tree):
        token = _split_fields(item, 2)[0]
        if token:
            paths.append(token)

    deps = []
    for block in fenced_blocks(doc.body("Dependencies"), "json"):
        deps.append(block)
    dep_items = bullets(doc.body("Dependencies"))

    notes = []
    for item, _sub in nested_bullets(doc.body("Architecture Notes")):
        parts = _split_fields(item, 2)
        notes.append({"finding": parts[-1] if len(parts) > 1 else parts[0],
                      "evidence": parts[0] if len(parts) > 1 else ""})

    tests = key_values(doc.body("Test Infrastructure"))
    return {
        "summary": doc.body("Summary").strip(),
        "paths": [p for p in dict.fromkeys(paths) if p],
        "dependencies_json": deps,
        "dependencies": dep_items,
        "architecture_notes": notes,
        "test_command": tests.get("test_command", ""),
        "unverified": bullets(doc.body("Unverified")),
    }


def validate_repo_analysis(doc: Document) -> list:
    errs = require_sections(doc, REPO_ANALYSIS_SECTIONS)
    if errs:
        return errs
    ra = parse_repo_analysis(doc)
    if not ra["summary"]:
        errs.append("'## Summary' is empty")
    if not ra["paths"]:
        errs.append("'## File Tree' lists no files (give the paths the change "
                    "touches, one per line or as bullets)")
    return errs


# --------------------------------------------------------------------------- #
# plan_vN.md
# --------------------------------------------------------------------------- #

PLAN_SECTIONS = ("Objective", "Files In Scope", "Milestones",
                 "Acceptance Criteria", "Risk Mitigations")

_MILESTONE_ID_RE = re.compile(r"^\*{0,3}(M\d+)\*{0,3}\s*(?:[—:.\-]\s*)?(.*)$")
_AC_ID_RE = re.compile(r"^(AC\d+)$", re.IGNORECASE)


def parse_plan(doc: Document) -> dict:
    """The plan is the contract the Implementor is scoped to and the Reviewer
    judges against, so its parse is the one that must be exact: ``files`` drives
    the PreToolUse write allowlist."""
    files = []
    scope_body = doc.body("Files In Scope")
    rows = table(scope_body)
    if rows:
        for row in rows:
            path = row.get("path") or row.get("file") or ""
            if path:
                files.append({"path": path,
                              "action": (row.get("action") or "").lower(),
                              "reason": row.get("reason", "")})
    else:
        for item in bullets(scope_body):
            parts = _split_fields(item, 3)
            if not parts or not parts[0]:
                continue
            files.append({"path": parts[0],
                          "action": (parts[1].lower() if len(parts) > 1 else ""),
                          "reason": parts[2] if len(parts) > 2 else ""})

    milestones = []
    for text, sub in nested_bullets(doc.body("Milestones")):
        m = _MILESTONE_ID_RE.match(_clean(text))
        mid = m.group(1) if m else ""
        desc = (m.group(2) if m else _clean(text)).strip()
        milestones.append({
            "id": mid,
            "description": desc,
            "files": csv_list(sub.get("files", "")),
            "verification": sub.get("verification", ""),
        })

    criteria = []
    for row in table(doc.body("Acceptance Criteria")):
        criteria.append({
            "id": row.get("id", ""),
            "criterion": row.get("criterion") or row.get("description", ""),
            "verification_method": _norm_key(row.get("verification_method")
                                             or row.get("method", "")),
        })

    risks = []
    for row in table(doc.body("Risk Mitigations")):
        risks.append({
            "risk": row.get("risk", ""),
            "likelihood": (row.get("likelihood") or "").lower(),
            "impact": (row.get("impact") or "").lower(),
            "mitigation": row.get("mitigation", ""),
        })

    revision_notes = []
    for row in table(doc.body("Revision Notes")):
        fid = row.get("finding_id") or row.get("finding") or row.get("id", "")
        if fid:
            revision_notes.append({"finding_id": fid,
                                   "resolution": row.get("resolution", "")})

    deps = []
    for block in fenced_blocks(doc.body("Dependencies"), "json"):
        deps.append(block)

    return {
        "objective": doc.body("Objective").strip(),
        "files": files,
        "milestones": milestones,
        "acceptance_criteria": criteria,
        "risks": risks,
        "scope_out": bullets(doc.body("Scope Out", "Out Of Scope")),
        "assumptions": bullets(doc.body("Assumptions")),
        "dependencies_json": deps,
        "dependencies": bullets(doc.body("Dependencies")),
        "revision_notes": revision_notes,
    }


def validate_plan(doc: Document) -> list:
    errs = require_sections(doc, PLAN_SECTIONS)
    if errs:
        return errs
    p = parse_plan(doc)
    if not p["objective"]:
        errs.append("'## Objective' is empty")

    if not p["files"]:
        errs.append("'## Files In Scope' lists no files; the Implementor's write "
                    "scope comes from this list "
                    "(format: `- path/to/file.py — modify — why`)")
    for i, f in enumerate(p["files"], 1):
        if f["action"] not in FILE_ACTIONS:
            errs.append(f"'## Files In Scope' entry {i} ({f['path']}): action "
                        f"{f['action']!r} is not one of {list(FILE_ACTIONS)}")

    if not p["milestones"]:
        errs.append("'## Milestones' is empty (format: `- M1 — description` with "
                    "nested `- files:` and `- verification:` bullets)")
    for i, m in enumerate(p["milestones"], 1):
        if not m["id"]:
            errs.append(f"'## Milestones' entry {i} has no M<n> id "
                        f"(got {m['description'][:40]!r})")
        if not m["verification"]:
            errs.append(f"milestone {m['id'] or i} has no `- verification:` "
                        f"sub-bullet")

    if not p["acceptance_criteria"]:
        errs.append("'## Acceptance Criteria' has no table rows (columns: "
                    "id | criterion | verification method)")
    for i, ac in enumerate(p["acceptance_criteria"], 1):
        if not _AC_ID_RE.match(ac["id"] or ""):
            errs.append(f"acceptance criterion {i}: id {ac['id']!r} must be AC<n>")
        if not ac["criterion"]:
            errs.append(f"acceptance criterion {ac['id'] or i}: empty criterion")
        if ac["verification_method"] not in AC_METHODS:
            errs.append(_enum_err(f"acceptance criterion {ac['id'] or i} "
                                  f"verification method",
                                  ac["verification_method"], AC_METHODS))

    for i, r in enumerate(p["risks"], 1):
        if r["likelihood"] and r["likelihood"] not in ("low", "medium", "high"):
            errs.append(f"risk {i}: likelihood {r['likelihood']!r} must be "
                        f"low|medium|high")
        if r["impact"] and r["impact"] not in ("low", "medium", "high"):
            errs.append(f"risk {i}: impact {r['impact']!r} must be low|medium|high")
        if not r["mitigation"]:
            errs.append(f"risk {i}: column 'mitigation' is missing or empty")
    return errs


# --------------------------------------------------------------------------- #
# review_vN.md
# --------------------------------------------------------------------------- #

REVIEW_SECTIONS = ("Verdict", "Findings", "Summary")

_FINDING_ID_RE = re.compile(r"^F\d+$", re.IGNORECASE)


def parse_review(doc: Document) -> dict:
    """The unified (security + code quality) review. `## Findings` is a table;
    `## Verdict` is a single word plus a rationale line."""
    vbody = doc.body("Verdict")
    vkv = key_values(vbody)
    verdict = (vkv.get("verdict") or first_scalar(vbody) or "").lower()
    verdict = _clean(verdict).split()[0].strip(".:") if verdict else ""

    findings = []
    for row in table(doc.body("Findings")):
        findings.append({
            "id": row.get("id", ""),
            "severity": (row.get("severity") or "").lower(),
            "category": (row.get("category") or "").lower(),
            "description": row.get("description", ""),
            "location": row.get("file") or row.get("location") or row.get("path", ""),
            "recommendation": row.get("recommendation", ""),
            "resolves_prior_finding": row.get("resolves_prior_finding", ""),
        })

    criteria = []
    for row in table(doc.body("Acceptance Criteria")):
        criteria.append({
            "id": row.get("id", ""),
            "status": _norm_key(row.get("status", "")),
            "evidence": row.get("evidence", ""),
        })

    checked = []
    for row in table(doc.body("Security Checklist")):
        area = _norm_key(row.get("area") or row.get("check", ""))
        if area:
            checked.append({"area": area, "evidence": row.get("evidence", "")})
    if not checked:
        for item, _sub in nested_bullets(doc.body("Security Checklist")):
            parts = _split_fields(item, 2)
            area = _norm_key(parts[0])
            if area:
                checked.append({"area": area,
                                "evidence": parts[1] if len(parts) > 1 else ""})

    return {
        "verdict": verdict,
        "verdict_rationale": vkv.get("rationale", "") or vbody.strip(),
        "findings": findings,
        "acceptance_criteria": criteria,
        "checked": checked,
        "summary": doc.body("Summary").strip(),
        "subject_ref": (doc.envelope.get("subject_ref") or "").strip(),
    }


def validate_review(doc: Document, *, require_acceptance_criteria: bool = True,
                    require_security_checklist: bool = False) -> list:
    errs = require_sections(doc, REVIEW_SECTIONS)
    if errs:
        return errs
    r = parse_review(doc)
    if r["verdict"] not in VERDICTS:
        errs.append(_enum_err("'## Verdict'", r["verdict"], VERDICTS))
    if not r["summary"]:
        errs.append("'## Summary' is empty")

    seen = set()
    for i, f in enumerate(r["findings"], 1):
        where = f"finding {f['id'] or i}"
        if not _FINDING_ID_RE.match(f["id"] or ""):
            errs.append(f"'## Findings' row {i}: id {f['id']!r} must be F<n>")
        elif f["id"].upper() in seen:
            errs.append(f"'## Findings': duplicate finding id {f['id']}")
        else:
            seen.add(f["id"].upper())
        if f["severity"] not in SEVERITIES:
            errs.append(_enum_err(f"{where} severity", f["severity"], SEVERITIES))
        if f["category"] not in CATEGORIES:
            errs.append(_enum_err(f"{where} category", f["category"], CATEGORIES))
        for col in ("description", "recommendation"):
            if not f[col]:
                errs.append(f"{where}: column '{col}' is missing or empty")

    if require_acceptance_criteria:
        if not r["acceptance_criteria"]:
            errs.append("'## Acceptance Criteria' has no table rows; every plan "
                        "criterion needs a row (columns: id | status | evidence)")
        for i, ac in enumerate(r["acceptance_criteria"], 1):
            if ac["status"] not in AC_STATUSES:
                errs.append(_enum_err(f"acceptance criterion {ac['id'] or i} status",
                                      ac["status"], AC_STATUSES))
            if ac["status"] == "not_verifiable" and not ac["evidence"]:
                errs.append(f"acceptance criterion {ac['id'] or i}: "
                            f"'not_verifiable' requires evidence explaining why")

    if require_security_checklist:
        if not r["checked"]:
            errs.append("'## Security Checklist' is required on a security-flagged "
                        "workflow and must record the areas actually checked "
                        "(columns: area | evidence); an empty checklist is not "
                        "an approval")
        bad = [c["area"] for c in r["checked"] if c["area"] not in SECURITY_AREAS]
        if bad:
            errs.append(_enum_err("'## Security Checklist' area", bad,
                                  SECURITY_AREAS))
    return errs


# --------------------------------------------------------------------------- #
# implementation_vN/manifest.md
# --------------------------------------------------------------------------- #

MANIFEST_SECTIONS = ("Base Commit", "Changes", "Tests", "Milestone Status",
                     "Deviations")


def parse_manifest(doc: Document) -> dict:
    """The Implementor's record of what it actually did. ``tests.command`` is
    re-run by the acceptance pipeline and compared to ``tests.result``, so those
    two fields carry the system's strongest honesty guarantee."""
    changes = []
    for row in table(doc.body("Changes")):
        path = row.get("path") or row.get("file", "")
        if path:
            changes.append({"path": path,
                            "action": (row.get("action") or "").lower(),
                            "summary": row.get("summary", "")})

    tests = key_values(doc.body("Tests"))
    milestones = []
    for row in table(doc.body("Milestone Status")):
        mid = row.get("milestone_id") or row.get("id") or row.get("milestone", "")
        if mid:
            milestones.append({"milestone_id": mid,
                               "status": (row.get("status") or "").lower(),
                               "note": row.get("note", "")})

    deviations = []
    dev_body = doc.body("Deviations")
    for row in table(dev_body):
        what = row.get("what") or row.get("deviation", "")
        if what:
            deviations.append({
                "what": what, "why": row.get("why", ""),
                "requires_approval": _clean(str(row.get("requires_approval", "")))
                .lower() in ("yes", "true", "1")})
    if not deviations:
        for item, sub in nested_bullets(dev_body):
            what = _split_fields(item, 2)[0]
            if what and what.lower() not in ("none", "n/a", "-"):
                deviations.append({
                    "what": what, "why": sub.get("why", ""),
                    "requires_approval": str(sub.get("requires_approval", ""))
                    .lower() in ("yes", "true", "1")})

    addressed = []
    for row in table(doc.body("Findings Addressed")):
        fid = row.get("finding_id") or row.get("finding") or row.get("id", "")
        if fid:
            addressed.append({"finding_id": fid,
                              "resolution": row.get("resolution", "")})

    return {
        "base_commit": first_scalar(doc.body("Base Commit")),
        "plan_ref": (doc.envelope.get("plan_ref") or "").strip(),
        "findings_addressed": addressed,
        "changes": changes,
        "tests": {"command": tests.get("command", ""),
                  "result": (tests.get("result") or "").lower(),
                  "added": csv_list(tests.get("added", "")),
                  "output_ref": tests.get("output_ref", "")},
        "milestone_status": milestones,
        "deviations": deviations,
    }


def validate_manifest(doc: Document) -> list:
    errs = require_sections(doc, MANIFEST_SECTIONS)
    if errs:
        return errs
    m = parse_manifest(doc)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", m["base_commit"] or ""):
        errs.append(f"'## Base Commit' must be a git sha "
                    f"(got {m['base_commit']!r}); use `git rev-parse HEAD`")
    if not m["changes"]:
        errs.append("'## Changes' has no table rows (columns: "
                    "path | action | summary)")
    for i, c in enumerate(m["changes"], 1):
        if c["action"] not in CHANGE_ACTIONS:
            errs.append(_enum_err(f"'## Changes' row {i} ({c['path']}) action",
                                  c["action"], CHANGE_ACTIONS))
        if not c["summary"]:
            errs.append(f"'## Changes' row {i} ({c['path']}): summary is empty")
    if m["tests"]["result"] not in TEST_RESULTS:
        errs.append(_enum_err("'## Tests' result", m["tests"]["result"],
                              TEST_RESULTS))
    if m["tests"]["result"] in ("pass", "fail") and not m["tests"]["command"]:
        errs.append("'## Tests' declares a result but no `command:` — the "
                    "command is re-run to verify the result")
    for i, ms in enumerate(m["milestone_status"], 1):
        if ms["status"] not in ("done", "partial", "skipped"):
            errs.append(_enum_err(f"'## Milestone Status' row {i} status",
                                  ms["status"], ("done", "partial", "skipped")))
    for i, d in enumerate(m["deviations"], 1):
        if not d["why"]:
            errs.append(f"deviation {i} ({d['what'][:40]}): 'why' is required")
    return errs


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

# logical artifact type -> (envelope artifact_type, required sections)
ARTIFACT_SPEC = {
    "classification": ("classification", CLASSIFICATION_SECTIONS),
    "repo_analysis": ("repo_analysis", REPO_ANALYSIS_SECTIONS),
    "plan": ("plan", PLAN_SECTIONS),
    "review": ("review", REVIEW_SECTIONS),
    "implementation": ("implementation_manifest", MANIFEST_SECTIONS),
}

_PARSERS = {
    "classification": parse_classification,
    "repo_analysis": parse_repo_analysis,
    "plan": parse_plan,
    "review": parse_review,
    "implementation": parse_manifest,
}


def parse_artifact(text: str, atype: str) -> tuple:
    """(Document, parsed dict) for ``atype``. Raises KeyError on an unknown
    type — that is a programming error, not artifact input."""
    doc = parse_document(text)
    return doc, _PARSERS[atype](doc)


def validate_artifact(text: str, atype: str, *, workflow_id: str, version: int,
                      **options) -> list:
    """Envelope + section + field validation for one artifact. Returns short
    error strings; empty means valid. A ``blocked`` artifact is envelope-checked
    only — 'I could not do this, here is why' is a legitimate, complete answer
    and must not be rejected for missing the sections it is explaining away."""
    if atype not in ARTIFACT_SPEC:
        return [f"unknown artifact type: {atype}"]
    envelope_type, _ = ARTIFACT_SPEC[atype]
    doc = parse_document(text)
    errs = validate_envelope(doc.envelope, artifact_type=envelope_type,
                             workflow_id=workflow_id, version=version)
    if errs or is_blocked(doc.envelope):
        return errs
    if atype == "review":
        return validate_review(doc, **options)
    return {
        "classification": validate_classification,
        "repo_analysis": validate_repo_analysis,
        "plan": validate_plan,
        "implementation": validate_manifest,
    }[atype](doc)
