"""Refresh an environment through its selected, version-bound AWS connection."""

import asyncio
import json
import re
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.products.verification import OBSERVATION_LIFETIME, OBSERVATION_TIMEOUT, denied, unavailable
from src.shared.models.vault import UserCredential


async def observe_aws_environment(binding, credential, *, now=None):
    """Prove current role access; do not claim service permissions or return a session."""
    if (
        not isinstance(credential, UserCredential)
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
        or credential.service != "aws"
        or credential.credential_type != "aws_role"
    ):
        return denied()
    from src.auth.vault_routes import get_secrets_manager

    try:
        async with asyncio.timeout(OBSERVATION_TIMEOUT):
            evidence = verified_connection_evidence(credential)
            version = evidence[1]
            store = get_secrets_manager()
            if await asyncio.to_thread(store.current_version_id, credential.secret_arn) != version:
                return unavailable("connection_changed")
            payload, served = await asyncio.to_thread(store.get_secret_at_version, credential.secret_arn, version)
            if served != version:
                return unavailable("connection_changed")
            secret = json.loads(payload)
            role, external_id, account = connection_material(secret, credential.scopes or {}, credential)
            region = secret.get("default_region")
            if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
                return unavailable("connection_region_unavailable")
            if (binding.namespace, binding.native_id) != (account, region):
                return denied()
            # This is a read-only connection proof, not an L5 provider session.
            # Keep the owner-pinned tags and restrict the discarded session.
            assumed = await asyncio.to_thread(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=region,
                aws_region=region,
                user_id=credential.user_id,
                agent_id="product-source-verification",
                task_id=f"binding-{binding.id}",
                label=credential.label,
                session_policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Effect": "Deny", "NotAction": "sts:GetCallerIdentity", "Resource": "*"},
                        ],
                    }
                ),
            )
            require_assumed_identity(assumed, role)
            if (
                await asyncio.to_thread(store.current_version_id, credential.secret_arn) != version
                or verified_connection_evidence(credential) != evidence
            ):
                return unavailable("connection_changed")
            observed_at = now or datetime.now(UTC)
            return {
                "coverage": "complete",
                "status": "healthy",
                "observed": {
                    "observed_at": observed_at.isoformat(),
                    "stale_after": (observed_at + OBSERVATION_LIFETIME).isoformat(),
                    "source_revision": version,
                    "native_id": region,
                    "namespace": account,
                    "verified_environment": {
                        "provider": "aws",
                        "account_id": account,
                        "regions": [region],
                        "role_ref": role,
                        "permission_tier": "unknown",
                        "verified_at": observed_at.isoformat(),
                        "source_revision": version,
                    },
                },
            }
    except STSAssumeError as error:
        return denied() if error.code in {"AccessDenied", "AccessDeniedException"} else unavailable("aws_read_unavailable")
    except (HTTPException, BotoCoreError, ClientError, OSError, TimeoutError, ValueError, TypeError, KeyError):
        return unavailable("aws_read_unavailable")
