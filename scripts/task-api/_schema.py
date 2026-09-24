"""A minimal JSON Schema draft 2020-12 validator, standard library only.

Why this exists rather than a dependency: the Task API conformance baseline is
meant to be checkable by any evaluator who clones the repository at a given
revision and runs one command. A pip install step is a place for that to fail,
and a validator that cannot run produces a blocked evaluation rather than a
verdict. This implements only the keywords the v1 contract schemas actually
use; anything else is reported as an unsupported keyword instead of being
silently ignored, so the schemas can never quietly outgrow the checker.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SUPPORTED = {
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "not",
    "anyOf",
    "allOf",
    "oneOf",
    "if",
    "then",
    "else",
    "format",
    "examples",
    "default",
    "deprecated",
}

TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}

SUPPORTED_FORMATS = {"date-time"}


class SchemaError(Exception):
    """Raised when a schema itself is malformed or uses an unsupported keyword."""


class Registry:
    """Loads schema documents by filename and resolves $ref between them."""

    def __init__(self, schema_dir: Path) -> None:
        self.schema_dir = schema_dir
        self.docs: dict[str, Any] = {}
        for path in sorted(schema_dir.glob("*.schema.json")):
            self.docs[path.name] = json.loads(path.read_text())

    def resolve(self, ref: str, current_doc: str) -> tuple[Any, str]:
        """Return (subschema, document name) for a $ref."""
        if "#" not in ref:
            raise SchemaError(f"unsupported $ref form: {ref!r}")
        doc_part, pointer = ref.split("#", 1)
        doc_name = doc_part or current_doc
        if doc_name not in self.docs:
            raise SchemaError(f"$ref {ref!r} names unknown document {doc_name!r}")
        node = self.docs[doc_name]
        for token in [t for t in pointer.split("/") if t]:
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or token not in node:
                raise SchemaError(f"$ref {ref!r} does not resolve")
            node = node[token]
        return node, doc_name

    def check_keywords(self) -> list[str]:
        """Report any schema keyword this validator does not implement."""
        problems: list[str] = []

        def walk(node: Any, where: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if where.endswith(("/properties", "/$defs")):
                        walk(value, f"{where}/{key}")
                        continue
                    if key not in SUPPORTED:
                        problems.append(f"{where}: unsupported keyword {key!r}")
                    if key == "format" and value not in SUPPORTED_FORMATS:
                        problems.append(f"{where}: unsupported format {value!r}")
                    walk(value, f"{where}/{key}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{where}[{i}]")

        for name, doc in self.docs.items():
            walk(doc, name)
        return problems


def _is_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    py = TYPE_MAP.get(expected)
    if py is None:
        raise SchemaError(f"unknown type {expected!r}")
    if py is str or py is dict or py is list or py is type(None):
        return isinstance(value, py)
    return isinstance(value, py)


def validate(
    instance: Any,
    schema: Any,
    registry: Registry,
    doc: str,
    path: str = "$",
) -> list[str]:
    """Return a list of validation errors. Empty means the instance is valid."""
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: schema forbids any value"]
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object or boolean")

    errors: list[str] = []

    if "$ref" in schema:
        target, target_doc = registry.resolve(schema["$ref"], doc)
        errors += validate(instance, target, registry, target_doc, path)
        # Sibling keywords alongside $ref still apply in 2020-12.
        rest = {k: v for k, v in schema.items() if k != "$ref"}
        if rest:
            errors += validate(instance, rest, registry, doc, path)
        return errors

    if "type" in schema:
        types = schema["type"]
        types = types if isinstance(types, list) else [types]
        if not any(_is_type(instance, t) for t in types):
            errors.append(
                f"{path}: expected type {'/'.join(types)}, got {type(instance).__name__}"
            )
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(
                f"{path}: length {len(instance)} exceeds maxLength {schema['maxLength']}"
            )
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']!r}")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{path}: {instance!r} is not a valid RFC3339 date-time")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} above maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(
                f"{path}: {len(instance)} items exceeds maxItems {schema['maxItems']}"
            )
        if schema.get("uniqueItems"):
            seen = [json.dumps(i, sort_keys=True) for i in instance]
            if len(set(seen)) != len(seen):
                errors.append(f"{path}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += validate(item, schema["items"], registry, doc, f"{path}[{i}]")

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        props = schema.get("properties", {})
        for name, value in instance.items():
            if name in props:
                errors += validate(value, props[name], registry, doc, f"{path}.{name}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(props))
            for name in extra:
                errors.append(f"{path}: additional property {name!r} is not permitted")
        elif isinstance(schema.get("additionalProperties"), dict):
            for name in sorted(set(instance) - set(props)):
                errors += validate(
                    instance[name],
                    schema["additionalProperties"],
                    registry,
                    doc,
                    f"{path}.{name}",
                )

    if "not" in schema and not validate(instance, schema["not"], registry, doc, path):
        desc = schema["not"].get("description", "")
        errors.append(f"{path}: value matches a forbidden shape. {desc}".rstrip())

    if "allOf" in schema:
        for i, sub in enumerate(schema["allOf"]):
            errors += validate(instance, sub, registry, doc, path)

    if "anyOf" in schema:
        results = [
            validate(instance, sub, registry, doc, path) for sub in schema["anyOf"]
        ]
        if all(results):
            errors.append(
                f"{path}: matches none of the {len(results)} permitted alternatives"
            )

    if "oneOf" in schema:
        results = [
            validate(instance, sub, registry, doc, path) for sub in schema["oneOf"]
        ]
        passing = [i for i, r in enumerate(results) if not r]
        if len(passing) != 1:
            errors.append(
                f"{path}: matched {len(passing)} oneOf alternatives, expected exactly 1"
            )

    if "if" in schema:
        if not validate(instance, schema["if"], registry, doc, path):
            if "then" in schema:
                errors += validate(instance, schema["then"], registry, doc, path)
        elif "else" in schema:
            errors += validate(instance, schema["else"], registry, doc, path)

    return errors
