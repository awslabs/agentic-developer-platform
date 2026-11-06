"""Offline durable Lambda readback for tests of downstream Product authority."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.api.bindings import _intent_hash
from l5_services.devops.resource_scope import ResourceSelection, policy_digest
from l5_services.models.devops_space import DevopsSpaceIntent
from l5_services.models.setup import BindingOperation, SetupIntent
from sqlalchemy import select

from src.l5_devops_resources import require_workload_refs, workload_refs
from src.products.models import ProductSourceBinding


async def seed_verified_lambda(db, binding, version):
    """Retain an exact fictional observation; exercise the real reference guard."""
    now = datetime.now(UTC)
    connection = binding.connection_ref
    identity = binding.provider_identity
    account, region = identity["account_id"], identity["region"]
    environment = {**connection, "record_type": "environment", "record_id": region}
    selected = SpaceSelection(binding.tenant_id, binding.product_id, binding.project_id, binding.binding_id, account, region)
    resource = ResourceSelection(
        account_id=account,
        region=region,
        role_arn=f"arn:aws:iam::{account}:role/offline-resource-reader",
        setup_role_arn=identity["role_arn"],
        connection_ref={**connection, "record_type": "credential"},
        workload_kind="lambda",
        resource_arn=f"arn:aws:lambda:{region}:{account}:function:offline-investigation",
        telemetry_arn=f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/offline-investigation:*",
    )
    native_id = binding.native_resource_ref["record_id"]
    # These downstream fixtures need the same original provenance as API setup;
    # a resource observation is not evidence that ADP created a customer space.
    binding.ownership = "customer_linked" if binding.ownership in {"adopted", "customer_linked"} else "adp_created"
    binding.native_resource_ref = {**connection, "record_type": "agent_space", "record_id": native_id, "record_revision": native_id}
    snapshot = {
        "version": 1,
        "role_arn": resource.role_arn,
        "trust_document": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "aidevops.amazonaws.com"},
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": account},
                        "ArnEquals": {"aws:SourceArn": f"arn:aws:aidevops:{region}:{account}:agentspace/{native_id}"},
                    },
                }
            ],
        },
        "managed_policies": [],
        "inline_policies": [
            {
                "name": "offline-read",
                "document": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {"Effect": "Allow", "Action": "lambda:GetFunction", "Resource": resource.resource_arn},
                        {"Effect": "Allow", "Action": "logs:FilterLogEvents", "Resource": resource.telemetry_arn},
                    ],
                },
            }
        ],
        "permissions_boundary": None,
        "selected_resources": {"lambda:GetFunction": [resource.resource_arn], "logs:FilterLogEvents": [resource.telemetry_arn]},
    }
    observation = {
        "status": "verifying",
        "reason": None,
        "native_space_id": native_id,
        "space_fingerprint": selected.fingerprint(),
        "account_id": account,
        "region": region,
        "role_arn": resource.role_arn,
        "resource_selection": asdict(resource),
        "scope_fingerprint": resource.fingerprint(),
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
        "effective_scope": {"trust": "selected_space", "policy_snapshot": snapshot, "policy_sha256": policy_digest(snapshot)},
        "observations": {"functions": 1},
        "association_id": "offline-association",
        "association_status": "valid",
        "association_readback": {
            "associationId": "offline-association",
            "agentSpaceId": native_id,
            "serviceId": "aws",
            "status": "valid",
            "configuration": {"aws": {"accountId": account, "accountType": "monitor", "assumableRoleArn": resource.role_arn}},
        },
    }
    receipts = (await db.scalars(select(DevopsSpaceIntent).where(DevopsSpaceIntent.tenant_id == binding.tenant_id))).all()
    receipt = next((item for item in receipts if item.selection.get("binding_id") == binding.binding_id), None)
    if receipt is None:
        receipt = DevopsSpaceIntent(tenant_id=binding.tenant_id, operation_id=str(uuid4()))
        db.add(receipt)
    receipt.selection, receipt.configuration_revision, receipt.intent_hash = asdict(selected), version.revision, version.hash
    receipt.state, receipt.native_space_id, receipt.ownership = "succeeded", native_id, binding.ownership
    receipt.scope_fingerprint, receipt.resource_observation = selected.fingerprint(), observation
    source = await db.scalar(
        select(ProductSourceBinding).where(
            ProductSourceBinding.tenant_id == binding.tenant_id,
            ProductSourceBinding.project_id == binding.project_id,
            ProductSourceBinding.credential_id == connection["record_id"],
            ProductSourceBinding.kind == "environment",
            ProductSourceBinding.provider == "aws",
            ProductSourceBinding.namespace == account,
            ProductSourceBinding.native_id == region,
        )
    )
    if source is None:
        source = ProductSourceBinding(
            tenant_id=binding.tenant_id,
            project_id=binding.project_id,
            credential_id=connection["record_id"],
            kind="environment",
            provider="aws",
            namespace=account,
            native_id=region,
        )
        db.add(source)
    source.status, source.unlinked_at = "healthy", None
    source.desired = {"source_revision": connection["record_revision"]}
    source.observed = {
        "source_revision": connection["record_revision"],
        "observed_at": now.isoformat(),
        "stale_after": (now + timedelta(hours=1)).isoformat(),
    }
    refs = workload_refs({"native_space_id": native_id, "resource_observation": observation})
    version.settings = {
        **version.settings,
        "source_refs": [environment],
        "devops": {
            **version.settings["devops"],
            "space_mode": "adopt" if binding.ownership == "customer_linked" else "create",
            "existing_space_id": native_id if binding.ownership == "customer_linked" else None,
            "accounts": [
                {
                    "account_id": account,
                    "environments": [
                        {
                            "environment_ref": environment,
                            "resource_refs": refs["resource_refs"],
                        }
                    ],
                }
            ],
        },
    }
    adopted = binding.ownership == "customer_linked"
    original = SetupIntent(
        tenant_id=binding.tenant_id,
        operation_id=receipt.operation_id,
        product_id=binding.product_id,
        project_id=binding.project_id,
        service_id="devops",
        actor_id=binding.scope["owner_id"],
        idempotency_key="offline-setup-" + receipt.operation_id,
        configuration_id=binding.configuration_id,
        scope=binding.scope,
        binding_generation=1,
        request={
            "action": "link" if adopted else "create",
            "configuration_revision": version.revision,
            **({"native_resource_ref": binding.native_resource_ref} if adopted else {}),
        },
        authorization={
            "purpose": "devops_setup",
            "control_action": "devops_setup",
            "setup_action": "link" if adopted else "create",
            "actor_id": binding.scope["owner_id"],
            "native_resource_ref": binding.native_resource_ref,
            "connection_ref": connection,
            "provider_identity": identity,
            "configuration_hash": version.hash,
        },
        intent_hash="",
    )
    original.intent_hash = _intent_hash(original)
    receipt.intent_hash = original.intent_hash
    db.add(original)
    db.add(
        BindingOperation(
            tenant_id=binding.tenant_id,
            operation_id=receipt.operation_id,
            binding_id=binding.binding_id,
            action=original.request["action"],
            state="succeeded",
            revision=1,
            gaps=[],
        )
    )
    binding.creation_receipt = (
        None
        if adopted
        else {
            "operation_id": receipt.operation_id,
            "intent_hash": original.intent_hash,
            "native_resource_ref": binding.native_resource_ref,
            "provider_identity": identity,
        }
    )
    await db.flush()
    assert await require_workload_refs(
        db,
        tenant_id=binding.tenant_id,
        project_id=binding.project_id,
        owner_id=binding.scope["owner_id"],
        settings=version.settings,
        binding_id=binding.binding_id,
    )
    return refs
