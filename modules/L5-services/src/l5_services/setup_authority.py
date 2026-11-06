"""Resolve explicit renewed consent separately from immutable setup identity."""

from datetime import UTC, datetime
from hashlib import sha256

import rfc8785
from fastapi import HTTPException
from sqlalchemy import select

from l5_services.models.setup import SetupAuthorityContinuation, SetupWork


def digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


def continuation_error(
    code="setup_continuation_invalid",
    message="The retained setup authority changed. Read the operation and its consent history.",
    status=409,
):
    return HTTPException(status, detail={"code": code, "message": message})


async def continuation_rows(session, intent):
    return (
        await session.scalars(
            select(SetupAuthorityContinuation)
            .where(
                SetupAuthorityContinuation.tenant_id == intent.tenant_id,
                SetupAuthorityContinuation.operation_id == intent.operation_id,
            )
            .order_by(SetupAuthorityContinuation.sequence)
            .execution_options(populate_existing=True)
        )
    ).all()


async def setup_authorization(session, intent, *, require_active=True, now=None):
    """Return effective authorization without modifying the ORM intent or its hash.

    Issuance locks configuration -> original work -> operation -> grants. Workers
    claim work before reading this chain, so issuance cannot race an active claim.
    Broker checks current membership, grant revocation and credential scope too.
    """
    if (
        intent.service_id != "devops"
        or intent.authorization.get("purpose") != "devops_setup"
    ):
        return intent.authorization
    rows = await continuation_rows(session, intent)
    if not rows:
        return intent.authorization
    original_hash = digest(
        {
            key: getattr(intent, key)
            for key in (
                "operation_id",
                "scope",
                "configuration_id",
                "request",
                "authorization",
                "binding_generation",
            )
        }
    )
    if original_hash != intent.intent_hash:
        raise continuation_error()
    authority, previous_hash, original_scope = (
        intent.authorization,
        None,
        rows[0].receipt.get("scope"),
    )
    for sequence, row in enumerate(rows, 1):
        receipt = row.receipt
        if (
            row.sequence != sequence
            or digest(receipt) != row.receipt_hash
            or receipt.get("schema_version") != 1
            or receipt.get("sequence") != sequence
            or receipt.get("operation_id") != intent.operation_id
            or receipt.get("intent_hash") != intent.intent_hash
            or receipt.get("previous_receipt_hash") != previous_hash
            or receipt.get("prior_authorization") != authority
            or receipt.get("scope") != original_scope
            or receipt.get("scope_digest") != digest(receipt.get("scope"))
            or any(
                receipt.get("scope", {}).get(key) != getattr(intent, key)
                for key in ("tenant_id", "product_id", "project_id")
            )
            or receipt.get("scope", {}).get("configuration_id")
            != intent.configuration_id
            or receipt.get("scope", {}).get("configuration_revision")
            != intent.request["configuration_revision"]
            or receipt.get("scope", {}).get("created_by") != intent.actor_id
            or receipt.get("work_generation") != 1
            or receipt.get("work_fence") != 0
        ):
            raise continuation_error()
        renewed = receipt.get("authorization", {})
        changed_fields = {"delegation_id", "delegation_revision", "authorized_at"}
        if (
            set(renewed) != set(intent.authorization)
            or any(
                renewed.get(key) != value
                for key, value in intent.authorization.items()
                if key not in changed_fields
            )
            or not renewed.get("delegation_id")
            or renewed["delegation_id"] == authority["delegation_id"]
            or type(renewed.get("delegation_revision")) is not int
            or renewed["delegation_revision"] < 1
        ):
            raise continuation_error()
        work = await session.get(
            SetupWork,
            (intent.tenant_id, receipt.get("work_id")),
            populate_existing=True,
        )
        if (
            work is None
            or work.operation_id != intent.operation_id
            or work.generation != receipt["work_generation"]
        ):
            raise continuation_error()
        try:
            expiry = datetime.fromisoformat(receipt["expires_at"])
            if expiry.tzinfo is None:
                raise ValueError("naive expiry")
        except (KeyError, ValueError, TypeError) as error:
            raise continuation_error() from error
        authority, previous_hash = renewed, row.receipt_hash
    if require_active and expiry <= (now or datetime.now(UTC)):
        raise continuation_error(
            "setup_continuation_expired",
            "Setup consent expired. Inspect the retained operation; explicit consent continuation is available only before any worker claim.",
            403,
        )
    return authority
