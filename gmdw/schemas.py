"""GMDW schemas — JSON-Schema-subset validation.

Since v0.3.0 the agent-authored artifacts are structured Markdown, validated by
:mod:`gmdw.parsers`. JSON survives in exactly one place: the **approval record**,
which is machine-written by ``wf approve`` (never by a model), is small and
closed, and is the artifact the APPROVED and COMPLETED guards read. Keeping it
JSON keeps the human-decision record exact.

Schemas are authored as valid JSON Schema Draft 2020-12 documents. Because the
runtime must be stdlib-only, validation is performed by a deliberate SUBSET
validator covering exactly the keywords the GMDW schemas use:

  type, required, properties, additionalProperties (boolean or schema),
  enum, const, pattern, items, minItems, maxItems, minimum, maximum

Schemas in .workflow/schemas/ MUST restrict themselves to these keywords
(no $ref/$defs/anyOf) — this is checked by the governance test suite.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# logical artifact type -> schema file (basename without .schema.json)
SCHEMA_FOR_TYPE = {
    "approval": "approval",
}

_TYPE_CHECK = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def load_schema(root, logical_name: str) -> dict:
    p = Path(root) / ".workflow" / "schemas" / f"{logical_name}.schema.json"
    return json.loads(p.read_text(encoding="utf-8"))


def validate(instance, schema: dict, path: str = "$") -> list:
    """Return a list of human-readable validation errors (empty = valid)."""
    errs = []

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPE_CHECK.get(x, lambda v: True)(instance) for x in types):
            errs.append(f"{path}: expected type {t}, got {type(instance).__name__}")
            return errs  # type mismatch: deeper checks are meaningless

    if "const" in schema and instance != schema["const"]:
        errs.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: {instance!r} not in {schema['enum']}")

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{path}.{req}: required property missing")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for key, val in instance.items():
            if key in props:
                errs += validate(val, props[key], f"{path}.{key}")
            elif addl is False:
                errs.append(f"{path}.{key}: additional property not allowed")
            elif isinstance(addl, dict):
                errs += validate(val, addl, f"{path}.{key}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append(f"{path}: expected at most {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, val in enumerate(instance):
                errs += validate(val, items, f"{path}[{i}]")

    if isinstance(instance, str) and "pattern" in schema:
        if not re.search(schema["pattern"], instance):
            errs.append(f"{path}: {instance!r} does not match {schema['pattern']!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append(f"{path}: {instance} > maximum {schema['maximum']}")

    return errs


def validate_artifact(root, artifact: dict, logical_type: str) -> list:
    """Validate a JSON artifact against its schema. `logical_type` is a key of
    SCHEMA_FOR_TYPE (only 'approval' since v0.3.0)."""
    if logical_type not in SCHEMA_FOR_TYPE:
        return [f"unknown JSON artifact type: {logical_type} "
                f"(Markdown artifacts are validated by gmdw.parsers)"]
    return validate(artifact, load_schema(root, SCHEMA_FOR_TYPE[logical_type]), "$")
