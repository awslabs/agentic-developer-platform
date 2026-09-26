"""Version-fenced standing Task API policy for canonical service principals."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
AUTHORITY_TABLE_ENV = "AGENT_AUTHORITY_TABLE"
TASK_SCOPES = {"submit", "read", "input", "cancel", "artifacts"}
MAX_DURATION_MINUTES = 360
MAX_TURNS = 8
MAX_OUTPUT_TOKENS = 4096
MAX_USD = 1


def _valid_duration(value):
    integer = type(value) is int or isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value()
    return integer and 0 < value <= MAX_DURATION_MINUTES


class TaskServicePolicyError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TaskServicePolicyStore:
    def __init__(self, *, table_name: str | None = None, client=None, clock=None):
        self.table = table_name or os.environ.get(AUTHORITY_TABLE_ENV, "")
        if not self.table:
            raise TaskServicePolicyError("unavailable")
        self.client = client or boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def key(tenant_id: str, canonical_principal_id: str) -> dict:
        return {
            "pk": {"S": f"TENANT#{tenant_id}"},
            "sk": {"S": f"TASK_POLICY#{canonical_principal_id}"},
        }

    def get(self, *, tenant_id: str, canonical_principal_id: str) -> dict | None:
        try:
            item = self.client.get_item(
                TableName=self.table,
                Key=self.key(tenant_id, canonical_principal_id),
                ConsistentRead=True,
            ).get("Item")
        except (ClientError, BotoCoreError):
            raise TaskServicePolicyError("unavailable") from None
        if item is None:
            return None
        if (
            item.get("record_type") != {"S": "TASK_SERVICE_POLICY"}
            or item.get("canonical_principal_id") != {"S": canonical_principal_id}
            or item.get("scope", {}).get("M", {}).get("tenant_id") != {"S": tenant_id}
        ):
            raise TaskServicePolicyError("corrupt_policy")
        document = {
            name: _DESERIALIZER.deserialize(value) for name, value in item.items() if name not in {"pk", "sk", "scope", "record_type", "personas"}
        }
        document["tenant_id"] = tenant_id
        limits = document.get("limits")
        if not isinstance(limits, dict) or not _valid_duration(limits.get("max_duration_minutes")):
            raise TaskServicePolicyError("corrupt_policy")
        return document

    def put(
        self,
        *,
        tenant_id: str,
        canonical_principal_id: str,
        expected_version: int,
        policy: dict,
        updated_by: str,
    ) -> dict:
        _validate_policy(policy)
        current = self.get(tenant_id=tenant_id, canonical_principal_id=canonical_principal_id)
        current_version = int(current["version"]) if current else 0
        if current_version != expected_version:
            raise TaskServicePolicyError("version_conflict")
        version = expected_version + 1
        now = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        document = {
            **policy,
            "record_type": "TASK_SERVICE_POLICY",
            "schema_version": "1.0",
            "tenant_id": tenant_id,
            "canonical_principal_id": canonical_principal_id,
            "version": version,
            "updated_at": now,
            "updated_by": updated_by,
        }
        stored_document = {name: value for name, value in document.items() if name != "tenant_id"}
        stored_document["scope"] = {"tenant_id": tenant_id}
        # T1's acceptance and every conditional mutation use this same field.
        # It is written atomically with the public policy projection, never by
        # a worker or a second admission-specific policy writer.
        stored_document["personas"] = set(policy["allowed_personas"])
        item = {name: _SERIALIZER.serialize(_decimal_json(value)) for name, value in stored_document.items()}
        item.update(self.key(tenant_id, canonical_principal_id))
        condition = "attribute_not_exists(pk)" if expected_version == 0 else "#version = :expected"
        put = {
            "TableName": self.table,
            "Item": item,
            "ConditionExpression": condition,
        }
        if expected_version:
            put["ExpressionAttributeNames"] = {"#version": "version"}
            put["ExpressionAttributeValues"] = {":expected": {"N": str(expected_version)}}
        policy_digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        audit = {
            **self.key(tenant_id, canonical_principal_id),
            "sk": {"S": f"TASK_POLICY_AUDIT#{canonical_principal_id}#VERSION#{version:010d}"},
            "record_type": {"S": "TASK_SERVICE_POLICY_AUDIT"},
            "schema_version": {"S": "1.0"},
            "canonical_principal_id": {"S": canonical_principal_id},
            "version": {"N": str(version)},
            "policy_digest": {"S": policy_digest},
            "scope": {"M": {"tenant_id": {"S": tenant_id}}},
            "updated_at": {"S": now},
            "updated_by": {"S": updated_by},
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"Put": put},
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": audit,
                            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise TaskServicePolicyError("version_conflict") from None
            raise TaskServicePolicyError("unavailable") from None
        except BotoCoreError:
            raise TaskServicePolicyError("unavailable") from None
        return {name: value for name, value in document.items() if name != "record_type"}


def _validate_policy(policy: dict) -> None:
    if policy.get("status") not in {"active", "disabled"}:
        raise TaskServicePolicyError("invalid_policy")
    from src.agentauth.task_repository_policy import repositories
    from src.agentauth.task_tool_policy import valid_tools

    try:
        repositories(policy)
    except (ValueError, TypeError):
        raise TaskServicePolicyError("invalid_policy") from None

    if not valid_tools(policy.get("allowed_tools", [])):
        raise TaskServicePolicyError("invalid_policy")
    personas = policy.get("allowed_personas")
    scopes = policy.get("task_scopes")
    if not isinstance(personas, list) or not personas or len(personas) > 16 or len(set(personas)) != len(personas):
        raise TaskServicePolicyError("invalid_policy")
    if any(not isinstance(value, str) or not value or len(value) > 128 for value in personas):
        raise TaskServicePolicyError("invalid_policy")
    if not isinstance(scopes, list) or not scopes or set(scopes) - TASK_SCOPES or len(set(scopes)) != len(scopes):
        raise TaskServicePolicyError("invalid_policy")
    if not isinstance(policy.get("model_policy_version"), str) or not policy["model_policy_version"] or len(policy["model_policy_version"]) > 128:
        raise TaskServicePolicyError("invalid_policy")
    limits = policy.get("limits")
    required_limits = {"max_duration_minutes", "max_turns", "max_output_tokens_per_turn", "max_usd_per_task"}
    if not isinstance(limits, dict) or set(limits) - {"codex_max_turns"} != required_limits:
        raise TaskServicePolicyError("invalid_policy")
    codex_turns = limits.get("codex_max_turns", 8)
    if isinstance(codex_turns, bool) or not isinstance(codex_turns, int | Decimal) or not 1 <= codex_turns <= 32 or int(codex_turns) != codex_turns:
        raise TaskServicePolicyError("invalid_policy")
    if not _valid_duration(limits["max_duration_minutes"]):
        raise TaskServicePolicyError("invalid_policy")
    ceilings = {
        "max_duration_minutes": MAX_DURATION_MINUTES,
        "max_turns": MAX_TURNS,
        "max_output_tokens_per_turn": MAX_OUTPUT_TOKENS,
        "max_usd_per_task": MAX_USD,
    }
    for name, ceiling in ceilings.items():
        value = limits.get(name)
        if not isinstance(value, int | float | Decimal) or isinstance(value, bool) or value <= 0 or value > ceiling:
            raise TaskServicePolicyError("invalid_policy")


def _decimal_json(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _decimal_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimal_json(item) for item in value]
    return value
