"""Physical encoding for JSON numbers outside DynamoDB's numeric domain.

Only designated record columns are encoded, and only when native DynamoDB
numbers cannot represent them. The root metadata is server-owned; user JSON
objects are never interpreted as encoding tags. Repository reads restore the
same JSON value before identity hashes or public DTOs consume it.
"""

from __future__ import annotations

import json
from decimal import Decimal, DecimalException
from typing import Any

import rfc8785
from boto3.dynamodb.types import TypeSerializer

JSON_ENCODING_FIELD = "_task_json_encoding_v1"
JSON_COLUMNS = {
    "TASK": frozenset({"input_payload", "input_reference", "result", "error", "input_request"}),
    "TASK_RUN": frozenset({"result", "error"}),
    "TASK_RUN_GRANT": frozenset({"input", "model_binding", "limits"}),
    "TASK_EVENTS": frozenset({"data"}),
    "TASK_COMMANDS": frozenset({"payload"}),
    "TASK_TURNS": frozenset({"messages", "input"}),
    "TASK_OPS": frozenset({"messages", "content", "usage", "request", "response", "result", "receipt"}),
}
_SERIALIZER = TypeSerializer()


def _numbers(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numbers(item) for item in value]
    return value


def encode_json_value(value: Any) -> tuple[Any, bool]:
    """Return native JSON or canonical UTF-8 JSON text plus its encoding flag."""
    canonical = rfc8785.dumps(value)
    try:
        _SERIALIZER.serialize(_numbers(value))
    except DecimalException:
        return canonical.decode("utf-8"), True
    return value, False


def _encoding_fields(record: dict[str, Any]) -> set[str]:
    marker = record.get(JSON_ENCODING_FIELD, [])
    allowed = JSON_COLUMNS.get(record.get("record_type"), frozenset())
    if not isinstance(marker, list) or any(not isinstance(name, str) or name not in allowed for name in marker):
        raise ValueError("Task JSON encoding metadata is invalid")
    if len(marker) != len(set(marker)):
        raise ValueError("Task JSON encoding metadata has duplicate columns")
    return set(marker)


def encode_record_json(record: dict[str, Any]) -> dict[str, Any]:
    """Encode a hydrated complete record without changing ordinary fixtures."""
    allowed = JSON_COLUMNS.get(record.get("record_type"), frozenset())
    if not allowed:
        if JSON_ENCODING_FIELD in record:
            raise ValueError("JSON encoding metadata is not allowed on this record")
        return dict(record)
    marked = _encoding_fields(record)
    result = dict(record)
    for name in allowed:
        if name not in record:
            marked.discard(name)
            continue
        result[name], encoded = encode_json_value(record[name])
        if encoded:
            marked.add(name)
        else:
            marked.discard(name)
    if marked:
        result[JSON_ENCODING_FIELD] = sorted(marked)
    else:
        result.pop(JSON_ENCODING_FIELD, None)
    return result


def decode_record_json(record: dict[str, Any]) -> dict[str, Any]:
    """Hydrate explicitly marked columns; nested user maps are never inspected."""
    if JSON_ENCODING_FIELD not in record:
        return record
    marked = _encoding_fields(record)
    result = dict(record)
    for name in marked:
        # Content compaction may remove a column while retaining its metadata.
        if name not in record:
            continue
        raw = record[name]
        if not isinstance(raw, str):
            raise ValueError("Encoded task JSON column is not text")
        value = json.loads(raw)
        if rfc8785.dumps(value).decode("utf-8") != raw:
            raise ValueError("Encoded task JSON column is not canonical")
        result[name] = value
    return result


def encode_json_updates(snapshot: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Prepare column updates and any changed root marker for one transaction.

    The caller must persist all returned entries under its existing version and
    authority fence. It must not pass these already-physical values through
    encode_record_json a second time; expression-value serializers are fine.
    """
    if JSON_ENCODING_FIELD in updates:
        raise ValueError("JSON encoding metadata cannot be caller-selected")
    allowed = JSON_COLUMNS.get(snapshot.get("record_type"), frozenset())
    previous = _encoding_fields(snapshot)
    marked = set(previous)
    result = dict(updates)
    for name in allowed.intersection(updates):
        result[name], encoded = encode_json_value(updates[name])
        if encoded:
            marked.add(name)
        else:
            marked.discard(name)
    if marked != previous:
        # An empty list removes stale decoding without adding a REMOVE clause.
        result[JSON_ENCODING_FIELD] = sorted(marked)
    return result
