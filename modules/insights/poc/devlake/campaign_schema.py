"""Validate private operating campaign documents against the public shape contract."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


SCHEMA = json.loads(Path(__file__).with_name("campaign.schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
VALIDATORS = {
    kind: Draft202012Validator(
        {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{kind}"},
        format_checker=FormatChecker(),
    )
    for kind in ("launch", "frozen", "limits", "batch", "checkpoint", "report")
}


def validate(document, kind):
    """Reject malformed evidence without repeating potentially private input values."""
    try:
        VALIDATORS[kind].validate(document)
    except ValidationError:
        raise ValueError(f"{kind} does not match campaign schema") from None
