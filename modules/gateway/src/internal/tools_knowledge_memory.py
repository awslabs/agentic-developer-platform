"""Bounded human memory calls under the original budgeted tool admission.

The facade supplies a signed reference to its original operation. A receipt
from discovery is insufficient: reopen the durable call admission, bind every
argument and claim one Door dispatch without a TTL. Lost acknowledgements stay
unknown and cannot authorize another write. Native/run authority is separate.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Literal
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from src.agentauth.test_run_admission_store import canonical
from src.internal.tools_admissions import ToolCallAdmission
from src.internal.tools_dynamic_catalogue import canonical as wire_canonical

MEMORY_TOOLS = frozenset({"knowledge___remember", "knowledge___experience"})
# A conservative reservation for one bounded embedding and its storage work.
# This uses the ordinary tool budget hierarchy; it is not native-test funding.
MEMORY_ESTIMATE_USD = Decimal("0.01")


class MemoryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    request_id: UUID
    execution_id: UUID
    requested_execution_id: UUID | None


def validate_arguments(operation: str, arguments: dict) -> None:
    """Bound provider input before either reserving funds or dispatching."""

    def text(name, maximum, *, required=False):
        value = arguments.get(name)
        if value is None and not required:
            return
        if type(value) is not str or len(value.encode()) > maximum or (required and not value.strip()):
            raise ValueError

    try:
        if len(canonical(arguments).encode()) > 32768:
            raise ValueError
        if operation == "remember":
            if not set(arguments) <= {"session_id", "messages", "outcome"}:
                raise ValueError
            text("session_id", 128, required=True)
            text("outcome", 2048)
            messages = arguments.get("messages")
            if type(messages) is not list or not 1 <= len(messages) <= 20:
                raise ValueError
            for message in messages:
                if (
                    type(message) is not dict
                    or set(message) != {"role", "content"}
                    or message["role"] not in {"system", "user", "assistant", "tool"}
                    or type(message["content"]) is not str
                    or len(message["content"].encode()) > 8192
                ):
                    raise ValueError
        elif operation == "experience":
            action = arguments.get("action")
            if action not in {"save", "recall", "list_syntheses"}:
                raise ValueError
            fields = {
                "save": {"content", "learning_type", "context", "visibility"},
                "recall": {"query", "limit", "cross_persona"},
                "list_syntheses": {"limit"},
            }[action]
            if not set(arguments) <= {"action", "persona"} | fields:
                raise ValueError
            if arguments.get("persona") not in {"developer", "operations", "architect", "reviewer"}:
                raise ValueError
            text("content", 8192, required=action == "save")
            text("query", 2048, required=action == "recall")
            text("learning_type", 128)
            if "context" in arguments and (type(arguments["context"]) is not dict or len(canonical(arguments["context"]).encode()) > 4096):
                raise ValueError
            if arguments.get("visibility", "private") not in {"private", "shared"}:
                raise ValueError
            if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 25):
                raise ValueError
            if "cross_persona" in arguments and type(arguments["cross_persona"]) is not bool:
                raise ValueError
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(422, "invalid knowledge memory arguments") from None


def claim_memory(claims, operation, arguments, runtime) -> dict:
    """Reopen the paid human admission, then consume one permanent replay fence."""
    from src.internal.tools_authorize import ToolsAuthorizationBody

    validate_arguments(operation, arguments)
    try:
        descriptor = MemoryOperation.model_validate(claims.get("knowledge_memory"))
        if claims.get("native_test") is not None or claims.get("run_id") is not None and type(claims.get("host_generation")) is not int:
            raise ValueError
        if descriptor.requested_execution_id is not None and descriptor.requested_execution_id != descriptor.execution_id:
            raise ValueError
        principal = claims["principal"]
        body = ToolsAuthorizationBody(
            schema_version="1.0",
            tool="knowledge___" + operation,
            arguments=arguments,
            request_id=descriptor.request_id,
            execution_id=descriptor.requested_execution_id,
        )
        original = ToolCallAdmission(
            table=runtime.store.table,
            client=runtime.store.client,
            tenant_id=principal["tenant_id"],
            user_id=principal["user_id"],
            team_id=principal["team_id"],
            run_id=claims.get("run_id"),
            workload_id=claims.get("workload_id"),
            body=body,
        )
        admitted = runtime.store.client.get_item(TableName=runtime.store.table, Key=original.key, ConsistentRead=True).get("Item") or {}
        if (
            admitted.get("record_type") != {"S": "TOOLS_CALL_ADMISSION"}
            or admitted.get("status") != {"S": "admitted"}
            or admitted.get("binding") != {"S": original.binding}
            or admitted.get("receipt_id") != {"S": claims["receipt_id"]}
        ):
            raise ValueError
        binding = {
            "schema_version": "1.0",
            "tool": "knowledge___" + operation,
            "principal": principal,
            "receipt_id": claims["receipt_id"],
            "arguments_sha256": claims["arguments_sha256"],
            "request_id": str(descriptor.request_id),
            "execution_id": str(descriptor.execution_id),
        }
        key = hashlib.sha256(canonical({"principal": principal, "receipt_id": claims["receipt_id"]}).encode()).hexdigest()
        runtime.store.client.put_item(
            TableName=runtime.store.table,
            Item={
                "pk": {"S": "TOOLS_KNOWLEDGE_MEMORY#" + key},
                "sk": {"S": "DISPATCH"},
                "binding": {"S": canonical(binding)},
                "status": {"S": "dispatch_claimed"},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return binding
    except (ValueError, KeyError, TypeError, ValidationError):
        raise HTTPException(403, "knowledge memory admission refused") from None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(409, "knowledge memory already submitted; recover original operation") from None
        raise HTTPException(503, "knowledge memory admission unavailable") from None
    except BotoCoreError:
        raise HTTPException(503, "knowledge memory admission unavailable") from None


def completion(binding, operation, arguments, result):
    """Only a complete, recognized Door result can settle a memory operation."""
    if result.get("isError"):
        return None
    content = result.get("content")
    try:
        if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
            raise ValueError
        value = json.loads(content[0]["text"])
        if not isinstance(value, dict) or value.get("error"):
            raise ValueError
        if operation == "remember":
            valid = value.get("stored") is True and value.get("session_id") == arguments["session_id"]
            entry_id = value.get("entry_id")
        elif arguments["action"] == "save":
            valid = value.get("status") == "saved" and value.get("persona") == arguments["persona"]
            valid = valid and value.get("visibility") == arguments.get("visibility", "private")
            entry_id = value.get("id")
        else:
            field = "results" if arguments["action"] == "recall" else "syntheses"
            valid = value.get("status") == "ok" and isinstance(value.get(field), list)
            entry_id = None
        if not valid or (entry_id is not None and (not isinstance(entry_id, str) or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", entry_id))):
            raise ValueError
        if (operation == "remember" or arguments.get("action") == "save") and entry_id is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HTTPException(502, "knowledge memory completion unavailable") from None
    return {**binding, "result_sha256": hashlib.sha256(wire_canonical(value)).hexdigest()}
