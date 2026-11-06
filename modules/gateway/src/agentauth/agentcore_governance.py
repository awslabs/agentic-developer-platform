"""Durable owner-wide precedence for explicit AgentCore memory changes."""

import hashlib
import json
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_capability import ChatAuthorizationUnavailableError


class MemoryGovernanceConflictError(Exception):
    pass


def _key(owner):
    digest = hashlib.sha256(json.dumps([owner.tenant_id, owner.user_id], separators=(",", ":")).encode()).hexdigest()
    return {"PK": f"memory-owner#{digest}", "SK": "governance"}


def owner_state(table, owner):
    key = _key(owner)
    try:
        row = table.get_item(Key=key, ConsistentRead=True).get("Item")
    except (BotoCoreError, ClientError) as error:
        raise ChatAuthorizationUnavailableError("memory governance unavailable") from error
    if not row or row.get("PK") != key["PK"]:
        return {"epoch": 0, "fences": {}}
    epoch, fences = row.get("epoch"), row.get("fences")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int | Decimal)
        or epoch < 1
        or int(epoch) != epoch
        or not isinstance(fences, dict)
        or len(fences) > 100
    ):
        raise ChatAuthorizationUnavailableError("memory governance invalid")
    for record_id, fence in fences.items():
        if (
            not isinstance(record_id, str)
            or not isinstance(fence, dict)
            or fence.get("kind") not in ("correct", "forget")
            or not isinstance(fence.get("revision"), str)
            or not isinstance(fence.get("epoch"), int | Decimal)
            or isinstance(fence["epoch"], bool)
            or not 1 <= fence["epoch"] <= epoch
            or (fence["kind"] == "correct" and (not isinstance(fence.get("text"), str) or len(fence["text"]) > 2000))
        ):
            raise ChatAuthorizationUnavailableError("memory governance invalid")
    return {"epoch": int(epoch), "fences": fences}


def fence_revision(record_id, fence):
    values = ["memory-governance-v1", record_id, fence["revision"], int(fence["epoch"]), fence["kind"], fence.get("text")]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def mutation_retry_epoch(prior, record_id, revision, kind, text=None):
    if prior:
        if prior.get("revision") == revision and prior.get("kind") == kind and prior.get("text") == text:
            return int(prior["epoch"])
        if prior.get("kind") == "forget" or revision != fence_revision(record_id, prior):
            raise MemoryGovernanceConflictError("memory revision changed")
    return None


def advance_owner_state(table, owner, record_id, revision, kind, text=None):
    state = owner_state(table, owner)
    prior = state["fences"].get(record_id)
    retry_epoch = mutation_retry_epoch(prior, record_id, revision, kind, text)
    if retry_epoch is not None:
        return retry_epoch
    if len(state["fences"]) >= 100 and record_id not in state["fences"]:
        raise ChatAuthorizationUnavailableError("memory governance capacity reached")
    epoch = state["epoch"] + 1
    fences = {**state["fences"], record_id: {"kind": kind, "revision": revision, "text": text, "epoch": epoch}}
    try:
        table.update_item(
            Key=_key(owner),
            UpdateExpression="SET #epoch = :epoch, fences = :fences",
            ConditionExpression="#epoch = :previous" if state["epoch"] else "attribute_not_exists(PK)",
            ExpressionAttributeNames={"#epoch": "epoch"},
            ExpressionAttributeValues={":epoch": epoch, ":fences": fences, **({":previous": state["epoch"]} if state["epoch"] else {})},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise MemoryGovernanceConflictError("memory state changed") from None
        raise ChatAuthorizationUnavailableError("memory governance unavailable") from error
    except BotoCoreError as error:
        raise ChatAuthorizationUnavailableError("memory governance unavailable") from error
    return epoch
