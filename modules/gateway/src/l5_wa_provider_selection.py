"""Read and pin explicit source-only resources through a Product owner's connection."""

import hashlib
import json
import os
import re

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import verified_connection_evidence
from src.l5_backend_well_architected import WellArchitectedService
from src.products.configuration import RecordRef
from src.products.models import ProductSourceBinding
from src.shared.models.vault import UserCredential


class ProviderSelection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_ref: RecordRef
    region: str = Field(pattern=r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")
    execution_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    source_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _unavailable(code, message):
    return HTTPException(409, detail={"code": code, "message": message})


def read_resources(provider, selection, account):
    """No discovery or writes: inspect only the two explicitly selected resources."""
    role_arn, bucket, region = selection.execution_role_arn, selection.source_bucket, selection.region
    iam = provider.client("iam", region_name=region)
    role = iam.get_role(RoleName=role_arn.rsplit("/", 1)[-1])["Role"]
    statements = role["AssumeRolePolicyDocument"]["Statement"]
    statements = [statements] if isinstance(statements, dict) else statements
    if role.get("Arn") != role_arn or not role.get("RoleId") or not isinstance(statements, list) or not statements:
        raise ValueError("execution_role_identity_unverified")
    for statement in statements:
        if not isinstance(statement, dict):
            raise ValueError("source_only_trust_unverified")
        conditions = {"StringEquals": {"aws:SourceAccount": account}}
        if "ArnLike" in (statement.get("Condition") or {}):
            conditions["ArnLike"] = {"aws:SourceArn": f"arn:aws:wellarchitected:{region}:{account}:agent-profile/*"}
        actions, principal = statement.get("Action"), statement.get("Principal")
        if (
            statement.get("Effect") != "Allow"
            or set(statement) - {"Sid", "Effect", "Action", "Principal", "Condition"}
            or actions not in ("sts:AssumeRole", ["sts:AssumeRole"])
            or principal not in ({"Service": "wellarchitected.amazonaws.com"}, {"Service": ["wellarchitected.amazonaws.com"]})
            or statement.get("Condition") != conditions
        ):
            raise ValueError("source_only_trust_unverified")
    resource = f"arn:aws:s3:::{bucket}/l5/well_architected/work/*"
    simulation = iam.simulate_principal_policy(PolicySourceArn=role_arn, ActionNames=["s3:GetObjectVersion"], ResourceArns=[resource])
    evaluations = simulation.get("EvaluationResults") or []
    if (
        len(evaluations) != 1
        or simulation.get("IsTruncated")
        or evaluations[0].get("EvalActionName") != "s3:GetObjectVersion"
        or evaluations[0].get("EvalResourceName") != resource
        or evaluations[0].get("EvalDecision") != "allowed"
        or evaluations[0].get("MissingContextValues")
    ):
        raise ValueError("versioned_source_read_unverified")
    s3 = provider.client("s3", region_name=region)
    target = {"Bucket": bucket, "ExpectedBucketOwner": account}
    location = s3.get_bucket_location(**target).get("LocationConstraint") or "us-east-1"
    location = "eu-west-1" if location == "EU" else location
    versioning = s3.get_bucket_versioning(**target).get("Status")
    privacy = s3.get_public_access_block(**target).get("PublicAccessBlockConfiguration")
    if (
        location != region
        or versioning != "Enabled"
        or privacy
        != {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
    ):
        raise ValueError("private_versioned_source_store_unverified")
    return {
        "role": {
            "arn": role_arn,
            "role_id": role["RoleId"],
            "trust": role["AssumeRolePolicyDocument"],
            "source_resource": resource,
            "source_read": "allowed",
        },
        "store": {"bucket": bucket, "account_id": account, "region": location, "versioning": versioning, "public_access_block": privacy},
    }


async def provider_selection(backend, project_id, selection, *, service_grant=None):
    tenant_id, owner_id = backend.actor.org_id, backend.actor.user_id
    if service_grant is not None:
        from src.auth.l5_schedule_scope import schedule_product_scope
        from src.l5_service_identity import worker_id

        current = await schedule_product_scope(
            backend.db, backend.actor, service_grant.id, service_grant.product_id, project_id, "credential:session"
        )
        if current is None or current.service_id != worker_id("well_architected"):
            raise HTTPException(404, detail={"code": "not_found"})
        tenant_id, owner_id = current.tenant_id, current.created_by
    connection = selection.connection_ref.model_dump()
    if (backend.actor.account_type != "human" and service_grant is None) or connection.get("record_type") != "connection":
        raise HTTPException(404, detail={"code": "not_found"})
    if connection["tenant_id"] != tenant_id or connection["project_id"] != project_id:
        raise HTTPException(404, detail={"code": "not_found"})
    if selection.source_bucket != os.environ.get("L5_ARCHIVE_BUCKET"):
        raise _unavailable("source_store_unavailable", "Select the private archive bucket configured for this installed L5 runtime.")
    credential = await backend.db.get(UserCredential, connection["record_id"], populate_existing=True)
    if (
        credential is None
        or credential.user_id != owner_id
        or credential.org_id != tenant_id
        or credential.service != "aws"
        or credential.credential_type != "aws_role"
        or credential.id != connection["source_connection_id"]
        or credential.aws_verified_version_id != connection["record_revision"]
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    connection_evidence = verified_connection_evidence(credential)
    account = (credential.scopes or {}).get("account_id")
    if (
        not isinstance(account, str)
        or not re.fullmatch(r"[0-9]{12}", account)
        or not selection.execution_role_arn.startswith(f"arn:aws:iam::{account}:role/")
        or selection.execution_role_arn == (credential.scopes or {}).get("role_arn")
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    bindings = (
        await backend.db.scalars(
            select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == tenant_id,
                ProductSourceBinding.project_id == project_id,
                ProductSourceBinding.credential_id == credential.id,
                ProductSourceBinding.kind == "environment",
                ProductSourceBinding.provider == "aws",
                ProductSourceBinding.namespace == account,
                ProductSourceBinding.native_id == selection.region,
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status == "healthy",
            )
        )
    ).all()
    if not any((binding.observed or {}).get("source_revision") == connection["record_revision"] for binding in bindings):
        raise _unavailable("environment_selection_required", "Select and verify this connection's environment on the Product first.")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["iam:GetRole", "iam:SimulatePrincipalPolicy"], "Resource": selection.execution_role_arn},
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock"],
                "Resource": f"arn:aws:s3:::{selection.source_bucket}",
            },
        ],
    }
    provider, identity = await WellArchitectedService(backend).selected_provider(
        credential,
        {"connection_ref": connection, "region": selection.region},
        session_policy=policy,
        **({"service_grant": service_grant} if service_grant is not None else {}),
    )
    try:
        observations = await run_in_threadpool(read_resources, provider, selection, account)
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError) as error:
        raise _unavailable(
            "source_resources_unqualified",
            "Verify the separate source-only role, its versioned source-read permission, and the private versioned bucket in the selected Region.",
        ) from error
    if identity["account_id"] != account or identity["region"] != selection.region:
        raise HTTPException(404, detail={"code": "not_found"})
    if (
        service_grant is not None
        and await schedule_product_scope(backend.db, backend.actor, service_grant.id, service_grant.product_id, project_id, "credential:session")
        is None
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    await backend.db.refresh(credential)
    for binding in bindings:
        await backend.db.refresh(binding)
    if (
        credential.user_id != owner_id
        or credential.org_id != tenant_id
        or credential.aws_verified_version_id != connection["record_revision"]
        or verified_connection_evidence(credential) != connection_evidence
        or not any(
            binding.status == "healthy"
            and binding.unlinked_at is None
            and binding.project_id == project_id
            and binding.credential_id == credential.id
            and (binding.observed or {}).get("source_revision") == connection["record_revision"]
            for binding in bindings
        )
    ):
        raise _unavailable("provider_selection_stale", "The Product connection or environment changed during validation. Refresh the selection.")
    result = {"provider_contacted": True, "native_qualified": False}
    for field, kind, record_id, observation in (
        ("execution_role_ref", "role", selection.execution_role_arn, observations["role"]),
        ("source_store_ref", "artifact_store", selection.source_bucket, observations["store"]),
    ):
        revision = hashlib.sha256(
            json.dumps(
                {"connection": connection, "observation": observation},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        result[field] = {**connection, "record_type": kind, "record_id": record_id, "record_revision": revision}
    return result


async def verify_provider_references(backend, project_id, settings, *, service_grant=None):
    references = stored_provider_references(settings)
    if not references:
        return []
    review = settings["well_architected"]
    selection = ProviderSelection.model_validate(
        {
            "connection_ref": settings["connection_ref"],
            "region": settings["region"],
            "execution_role_arn": review["execution_role_ref"]["record_id"],
            "source_bucket": review["source_store_ref"]["record_id"],
        }
    )
    observed = await provider_selection(backend, project_id, selection, **({"service_grant": service_grant} if service_grant is not None else {}))
    fields = ("execution_role_ref", "source_store_ref")
    if any(review[field] != observed[field] for field in fields):
        raise _unavailable("provider_selection_stale", "Validate the selected provider resources again and explicitly save their current references.")
    return [observed[field] for field in fields]


def stored_provider_references(settings):
    """Stored server-validated choices remain readable while a provider needs repair.

    This only authorizes metadata. Saves perform fresh provider validation, and
    setup/work admission independently qualifies the selected role and Profile.
    """
    review = settings.get("well_architected") or {}
    if not isinstance(review, dict):
        raise _unavailable("provider_selection_required", "Provide source-only review settings and validate both provider resources.")
    fields = ("execution_role_ref", "source_store_ref")
    if not any(review.get(field) for field in fields):
        return []
    try:
        ProviderSelection.model_validate(
            {
                "connection_ref": settings["connection_ref"],
                "region": settings["region"],
                "execution_role_arn": review["execution_role_ref"]["record_id"],
                "source_bucket": review["source_store_ref"]["record_id"],
            }
        )
        for field, kind in (("execution_role_ref", "role"), ("source_store_ref", "artifact_store")):
            reference = RecordRef.model_validate(review[field]).model_dump()
            if (
                reference["record_type"] != kind
                or not re.fullmatch(r"[a-f0-9]{64}", reference["record_revision"])
                or any(reference[key] != settings["connection_ref"][key] for key in ("tenant_id", "project_id", "source_connection_id"))
            ):
                raise ValueError("provider reference scope changed")
    except (ValueError, KeyError, TypeError):
        raise _unavailable(
            "provider_selection_required",
            "Validate both source-only provider resources before saving their returned references.",
        ) from None
    return [review[field] for field in fields]


def add_provider_resources(available, references):
    for item in available["items"]:
        item["resources"].extend(reference for reference in references if reference["source_connection_id"] == item["connection_ref"]["id"])
    return available


def create_provider_selection_router(backend_dependency):
    router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")

    @router.post("/configuration/provider-selection", operation_id="validateWellArchitectedProviderSelection")
    async def validate_selection(
        product_id: str, project_id: str, service_id: str, selection: ProviderSelection, response: Response, backend=Depends(backend_dependency)
    ):
        if service_id != "well_architected":
            raise HTTPException(404, detail={"code": "not_found"})
        await backend.profile(product_id, project_id)
        response.headers["Cache-Control"] = "no-store"
        return await provider_selection(backend, project_id, selection)

    return router
