"""GMDW agent runner — the ``claude`` CLI adapter.

This is the ONLY module in GMDW that talks to a model. Everything upstream of it
(which stage runs next, what it may write, whether its output is acceptable) and
everything downstream (parsing, validation, state advance) is deterministic
Python. The boundary is deliberately this narrow: an LLM is a subroutine that
turns a scoped prompt into a file on disk, not the thing driving the workflow.

The child session is a full Claude Code session rooted in the repository, so the
project's ``.claude/settings.json`` loads and the three governance hooks fire
inside it. That is what keeps write-scoping, protected paths, and the push gate
enforced even though the orchestrator itself is now an ordinary Python process.

``Runner`` is an interface with two implementations: :class:`ClaudeRunner` (real)
and :class:`ScriptedRunner` (tests and ``--dry-run``), so the whole orchestrator
loop is exercisable without spending a token.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import string
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT_MIN = 30

# Tools each role may use inside its child session. This is a convenience
# narrowing, NOT the security boundary — the PreToolUse gate is, and it does not
# trust this list. Keeping it in sync with .claude/agents/*.md keeps the two
# descriptions of a role's capability from drifting apart.
TOOLS_FOR_ROLE = {
    "analyst-planner": ["Read", "Grep", "Glob", "Bash", "Write"],
    "implementor": ["Read", "Grep", "Glob", "Bash", "Write", "Edit"],
    "reviewer": ["Read", "Grep", "Glob", "Bash", "Write"],
}


@dataclass
class InvocationSpec:
    """Everything needed to run one agent, assembled deterministically."""
    role: str
    model: str | None
    prompt: str
    system_prompt: str = ""
    cwd: str = "."
    workflow_id: str = ""
    timeout_sec: int = DEFAULT_TIMEOUT_MIN * 60
    allowed_tools: list = field(default_factory=list)


@dataclass
class InvocationResult:
    """What came back. ``tokens`` and ``cost_usd`` feed the budget ledger;
    ``exact`` says whether the runtime reported usage or we estimated it."""
    ok: bool
    text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    exact: bool = False
    exit_code: int = 0
    error: str = ""
    raw: dict = field(default_factory=dict)


class Runner:
    """Interface: turn an InvocationSpec into an InvocationResult."""

    def invoke(self, spec: InvocationSpec) -> InvocationResult:  # pragma: no cover
        raise NotImplementedError


def _extract_usage(payload: dict) -> tuple:
    """(tokens, cost_usd, exact) from a `--output-format json` envelope."""
    usage = payload.get("usage")
    total = 0
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            try:
                total += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
    cost = payload.get("total_cost_usd") or payload.get("cost_usd") or 0.0
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        cost = 0.0
    return total, cost, bool(total)


class ClaudeRunner(Runner):
    """Shells out to the ``claude`` CLI in headless (``-p``) mode.

    The prompt goes in on stdin rather than argv: a plan-sized prompt plus the
    artifacts it references will outgrow ARG_MAX on some systems, and a run that
    dies on a long request is exactly the stall this rework exists to remove.
    """

    def __init__(self, binary: str | None = None, extra_args=None):
        self.binary = binary or os.environ.get("GMDW_CLAUDE_BIN") or "claude"
        # GMDW_CLAUDE_ARGS is the operator's seam into the child session — most
        # usefully `--permission-mode`, which is a deployment decision GMDW has no
        # business hard-coding: a headless session cannot answer a permission
        # prompt, and how permissive to be instead depends on the environment.
        # Whatever is set here, the PreToolUse gate still runs inside the child,
        # so this can widen what the child may *attempt*, never what it may do.
        self.extra_args = list(extra_args or []) or shlex.split(
            os.environ.get("GMDW_CLAUDE_ARGS", ""))

    def available(self) -> tuple:
        if shutil.which(self.binary) or Path(self.binary).exists():
            return True, ""
        return False, (f"the '{self.binary}' CLI was not found on PATH; the "
                       f"orchestrator invokes agents through it (set "
                       f"GMDW_CLAUDE_BIN to point at it)")

    def build_argv(self, spec: InvocationSpec) -> list:
        argv = [self.binary, "-p", "--output-format", "json"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.system_prompt:
            argv += ["--append-system-prompt", spec.system_prompt]
        if spec.allowed_tools:
            argv += ["--allowed-tools", ",".join(spec.allowed_tools)]
        argv += self.extra_args
        return argv

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        ok, why = self.available()
        if not ok:
            return InvocationResult(ok=False, error=why, exit_code=127)
        argv = self.build_argv(spec)
        env = {**os.environ,
               "GMDW_ROLE": spec.role,
               "GMDW_WORKFLOW_ID": spec.workflow_id}
        try:
            proc = subprocess.run(argv, input=spec.prompt, cwd=spec.cwd,
                                  capture_output=True, text=True, env=env,
                                  timeout=spec.timeout_sec)
        except subprocess.TimeoutExpired:
            return InvocationResult(
                ok=False, exit_code=124,
                error=f"agent '{spec.role}' exceeded its "
                      f"{spec.timeout_sec // 60}-minute invocation timeout")
        except OSError as e:
            return InvocationResult(ok=False, exit_code=126,
                                    error=f"could not start {self.binary}: {e}")

        payload, text = {}, (proc.stdout or "").strip()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, list) and payload:
            payload = payload[-1] if isinstance(payload[-1], dict) else {}
        if not isinstance(payload, dict):
            payload = {}

        tokens, cost, exact = _extract_usage(payload)
        if not tokens:
            # No usage reported: a rough size-based estimate keeps the budget
            # monotonic rather than silently free.
            tokens = max(1, (len(spec.prompt) + len(text)) // 4)
        result_text = payload.get("result") if isinstance(payload.get("result"), str) \
            else text
        failed = proc.returncode != 0 or bool(payload.get("is_error"))
        return InvocationResult(
            ok=not failed, text=result_text or "", tokens=tokens, cost_usd=cost,
            exact=exact, exit_code=proc.returncode,
            error="" if not failed else (proc.stderr or result_text or
                                         f"exit {proc.returncode}").strip()[:2000],
            raw=payload)


class ScriptedRunner(Runner):
    """A Runner whose behavior is supplied by the caller.

    ``script`` is a callable ``(spec) -> InvocationResult | None``; returning
    None means "the agent ran and wrote nothing", which is exactly the failure
    the acceptance pipeline must catch. Used by the test suite and by
    ``--dry-run`` to walk the whole loop with no model in it.
    """

    def __init__(self, script=None):
        self.script = script
        self.calls = []

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        self.calls.append(spec)
        out = self.script(spec) if self.script else None
        if out is None:
            return InvocationResult(ok=True, text="(no-op)", tokens=1)
        return out


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Prompt template per role. These are files rather than string constants only so
# they stay readable and diffable; they are code, not configuration, and the
# PreToolUse gate protects them from being rewritten mid-workflow.
PROMPT_FOR_ROLE = {
    "analyst-planner": "analyst_planner.md",
    "implementor": "implementor.md",
    "reviewer": "reviewer.md",
}


def load_prompt(role: str) -> str:
    name = PROMPT_FOR_ROLE.get(role)
    if not name:
        raise KeyError(f"no prompt template for role {role!r}")
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def render(template: str, values: dict) -> str:
    """``$name`` substitution. Deliberately not str.format: Markdown and code
    samples are full of braces, and a template language that trips over its own
    payload is a retry generator."""
    return string.Template(template).safe_substitute(values)


def build_prompt(role: str, values: dict) -> str:
    return render(load_prompt(role), values)
