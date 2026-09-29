"""Seed provider-verified ownership for broker identity regression tests."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from src.auth.aws_connection_authority import connection_binding
from src.shared.models.vault import UserCredential


async def verified_role_material(db, sm, raw):
    material = json.loads(raw)
    material["account_id"] = material["role_arn"].split(":")[4]
    material.setdefault("external_id", "fixture-server-issued-id")
    credentials = (await db.scalars(select(UserCredential).where(UserCredential.credential_type == "aws_role"))).all()
    for credential in credentials:
        credential.aws_external_id = material["external_id"]
        credential.scopes = {
            **(credential.scopes or {}),
            "account_id": material["account_id"],
            "role_arn": material["role_arn"],
            "source": "imported_role",
            "status": "verified",
        }
        credential.aws_verified_at = datetime.now(UTC)
        credential.aws_verified_version_id = "ownership-version"
        credential.aws_verification_attempt = "ownership-attempt"
        await db.flush()
        credential.aws_verified_binding = connection_binding(credential)
    await db.commit()
    sm.current_version_id.return_value = "ownership-version"
    sm.get_secret_at_version.return_value = (json.dumps(material), "ownership-version")
