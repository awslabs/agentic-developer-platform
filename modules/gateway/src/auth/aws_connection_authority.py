"""Server-owned, version-bound authority for personal AWS connections."""

import hashlib
import json
import re
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from src.shared.aws_role_trust import validate_customer_role
from src.shared.models.vault import UserCredential
from src.shared.services.routing_probe import ROUTING_REASON_PROBE_INCONCLUSIVE

_VERDICT_KEYS = {"status", "verified_at", "routing_capable", "routing_reason"}
_ROLE = re.compile(r"arn:(aws(?:-[a-z]+)*):iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})")


def connection_conflict(message="The AWS connection changed. Verify it again."):
    return HTTPException(409, detail={"error": "connection_not_verified", "message": message})


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require_active_connection(credential):
    expiry = credential.expires_at
    # PostgreSQL returns an aware timestamp; SQLite drops the declared timezone.
    if expiry is not None and _utc(expiry) <= datetime.now(UTC):
        raise connection_conflict("The AWS connection has expired.")


def connection_binding(credential):
    """Bind all mutable authority fields, excluding the verification result itself."""
    expiry = credential.expires_at
    fields = {
        name: getattr(credential, name)
        for name in ("id", "org_id", "user_id", "team_id", "domain_app_id", "service", "credential_type", "label", "secret_arn", "strict")
    }
    fields["aws_external_id"] = credential.aws_external_id
    fields["expires_at"] = _utc(expiry).isoformat() if expiry is not None else None
    fields["scopes"] = {key: value for key, value in (credential.scopes or {}).items() if key not in _VERDICT_KEYS}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def invalidate_connection(credential):
    credential.aws_verified_at = None
    credential.aws_verified_version_id = None
    credential.aws_verified_binding = None
    credential.aws_verification_attempt = None
    scopes = dict(credential.scopes or {})
    scopes["status"] = "pending"
    scopes.pop("verified_at", None)
    scopes["routing_capable"] = False
    scopes["routing_reason"] = ROUTING_REASON_PROBE_INCONCLUSIVE
    credential.scopes = scopes


def verified_connection_evidence(credential):
    require_active_connection(credential)
    if (
        not credential.aws_external_id
        or (credential.scopes or {}).get("status") != "verified"
        or credential.aws_verified_at is None
        or not credential.aws_verification_attempt
        or not credential.aws_verified_version_id
        or credential.aws_verified_binding != connection_binding(credential)
    ):
        raise connection_conflict()
    return (
        credential.aws_verification_attempt,
        credential.aws_verified_version_id,
        credential.aws_verified_binding,
        _utc(credential.aws_verified_at),
    )


async def owned_aws_connection(db, credential_id, user_id, org_id, *, lock=False):
    query = (
        select(UserCredential)
        .where(
            UserCredential.id == credential_id,
            UserCredential.user_id == user_id,
            UserCredential.org_id == org_id,
            UserCredential.service == "aws",
            UserCredential.credential_type == "aws_role",
        )
        .execution_options(populate_existing=True)
    )
    credential = await db.scalar(query.with_for_update() if lock else query)
    if credential is None:
        raise HTTPException(404, detail={"error": "not_found", "message": "AWS connection not found"})
    return credential


def connection_material(secret, scopes, credential):
    """Reject caller-authored account metadata inconsistent with the assumed role."""
    role = secret.get("role_arn") if isinstance(secret, dict) else None
    match = _ROLE.fullmatch(role) if isinstance(role, str) else None
    account = secret.get("account_id") if isinstance(secret, dict) else None
    if match is None or match[2] != account or scopes.get("account_id") != account or scopes.get("role_arn") != role:
        raise connection_conflict("The AWS role does not match the connection's account metadata.")
    external_id = secret.get("external_id")
    if not credential.aws_external_id:
        raise connection_conflict("Disconnect and register this legacy connection again to establish role ownership.")
    if external_id != credential.aws_external_id:
        raise connection_conflict("The AWS connection contains invalid trust metadata.")
    try:
        validate_customer_role(role)
    except ValueError:
        raise connection_conflict("The AWS role cannot be used for this connection.") from None
    return role, external_id, account


def require_assumed_identity(result, role_arn):
    role = _ROLE.fullmatch(role_arn)
    identity = getattr(result, "assumed_role_arn", None)
    prefix = f"arn:{role[1]}:sts::{role[2]}:assumed-role/{role[3].rsplit('/', 1)[-1]}/"
    if not isinstance(identity, str) or not identity.startswith(prefix) or not identity[len(prefix) :] or "/" in identity[len(prefix) :]:
        raise connection_conflict("AWS did not attest the expected account and role.")
