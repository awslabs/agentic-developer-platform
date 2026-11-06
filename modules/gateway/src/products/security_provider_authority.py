"""Read the selected provider identity using current Product-owned authority."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import boto3
from fastapi import HTTPException

from src.auth.aws_connection_authority import connection_material, require_assumed_identity
from src.internal.sts_assume_service import assume_role
from src.shared.models.vault import UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper


async def read(backend, product_id, project_id, binding_id, http):
    async def selected():
        from src.auth.native_launch_revocation import require_native_launch_session

        await require_native_launch_session(http.headers.get("Authorization", ""))
        from src.orchestration.l5_observation_scope import aware

        if backend.actor.expires_at is None or aware(backend.actor.expires_at) <= datetime.now(UTC):
            raise HTTPException(401, detail={"code": "security_provider_session_expired"})
        backend.db.expire_all()
        store, authority = await backend.binding_authority(product_id, project_id, "security")
        binding = await store.binding(binding_id)
        identity = {key: authority[key] for key in ("account_id", "role_arn", "region")}
        if (
            binding.state != "ready"
            or binding.configuration_id != authority["configuration_id"]
            or binding.connection_ref != authority["connection_ref"]
            or any(binding.provider_identity.get(key) != value for key, value in identity.items())
        ):
            raise HTTPException(409, detail={"code": "security_provider_selection_changed"})
        stable = {key: authority[key] for key in ("configuration_id", "configuration_revision", "scope", "connection_ref")}
        stable.update(
            identity=identity,
            binding_id=binding_id,
            binding_generation=binding.generation,
            binding_revision=binding.revision,
            delegation_id=authority["authorization"]["delegation_id"],
            delegation_revision=authority["authorization"]["delegation_revision"],
        )
        return identity, stable

    identity, before = await selected()
    credential = await backend.db.get(UserCredential, before["connection_ref"]["record_id"])
    helper = SecretsManagerHelper()
    try:
        async with asyncio.timeout(30):
            version = credential.aws_verified_version_id
            if await asyncio.to_thread(helper.current_version_id, credential.secret_arn) != version:
                raise ValueError("connection changed")
            payload, served = await asyncio.to_thread(helper.get_secret_at_version, credential.secret_arn, version)
            if served != version:
                raise ValueError("connection changed")
            role, external_id, account = connection_material(json.loads(payload), credential.scopes or {}, credential)
            if (role, account) != (identity["role_arn"], identity["account_id"]):
                raise ValueError("connection changed")
            assumed = await asyncio.to_thread(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=identity["region"],
                aws_region=identity["region"],
                user_id=backend.actor.user_id,
                agent_id="l5-security",
                task_id=binding_id,
                label=credential.label,
                session_policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["sts:GetCallerIdentity"],
                                "Resource": "*",
                                "Condition": {"DateLessThan": {"aws:CurrentTime": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}},
                            }
                        ],
                    }
                ),
            )
            require_assumed_identity(assumed, role)
            session = boto3.Session(
                aws_access_key_id=assumed.access_key_id,
                aws_secret_access_key=assumed.secret_access_key,
                aws_session_token=assumed.session_token,
                region_name=identity["region"],
            )
            observed = await asyncio.to_thread(session.client("sts").get_caller_identity)
            expected_prefix = f"arn:aws:sts::{account}:assumed-role/{role.rsplit('/', 1)[-1]}/"
            if observed.get("Account") != account or not observed.get("Arn", "").startswith(expected_prefix):
                raise ValueError("provider identity changed")
    except Exception:
        raise HTTPException(
            503,
            detail={
                "code": "security_provider_identity_unavailable",
                "next_action": "Verify the selected Product AWS connection and owner delegation; no assessment was submitted.",
            },
        ) from None
    _, after = await selected()
    if after != before:
        raise HTTPException(409, detail={"code": "security_provider_selection_changed"})
    return {
        "provider_identity": identity,
        "configuration_id": before["configuration_id"],
        "configuration_revision": before["configuration_revision"],
        "binding_id": binding_id,
        "observed_at": datetime.now(UTC).isoformat(),
    }
