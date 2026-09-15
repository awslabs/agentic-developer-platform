"""Provision a service run only from a recorded human-approved service policy."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from common.agent_authority import (
    AuthorityProvisionError,
    _digest,
    _expiry_live,
    _key,
    _read,
)


@dataclass(frozen=True)
class VerifiedServiceEvent:
    event_id: str
    service_identity: str
    tenant_id: str
    repo: str
    rule_arn: str

    @classmethod
    def from_native_event(cls, *, event: dict, identity):
        # Invocation is through the native EventBridge Lambda permission, never
        # an API Gateway wrapper. Workers have neither PutEvents nor InvokeFunction.
        try:
            event_id = str(uuid.UUID(event["id"]))
            rule_arn = identity.rule_arn
            if (
                "headers" in event
                or "requestContext" in event
                or not rule_arn.startswith("arn:aws:events:")
                or event.get("account") != rule_arn.split(":")[4]
                or event.get("adp_rule_arn") != rule_arn
                or not identity.repo
            ):
                raise ValueError("unverified service source")
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise AuthorityProvisionError("verified service event required") from None
        return cls(
            event_id,
            identity.service_identity,
            identity.tenant_id,
            identity.repo,
            rule_arn,
        )


def provision_service_dispatch(
    *, envelope, event: VerifiedServiceEvent, client=None, now=None
):
    table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
    if not table or not isinstance(event, VerifiedServiceEvent):
        raise AuthorityProvisionError("service authority is not configured")
    now = now or datetime.now(UTC)
    ddb = client or boto3.client(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
    pk = f"TENANT#{event.tenant_id}"
    try:
        binding = _read(ddb, table, pk, f"SERVICE#{event.service_identity}") or {}
        reference = binding["authority_reference_id"]["S"]
        authority = _read(ddb, table, pk, f"AUTHORITY#{reference}") or {}
        persona = envelope["persona"]
        if (
            envelope.get("tenant_id") != event.tenant_id
            or envelope["source_ref"]["repo"] != event.repo
            or authority.get("service_identity") != {"S": event.service_identity}
            or authority.get("rule_arn") != {"S": event.rule_arn}
            or authority.get("repo") != {"S": event.repo}
            or authority.get("status") != {"S": "active"}
            or authority.get("authority_kind") != {"S": "service_policy"}
            or authority.get("actor_kind") != {"S": "human"}
            or not authority.get("human_id", {}).get("S")
            or persona not in authority.get("root_personas", {}).get("SS", [])
            or not _expiry_live(authority, now)
        ):
            raise AuthorityProvisionError("service delegation refused")
        invocation = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"adp-service:{event.tenant_id}:{event.service_identity}:{event.event_id}:{persona}",
            )
        )
        prior = _read(ddb, table, f"INVOCATION#{invocation}", "DISPATCH")
        arrived_at = (
            prior["arrived_at"]["S"] if prior else now.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        expiry = min(
            datetime.strptime(
                authority["expires_at"]["S"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC),
            datetime.strptime(arrived_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            + timedelta(days=7),
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        flow = f"service-run:{invocation}"
        final = {
            **envelope,
            "message_id": invocation,
            "arrived_at": arrived_at,
            "cognito_sub": "",
            "actor": {
                **envelope.get("actor", {}),
                "user_id": event.service_identity,
                "org_id": event.tenant_id,
                "kind": "service",
                "is_bot": True,
            },
            "correlation": {
                "correlation_id": flow,
                "root_human_id": authority["human_id"]["S"],
                "is_human_rooted": True,
                "parent_invocation_id": None,
                "chain_depth": 0,
            },
        }
        digest = _digest(final)
        execution = {
            **_key(pk, f"EXEC#{invocation}"),
            "invocation_id": {"S": invocation},
            "tenant_id": {"S": event.tenant_id},
            "current_attempt": {"N": "1"},
            "status": {"S": "pending"},
            "current_credential_epoch": {"N": "1"},
            "min_acceptable_credential_epoch": {"N": "1"},
            "repo": {"S": event.repo},
            "flow_id": {"S": flow},
            "arrived_at": {"S": arrived_at},
            "persona": {"S": persona},
            "envelope_digest": {"S": digest},
            # Scheduled roots can create their issue after starting. Zero is an
            # unassigned work item, never a wildcard for child dispatch.
            "issue_number": {"N": str(final["source_ref"].get("issue") or 0)},
            "installation_id": {"N": str(final["source_ref"]["installation_id"])},
            "chain_depth": {"N": "0"},
        }
        child_personas = authority.get("child_personas", {}).get("SS", [])
        repository_id = final["source_ref"].get("provider_repository_id")
        if type(repository_id) is int and repository_id > 0:
            execution["provider_repository_id"] = {"N": str(repository_id)}
        actions = ["monitor", "dispatch"] if child_personas else ["monitor"]
        grant = {
            **_key(pk, f"GRANT#{invocation}#1"),
            "grant_id": {"S": f"grant:{invocation}:1"},
            "tenant_id": {"S": event.tenant_id},
            "principal": {"S": f"{invocation}#1"},
            "authority_kind": {"S": "service_policy"},
            "authority_reference_id": {"S": reference},
            "authority_human_id": authority["human_id"],
            "authority_org_id": {"S": event.tenant_id},
            "allowed_actions": {"SS": actions},
            "delegable_actions": {"SS": actions},
            "target_relationships": {"SS": ["self", "descendant"]},
            "repo_scope": {"SS": [event.repo]},
            "flow_id": {"S": flow},
            "expires_at": {"S": expiry},
            "revocation_epoch": {"N": "1"},
            "revoked": {"BOOL": False},
            "max_dispatch_concurrency": authority["max_dispatch_concurrency"],
            "max_total_dispatches": authority["max_total_dispatches"],
            "max_chain_depth": authority["max_chain_depth"],
            "work_item_issue": execution["issue_number"],
            "dispatch_issue_scope": authority["child_issue_scope"],
        }
        if child_personas:
            grant["dispatch_personas"] = {"SS": child_personas}
        lookup = {
            **_key(f"INVOCATION#{invocation}", "DISPATCH"),
            "tenant_id": {"S": event.tenant_id},
            "envelope_digest": {"S": digest},
            "arrived_at": {"S": arrived_at},
            "grant_digest": {"S": _digest(grant)},
        }
        transaction = [
            {
                "Put": {
                    "TableName": table,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            }
            for item in (execution, grant, lookup)
        ]
        transaction.extend(
            [
                {
                    "ConditionCheck": {
                        "TableName": table,
                        "Key": _key(pk, f"AUTHORITY#{reference}"),
                        "ConditionExpression": (
                            "#s = :active AND expires_at > :now "
                            "AND intent_digest = :digest"
                        ),
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":active": {"S": "active"},
                            ":now": {"S": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
                            ":digest": authority["intent_digest"],
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": table,
                        "Key": _key(pk, f"SERVICE#{event.service_identity}"),
                        "ConditionExpression": "authority_reference_id = :reference",
                        "ExpressionAttributeValues": {":reference": {"S": reference}},
                    }
                },
            ]
        )
        try:
            ddb.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError):
            current = _read(ddb, table, pk, f"EXEC#{invocation}") or {}
            if (
                _read(ddb, table, f"INVOCATION#{invocation}", "DISPATCH") != lookup
                or _read(ddb, table, pk, f"GRANT#{invocation}#1") != grant
                or _read(ddb, table, pk, f"AUTHORITY#{reference}") != authority
                or _read(ddb, table, pk, f"SERVICE#{event.service_identity}") != binding
                or current.get("status", {}).get("S") not in {"pending", "active"}
            ):
                raise AuthorityProvisionError(
                    "service dispatch conflict or unavailable"
                ) from None
        return final
    except (ClientError, BotoCoreError, KeyError, TypeError, ValueError):
        raise AuthorityProvisionError("service approval unavailable") from None
