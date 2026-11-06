"""Immutable evidence succession for one unchanged stopped bootstrap attempt.

This framing contract is also loaded by the native installation owner. It has
no Gateway runtime dependencies. Source qualification remains with the genuine
collector and the Gateway's full proof validator; history is never authority.
"""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

IDENTITY = ("org_id", "workspace_id", "operation_id", "plan_digest", "source_key", "source_fence")
MAX_RENEWALS = 8
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_STORED_BYTES = 380000
PREFIX = "sppr1:bootstrap:"
SUCCESSOR_PREFIX = PREFIX + "renewal:"
RENEWAL_FIELDS = {"generation", "predecessor_record_id", "predecessor_revision"}


def require(value):
    if not value:
        raise ValueError("bootstrap evidence succession is unproven")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def encode_document(value):
    """Preserve legacy small items; compress complete larger immutable history."""
    import base64
    import gzip

    raw = canonical(value).encode()
    require(isinstance(value, dict) and len(raw) <= MAX_DOCUMENT_BYTES)
    if len(raw) <= MAX_STORED_BYTES:
        return raw.decode()
    document = canonical({"encoding": "gzip-base64-v1", "value": base64.b64encode(gzip.compress(raw, mtime=0)).decode()})
    require(len(document.encode()) <= MAX_STORED_BYTES)
    return document


def decode_document(document):
    import base64
    import gzip
    import io

    require(isinstance(document, str) and len(document.encode()) <= 400 * 1024)
    value = json.loads(document)
    require(isinstance(value, dict))
    if set(value) == {"encoding", "value"}:
        require(len(document.encode()) <= MAX_STORED_BYTES)
        require(value["encoding"] == "gzip-base64-v1")
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(value["value"], validate=True))) as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        require(len(raw) <= MAX_DOCUMENT_BYTES)
        value = json.loads(raw)
        require(isinstance(value, dict) and raw == canonical(value).encode())
    return value


def base_record_id(value):
    return PREFIX + digest({key: value[key] for key in IDENTITY})


def generation(value):
    if "renewal" not in value:
        return 0
    renewal = value["renewal"]
    require(isinstance(renewal, dict) and set(renewal) == RENEWAL_FIELDS)
    require(type(renewal["generation"]) is int and 1 <= renewal["generation"] <= MAX_RENEWALS)
    require(isinstance(renewal["predecessor_revision"], str) and re.fullmatch(r"[a-f0-9]{64}", renewal["predecessor_revision"]))
    predecessor = renewal["predecessor_record_id"]
    if renewal["generation"] == 1:
        require(predecessor == base_record_id(value))
    else:
        require(isinstance(predecessor, str) and re.fullmatch(re.escape(SUCCESSOR_PREFIX) + r"[a-f0-9]{64}", predecessor))
    return renewal["generation"]


def _successor_id(base, renewal):
    return SUCCESSOR_PREFIX + digest({"base_record_id": base, **renewal})


def record_id(value):
    return _successor_id(base_record_id(value), value["renewal"]) if generation(value) else base_record_id(value)


def successor(value):
    """Derive the only child address, including a probe beyond the chain limit."""
    renewal = {
        "generation": generation(value) + 1,
        "predecessor_record_id": value["record_id"],
        "predecessor_revision": digest(value),
    }
    return _successor_id(base_record_id(value), renewal), renewal


def framing(value, *, require_fresh=True):
    require(type(require_fresh) is bool and isinstance(value, dict))
    require(value.get("record_id") == record_id(value))
    require(type(value.get("source_fence")) is int and 1 <= value["source_fence"] <= 32)
    checked, expiry = (datetime.fromisoformat(value[key]) for key in ("checked_at", "expires_at"))
    now = datetime.now(UTC)
    require(checked.tzinfo is not None and expiry.tzinfo is not None)
    require(checked <= now and checked < expiry <= checked + timedelta(hours=1))
    require(not require_fresh or now < expiry)
    return value


def stable_boundary(value):
    result = copy.deepcopy(value)
    for key in ("record_id", "renewal", "checked_at", "expires_at", "source_audit"):
        result.pop(key, None)
    for proof in result.get("precomponent_failures", {}).values():
        proof.pop("source_audit_sha256", None)
    return result


def linked(predecessor, value):
    key, renewal = successor(predecessor)
    require(value.get("record_id") == key and value.get("renewal") == renewal)
    require(stable_boundary(predecessor) == stable_boundary(value))
    require(datetime.fromisoformat(predecessor["expires_at"]) <= datetime.fromisoformat(value["checked_at"]))
    before, after = predecessor["source_audit"], value["source_audit"]
    for field in ("log_group", "creation_event_id"):
        require(isinstance(before.get(field), str) and bool(before[field]) and before[field] == after.get(field))
    terminal_ids = []
    for audit in (before, after):
        values = audit.get("terminal_event_ids")
        require(isinstance(values, list) and values and all(isinstance(v, str) and v for v in values))
        require(values == sorted(set(values)))
        terminal_ids.append(set(values))
    require(terminal_ids[0] <= terminal_ids[1])


def decode_item(item, key):
    require(isinstance(item, dict) and set(item) == {"record_id", "document", "revision"})
    for attribute in item.values():
        require(isinstance(attribute, dict) and set(attribute) == {"S"} and isinstance(attribute["S"], str))
    value = decode_document(item["document"]["S"])
    require(item["record_id"]["S"] == key and item["revision"]["S"] == digest(value))
    require(isinstance(value, dict) and value.get("record_id") == key)
    return framing(value, require_fresh=False)


def chain(read_item, identity, *, validate_history=None):
    """Walk immutable records using consistent point reads supplied by the owner.

    A missing root is returned only for first publication. A malformed child is
    never absence. Probe the child even when a node is fresh or at the maximum
    generation, so an appended or over-limit record cannot be silently ignored.
    """
    result, key = [], base_record_id(identity)
    while True:
        item = read_item(key)
        if item is None:
            return result
        require(len(result) <= MAX_RENEWALS)
        value = decode_item(item, key)
        require(all(value[k] == identity[k] for k in IDENTITY))
        if validate_history is not None:
            validate_history(value)
        if result:
            linked(result[-1], value)
        else:
            require(generation(value) == 0)
        result.append(value)
        key, _ = successor(value)


def renew(evidence, predecessor):
    """Attach a checked predecessor to newly collected evidence; never alter dates."""
    require("renewal" not in evidence and evidence["record_id"] == base_record_id(evidence))
    require(generation(predecessor) < MAX_RENEWALS)
    result = copy.deepcopy(evidence)
    result["record_id"], result["renewal"] = successor(predecessor)
    framing(result)
    linked(predecessor, result)
    return result
