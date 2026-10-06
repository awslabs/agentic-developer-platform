"""Installation-owned provider authority, distinct from personal vault entries.

Every read is consistent and every consumption re-establishes installation,
human membership, policy and secret identities. Configuration selectors are not
identity proof. Only the paid broker can release an AWS session.
"""

import asyncio
import hmac
import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from src.internal.domain_current_identity import current_human_identity
from src.internal.domain_operation_store import aws_client, binding_for
from src.shared.domain_provider_contract import boundary_identity, canonical, digest, policy_identity, validate
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User

REFUSED = "domain provider authority refused"


def require(condition):
    if not condition:
        raise HTTPException(403, REFUSED)


def tables():
    environment = os.environ.get("BG_ENVIRONMENT", "")
    authority = os.environ.get("ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE", "")
    evidence = os.environ.get("ADP_DOMAIN_PROVIDER_EVIDENCE_TABLE", "")
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment))
    require(authority == f"adp-{environment}-superplane-provider-authorities")
    require(evidence == f"adp-{environment}-superplane-provider-evidence")
    return authority, evidence


def read(credential_id):
    table, _ = tables()
    try:
        item = aws_client("dynamodb").get_item(TableName=table, Key={"record_id": {"S": credential_id}}, ConsistentRead=True).get("Item")
        require(item is not None and set(item) == {"record_id", "document", "revision"})
        value = validate(json.loads(item["document"]["S"]))
        require(value["credential_id"] == credential_id and item["revision"]["S"] == digest(value))
        require(value["status"] == "active" and datetime.fromisoformat(value["expires_at"]) > datetime.now(UTC))
        return value
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError):
        raise HTTPException(403, REFUSED) from None
    except Exception:
        raise HTTPException(503, "domain provider authority unavailable") from None


async def human(db, record, subject):
    require(subject == record["subject"])
    current = await current_human_identity(db, subject=subject, adp_org_id=record["adp_org_id"])
    require(
        current.get("membership_id") == record["membership_id"]
        and current.get("subject") == subject
        and current.get("adp_org_id") == record["adp_org_id"]
        and current.get("principal_type") == "human"
        and current.get("enabled") is True
        and current.get("active") is True
    )
    user = await db.scalar(
        select(User)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .where(
            TenantMembership.id == record["membership_id"],
            TenantMembership.tenant_id == record["adp_org_id"],
            TenantMembership.revoked_at.is_(None),
            User.org_id == record["adp_org_id"],
        )
        .execution_options(populate_existing=True)
    )
    require(user is not None and user.id == record["user_id"] and user.user_kind == "human" and not user.is_shadow)


async def resolve(db, sm, credential_id, *, subject, adp_org_id=None, org_id=None, workspace_id=None, service=None, label=None):
    record = await asyncio.to_thread(read, credential_id)
    for key, expected in (("adp_org_id", adp_org_id), ("org_id", org_id), ("workspace_id", workspace_id), ("service", service), ("label", label)):
        require(expected is None or record[key] == expected)
    require(record["account_id"] == os.environ.get("ADP_DOMAIN_PROVIDER_ACCOUNT_ID"))
    binding = binding_for("superplane", record["org_id"])
    require(binding.adp_org_id == record["adp_org_id"] and digest(asdict(binding)) == record["binding_sha256"])
    await asyncio.to_thread(current_registrations, record, binding)
    from src.domain_proxy.superplane import registration, route_bucket

    require(route_bucket() == f"adp-terraform-state-{record['account_id']}")
    installed = await asyncio.to_thread(registration, fresh=True)
    require(installed.get("installation_id") == record["installation_id"] and installed.get("namespace") == binding.worker_namespace)
    await human(db, record, subject)
    try:
        iam = aws_client("iam")
        role_id, policy = await asyncio.to_thread(policy_identity, iam, record["role_arn"], record["managed_policy_arns"])
        child = await asyncio.to_thread(boundary_identity, iam, record["child_boundary_arn"])
        require((role_id, policy, child) == (record["role_id"], record["policy_sha256"], record["child_boundary_sha256"]))
        require(await asyncio.to_thread(sm.current_version_id, record["secret_arn"]) == record["secret_version"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "domain provider identity unavailable") from None
    # All preceding I/O is untrusted time: a revoke must not survive it.
    require(await asyncio.to_thread(read, credential_id) == record)
    return record


def current_registrations(record, binding):
    from src.auth.agent_registry import get_agent_registry_service

    registry = get_agent_registry_service()
    environment = os.environ.get("BG_ENVIRONMENT", "")
    for registry_id, suffix, scopes in (
        (binding.producer_registry_id, "api-producer", {"domain:operation-producer"}),
        (binding.worker_registry_id, "domain-worker", {"domain:operation-executor", "domain:operation-recovery"}),
    ):
        arn = f"arn:aws:iam::{record['account_id']}:role/adp-{environment}-superplane-{suffix}"
        entry = registry.get_current_agent(registry_id, arn)
        require(
            entry is not None
            and entry.get("owner") == "webhook-terraform-domain-operations-v1"
            and entry.get("org_id") == record["adp_org_id"]
            and entry.get("domain_org_id") == record["org_id"]
            and entry.get("scope") == "internal"
            and set(entry.get("credential_scopes", [])) == scopes
        )


async def material(sm, record):
    try:
        raw, version = await asyncio.to_thread(sm.get_secret_at_version, record["secret_arn"], record["secret_version"])
        value = json.loads(raw)
        require(version == record["secret_version"] and isinstance(value, dict) and set(value) == {"role_arn", "account_id", "external_id"})
        require(value["role_arn"] == record["role_arn"] and value["account_id"] == record["account_id"])
        require(isinstance(value["external_id"], str) and 16 <= len(value["external_id"]) <= 255)
        require(await asyncio.to_thread(sm.current_version_id, record["secret_arn"]) == version)
        return value
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "domain provider material unavailable") from None


