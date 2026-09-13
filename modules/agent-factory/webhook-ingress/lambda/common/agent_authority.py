"""Trusted human webhook -> protected authority, grant and pending execution.

Only the HMAC-verified GitHub handler supplies VerifiedHumanEvent. Agent HTTP,
bot-comment and EventBridge adapters cannot manufacture human authority by
passing their mutable correlation fields into the shared spawn function.
The worker role has no write permission on this table.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError


class AuthorityProvisionError(Exception):
    """Dispatch cannot publish without a matching protected authority record."""


@dataclass(frozen=True)
class VerifiedHumanEvent:
    reference_id: str
    human_id: str
    tenant_id: str
    repo: str

    @classmethod
    def from_verified_webhook(
        cls,
        *,
        body: bytes,
        event_type: str,
        resolved,
        sender: dict,
        tenant_id: str,
        repo: str,
    ):
        # Called after signature and tenant/sender resolution in github/handler.
        if (
            resolved.user_kind != "human"
            or sender.get("type") != "User"
            or not resolved.user_id
            or not tenant_id
            or not repo
        ):
            raise AuthorityProvisionError("human authorization required")
        digest = hashlib.sha256(event_type.encode() + b"\0" + body).hexdigest()
        return cls(f"github-event:{digest}", resolved.user_id, tenant_id, repo)


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _key(pk: str, sk: str) -> dict:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _read(client, table: str, pk: str, sk: str) -> dict | None:
    return client.get_item(TableName=table, Key=_key(pk, sk), ConsistentRead=True).get(
        "Item"
    )


def _expiry_live(authority: dict, now: datetime) -> bool:
    try:
        value = authority["expires_at"]["S"]
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return parsed > now
    except (KeyError, TypeError, ValueError):
        return False


def provision_human_dispatch(
    *,
    envelope: dict,
    event: VerifiedHumanEvent,
    client=None,
    now: datetime | None = None,
) -> dict:
    """Return the final envelope only after protected dispatch commits.

    The schema is shared with gateway BootstrapStore and covered by a contract
    test that bootstraps these records, rather than by parallel schema fixtures.
    No signer or invocation credential is delivered in the queue message.
    """
    table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
    if not table:
        raise AuthorityProvisionError("authority table is not configured")
    if (
        envelope.get("tenant_id") != event.tenant_id
        or envelope.get("source_ref", {}).get("repo") != event.repo
    ):
        raise AuthorityProvisionError("authority scope mismatch")
    now = now or datetime.now(UTC)
    ddb = client or boto3.client(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
    persona = envelope["persona"]
    invocation = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"adp:{event.tenant_id}:{event.reference_id}:{persona}"
        )
    )
    pk = f"TENANT#{event.tenant_id}"
    authority_key = f"AUTHORITY#{event.reference_id}"
    try:
        authority = _read(ddb, table, pk, authority_key)
        if authority is None:
            authority = {
                **_key(pk, authority_key),
                "status": {"S": "active"},
                "authority_kind": {"S": "github_event"},
                "human_id": {"S": event.human_id},
                "repo": {"S": event.repo},
                "created_at": {"S": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
                "expires_at": {
                    "S": (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                },
            }
            try:
                ddb.put_item(
                    TableName=table,
                    Item=authority,
                    ConditionExpression="attribute_not_exists(pk)",
                )
            except (ClientError, BotoCoreError):
                authority = _read(ddb, table, pk, authority_key)
        if (
            not authority
            or authority.get("status") != {"S": "active"}
            or authority.get("authority_kind") != {"S": "github_event"}
            or authority.get("human_id") != {"S": event.human_id}
            or authority.get("repo") != {"S": event.repo}
            or not _expiry_live(authority, now)
        ):
            raise AuthorityProvisionError("authority refused")
        prior = _read(ddb, table, f"INVOCATION#{invocation}", "DISPATCH")
        final = {
            **envelope,
            "message_id": invocation,
            "arrived_at": authority["created_at"]["S"],
        }
        if prior is not None:
            final["arrived_at"] = prior.get("arrived_at", {}).get(
                "S", final["arrived_at"]
            )
        # Root lineage is newly authorized by this actual human event. Advisory
        # correlation pointers are retained for display, not copied as authority.
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
            "flow_id": {"S": event.reference_id},
            "arrived_at": {"S": final["arrived_at"]},
            "persona": {"S": persona},
            "envelope_digest": {"S": digest},
            "issue_number": {"N": str(final["source_ref"]["issue"])},
            "installation_id": {
                "N": str(final["source_ref"].get("installation_id", 0))
            },
            "chain_depth": {"N": "0"},
        }
        dispatch_personas = {
            "developer": ["reviewer"],
            "operations": ["developer", "reviewer", "operations"],
            "aidlc": ["developer", "reviewer", "operations"],
        }.get(persona, [])
        actions = ["monitor", "dispatch"] if dispatch_personas else ["monitor"]
        grant = {
            **_key(pk, f"GRANT#{invocation}#1"),
            "grant_id": {"S": f"grant:{invocation}:1"},
            "tenant_id": {"S": event.tenant_id},
            "principal": {"S": f"{invocation}#1"},
            "authority_kind": {"S": "github_event"},
            "authority_reference_id": {"S": event.reference_id},
            "authority_human_id": {"S": event.human_id},
            "authority_org_id": {"S": event.tenant_id},
            "allowed_actions": {"SS": actions},
            "delegable_actions": {
                "SS": ["monitor", "dispatch"]
                if persona in {"operations", "aidlc"}
                else ["monitor"]
            },
            "target_relationships": {"SS": ["self", "descendant"]},
            "repo_scope": {"SS": [event.repo]},
            "flow_id": {"S": event.reference_id},
            "expires_at": authority["expires_at"],
            "revocation_epoch": {"N": "1"},
            "revoked": {"BOOL": False},
            "max_dispatch_concurrency": {"N": "2"},
            "max_total_dispatches": {
                "N": "4" if persona in {"operations", "aidlc"} else "2"
            },
            # Explicit at human launch: a six-story wave can dispatch each
            # developer/reviewer, evaluation and its approved successor.
            "max_child_dispatches": {
                "N": "16" if persona in {"operations", "aidlc"} else "1"
            },
            "max_chain_depth": {"N": "8"},
            "work_item_issue": execution["issue_number"],
        }
        if dispatch_personas:
            grant["dispatch_personas"] = {"SS": dispatch_personas}
        lookup = {
            **_key(f"INVOCATION#{invocation}", "DISPATCH"),
            "tenant_id": {"S": event.tenant_id},
            "envelope_digest": {"S": digest},
            "arrived_at": {"S": final["arrived_at"]},
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
        transaction.append(
            {
                "ConditionCheck": {
                    "TableName": table,
                    "Key": _key(pk, authority_key),
                    "ConditionExpression": (
                        "#st = :active AND expires_at > :now AND human_id = :human"
                    ),
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": {
                        ":active": {"S": "active"},
                        ":now": {"S": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
                        ":human": {"S": event.human_id},
                    },
                }
            }
        )
        try:
            ddb.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError):
            if _read(ddb, table, f"INVOCATION#{invocation}", "DISPATCH") != lookup:
                raise AuthorityProvisionError(
                    "protected dispatch conflict or unavailable"
                ) from None
            current = _read(ddb, table, pk, f"EXEC#{invocation}")
            if current is None or current.get("status", {}).get("S") not in {
                "pending",
                "active",
            }:
                raise AuthorityProvisionError("dispatch is no longer active") from None
            current_grant = _read(ddb, table, pk, f"GRANT#{invocation}#1")
            current_authority = _read(ddb, table, pk, authority_key)
            if current_grant != grant or current_authority != authority:
                raise AuthorityProvisionError("dispatch authority changed") from None
        return final
    except (ClientError, BotoCoreError, KeyError, TypeError, ValueError):
        raise AuthorityProvisionError("protected authority unavailable") from None
