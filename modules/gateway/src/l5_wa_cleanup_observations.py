"""Owner-authorized absence readback from exact retained WA creation/cleanup intent."""

import json
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError
from fastapi import HTTPException
from l5_services.api.bindings import _intent_hash
from l5_services.models.binding import Binding
from l5_services.models.setup import BindingOperation, SetupIntent
from sqlalchemy import select

from src.l5_wa_observations import read_authority, run_in_threadpool


async def authority(backend, product_id, project_id, run_id):
    values = await read_authority(backend, product_id, project_id, run_id, cleaned=True)
    run, version, credential, grant, result, context, fence = values
    binding = await backend.db.get(Binding, (run.tenant_id, run.binding_id), populate_existing=True)
    receipt = binding.creation_receipt
    creation = (
        await backend.db.get(SetupIntent, (run.tenant_id, receipt.get("operation_id")), populate_existing=True) if isinstance(receipt, dict) else None
    )
    intents = (
        await backend.db.scalars(
            select(SetupIntent)
            .where(
                SetupIntent.tenant_id == run.tenant_id,
                SetupIntent.product_id == run.product_id,
                SetupIntent.project_id == run.project_id,
                SetupIntent.service_id == "well_architected",
                SetupIntent.configuration_id == run.configuration_id,
                SetupIntent.binding_generation == binding.generation,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    cleanup = intents[0] if len(intents) == 1 else None
    operation = await backend.db.get(BindingOperation, (run.tenant_id, cleanup.operation_id), populate_existing=True) if cleanup else None
    if (
        creation is None
        or cleanup is None
        or operation is None
        or creation.request.get("action") != "create"
        or creation.configuration_id != run.configuration_id
        or creation.scope != binding.scope
        or creation.intent_hash != _intent_hash(creation)
        or receipt.get("intent_hash") != creation.intent_hash
        or receipt.get("native_resource_ref") != binding.native_resource_ref
        or receipt.get("provider_identity") != binding.provider_identity
        or cleanup.intent_hash != _intent_hash(cleanup)
        or cleanup.scope != binding.scope
        or cleanup.owned_resource_receipt != receipt
        or cleanup.request
        != {"action": "cleanup", "configuration_revision": version.revision, "binding_id": binding.binding_id, "owned_cleanup_confirmed": True}
        or operation.binding_id != binding.binding_id
        or operation.action != "cleanup"
        or operation.state != "succeeded"
        or operation.cleanup_receipt
        != {
            "schema_version": 1,
            "source": "installed-worker",
            "absent": True,
            "operation_id": cleanup.operation_id,
            "intent_hash": cleanup.intent_hash,
            "creation_receipt": receipt,
            "native_resource_ref": binding.native_resource_ref,
            "provider_identity": binding.provider_identity,
        }
    ):
        raise HTTPException(409, detail={"code": "owned_cleanup_receipt_unverified"})
    projection = {
        "creation_operation_id": creation.operation_id,
        "cleanup_operation_id": cleanup.operation_id,
        "cleanup_intent_sha256": cleanup.intent_hash,
        "native_profile_arn": binding.native_resource_ref["record_id"],
    }
    return values, projection, fence + json.dumps(projection, sort_keys=True)


def observe_absence(provider, region, profile):
    try:
        provider.client("wellarchitected", region_name=region).get_agent_profile(profileArn=profile)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return True
        raise
    return False


async def read_cleanup_observations(service, product_id, project_id, run_id):
    from src.l5_wa_runtime_observations import read_runtime

    backend = service.backend
    arguments = (backend, product_id, project_id, run_id)
    values, projection, fence = await authority(*arguments)
    run, version, credential, grant, result, context, _ = values
    runtime = await read_runtime(backend.db, run, context)
    expiry = min(grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC), datetime.now(UTC) + timedelta(minutes=15))
    condition = {"DateLessThan": {"aws:CurrentTime": expiry.isoformat()}}
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["sts:GetCallerIdentity"], "Resource": "*", "Condition": condition},
            {"Effect": "Allow", "Action": ["wellarchitected:GetAgentProfile"], "Resource": projection["native_profile_arn"], "Condition": condition},
        ],
    }
    try:
        provider, identity = await service.selected_provider(credential, version.settings, session_policy=policy, audit_run_id=run_id)
        if identity != context["provider_identity"] or (await authority(*arguments))[-1] != fence:
            raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
        absent = await run_in_threadpool(observe_absence, provider, identity["region"], projection["native_profile_arn"])
        if (await authority(*arguments))[-1] != fence or await read_runtime(backend.db, run, context) != runtime:
            raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
        if not absent:
            raise HTTPException(409, detail={"code": "owned_resource_present"})
        return {
            "schema_version": 1,
            **context,
            "runtime": runtime,
            "cleanup": {**projection, "owned_absent": True, "source": "independent-native-read", "result_retained": True},
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, detail={"code": "owned_cleanup_observation_unavailable"}) from error