def evidence_key(record, report_digest):
    return f"{record['credential_id']}/{record['generation']}/{record['secret_version']}/{report_digest}"


def reading_digest(reading):
    from src.auth.vault_evidence import validation_digest

    return validation_digest(
        **{key: reading[key] for key in ("credential_valid", "permissions_sufficient", "quota_available", "observed_capacity", "detail")}
    )


def read_report(record, claimed):
    _, table = tables()
    response = aws_client("dynamodb").query(
        TableName=table,
        KeyConditionExpression="record_id = :key",
        ExpressionAttributeValues={":key": {"S": evidence_key(record, claimed or "current")}},
        ConsistentRead=True,
        ScanIndexForward=False,
        Limit=1,
    )
    rows = response.get("Items", [])
    require(len(rows) == 1)
    item = rows[0]
    reading = json.loads(item["document"]["S"])
    checked = datetime.fromisoformat(item["observed_at"]["S"])
    require(checked.tzinfo is not None and datetime.now(UTC) - timedelta(hours=6) <= checked <= datetime.now(UTC))
    require(item["authority_revision"]["S"] == digest(record) and (claimed is None or hmac.compare_digest(reading_digest(reading), claimed)))
    if claimed is None:
        require(all(reading.get(key) is True for key in ("credential_valid", "permissions_sufficient", "quota_available")))
    return checked


async def evidence(db, sm, **kw):
    from src.auth.vault_evidence import VaultCredentialEvidence

    claimed = kw.pop("report_digest")
    subject = kw.pop("principal")
    try:
        record = await resolve(db, sm, subject=subject, **kw)
        checked = await asyncio.to_thread(read_report, record, claimed) if claimed is not None else None
        require(await resolve(db, sm, subject=subject, **kw) == record)
        return VaultCredentialEvidence(
            org_id=record["org_id"],
            workspace_id=record["workspace_id"],
            credential_id=record["credential_id"],
            service=record["service"],
            label=record["label"],
            owner_principal="domain_app:superplane/" + record["installation_id"],
            owner_scope="domain_app",
            delegated_to_workspaces=frozenset({record["workspace_id"]}),
            current_version_id=record["secret_version"],
            expires_at=datetime.fromisoformat(record["expires_at"]),
            attested_report_digest=claimed,
            report_checked_at=checked,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            return None
        raise
    except Exception:
        raise HTTPException(503, "domain provider evidence unavailable") from None


def write_report(record, reading):
    authority, evidence_table = tables()
    report = {key: getattr(reading, key) for key in ("credential_valid", "permissions_sufficient", "quota_available", "observed_capacity", "detail")}
    require(reading.provider_account_id == record["account_id"])
    observed = reading.checked_at
    require(observed.tzinfo is not None and datetime.now(UTC) - timedelta(hours=6) <= observed <= datetime.now(UTC))
    report_digest = reading_digest(report)
    item = {
        "record_id": {"S": evidence_key(record, report_digest)},
        "observed_at": {"S": observed.isoformat()},
        "document": {"S": canonical(report)},
        "authority_revision": {"S": digest(record)},
    }
    latest = {**item, "record_id": {"S": evidence_key(record, "current")}}
    aws_client("dynamodb").transact_write_items(
        TransactItems=[
            {
                "ConditionCheck": {
                    "TableName": authority,
                    "Key": {"record_id": {"S": record["credential_id"]}},
                    "ConditionExpression": "revision = :revision",
                    "ExpressionAttributeValues": {":revision": {"S": digest(record)}},
                }
            },
            {"Put": {"TableName": evidence_table, "Item": item, "ConditionExpression": "attribute_not_exists(record_id)"}},
            {"Put": {"TableName": evidence_table, "Item": latest, "ConditionExpression": "attribute_not_exists(record_id)"}},
        ]
    )
    return {**report, "checked_at": observed.isoformat()}


async def validate_provider(db, sm, credential_id, *, subject, adp_org_id):
    from src.auth.provider_validation import AwsEc2Validator, AwsValidationProfile, ValidationUnavailableError

    args = {"subject": subject, "adp_org_id": adp_org_id}
    record = await resolve(db, sm, credential_id, **args)
    require(datetime.fromisoformat(record["expires_at"]) > datetime.now(UTC) + timedelta(seconds=900))
    secret = await material(sm, record)
    profile = AwsValidationProfile.model_validate(record["validation_profile"])
    require(profile.region == record["region"])
    require(await resolve(db, sm, credential_id, **args) == record)
    try:
        reading = await asyncio.to_thread(
            AwsEc2Validator(profile).validate,
            canonical(secret),
            credential_type="aws_role",
            user_id=record["user_id"],
            label=record["label"],
            expected_role_id=record["role_id"],
            agent_id="superplane-provider-validation",
        )
    except ValidationUnavailableError:
        raise HTTPException(503, "domain provider validation unavailable") from None
    require(await resolve(db, sm, credential_id, **args) == record)
    try:
        report = await asyncio.to_thread(write_report, record, reading)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(403, REFUSED) from None
    require(await resolve(db, sm, credential_id, **args) == record)
    return report
