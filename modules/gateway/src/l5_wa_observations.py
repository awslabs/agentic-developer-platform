"""Current Product authority for independent, read-only native WA observations."""

import base64
import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from l5_services.adapters.well_architected.qualification import _pages
from l5_services.adapters.well_architected.source_evidence import archive_location, read_source_files
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.evidence import AssessmentResult
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import verified_connection_evidence
from src.l5_service_identity import worker_id
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


async def read_prerequisites(backend, product_id, project_id):
    _, verified = await backend.binding_authority(product_id, project_id, "well_architected")
    configuration, version = await backend.current(product_id, project_id, "well_architected")
    grant = await backend.db.get(CredentialServiceDelegation, verified["authorization"]["delegation_id"], populate_existing=True)
    credential = await backend.db.get(UserCredential, verified["connection_ref"]["record_id"], populate_existing=True)
    if (
        version.revision != verified["configuration_revision"]
        or configuration.handoff_state != "linked"
        or version.settings.get("enabled") is not True
        or version.settings.get("well_architected", {}).get("review_mode") != "source_only"
        or grant is None
        or grant.parent_delegation_id is not None
        or grant.revoked_at is not None
        or grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC) <= datetime.now(UTC)
        or not {"product:read", "source:pin", "source:read", "credential:session"} <= set(grant.operations)
        or "architecture_review" not in grant.capabilities
        or any(source not in grant.sources for source in version.settings.get("source_refs", []))
        or credential is None
        or credential.agent_consent is not True
    ):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    verified_connection_evidence(credential)
    return {
        "schema_version": 1,
        "configuration": view(configuration, version),
        "provider_identity": {field: verified[field] for field in ("account_id", "role_arn", "region")},
        "status": "ready_for_setup",
    }


async def read_authority(backend, product_id, project_id, run_id, *, cleaned=False):
    if backend.actor.account_type != "human":
        raise HTTPException(404, detail={"code": "not_found"})
    run = await backend.get_run(product_id, project_id, "well_architected", run_id)
    await backend.db.refresh(run)
    configuration, version = await backend.current(product_id, project_id, "well_architected")
    await backend.db.refresh(configuration)
    await backend.db.refresh(version)
    await backend.authorize_sources(version, project_id)
    binding = await backend.db.get(Binding, (backend.actor.org_id, run.binding_id), populate_existing=True)
    connection = version.settings.get("connection_ref") or {}
    credential = await backend.db.get(UserCredential, connection.get("record_id"), populate_existing=True)
    admission = run.admission or {}
    sources = run.input.get("sources")
    native = run.native_job_ref or {}
    if (
        configuration.owner_id != backend.actor.user_id
        or configuration.handoff_state != "linked"
        or configuration.current_revision != version.revision
        or run.configuration_id != configuration.configuration_id
        or run.configuration_revision != version.revision
        or version.state != "ready"
        or version.settings.get("enabled") is not True
        or run.state != "succeeded"
        or not isinstance(sources, list)
        or not sources
        or sources != version.settings.get("source_refs")
        or admission.get("configuration_hash") != version.hash
        or run not in await backend.run_source_rights(project_id, [run])
        or binding is None
        or binding.state != ("cleaned" if cleaned else "ready")
        or binding.configuration_id != configuration.configuration_id
        or binding.product_id != product_id
        or binding.project_id != project_id
        or binding.service_id != "well_architected"
        or (binding.generation <= run.binding_generation if cleaned else binding.generation != run.binding_generation)
        or (cleaned and binding.ownership != "adp_created")
        or binding.connection_ref != connection
        or binding.native_resource_ref != admission.get("native_profile_ref")
        or native.get("profile_arn") != (binding.native_resource_ref or {}).get("record_id")
        or not isinstance(native.get("generation_id"), str)
        or not native["generation_id"]
        or credential is None
        or credential.agent_consent is not True
        or credential.org_id != backend.actor.org_id
        or credential.id != connection.get("source_connection_id")
        or credential.aws_verified_version_id != connection.get("record_revision")
        or any(binding.provider_identity.get(field) != (credential.scopes or {}).get(field) for field in ("account_id", "role_arn"))
        or binding.provider_identity.get("region") != version.settings.get("region")
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    grants = (
        await backend.db.scalars(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == backend.actor.org_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.created_by == backend.actor.user_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.service_id == worker_id("well_architected"),
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    grant = next(
        (
            candidate
            for candidate in grants
            if {"product:read", "source:read", "credential:session"} <= set(candidate.operations)
            and "architecture_review" in candidate.capabilities
            and candidate.binding_generation == run.binding_generation
            and all(source in candidate.sources for source in [{**connection, "record_type": "credential"}, *sources])
        ),
        None,
    )
    if grant is None:
        raise HTTPException(403, detail={"code": "delegation_denied"})
    credential_evidence = verified_connection_evidence(credential)
    result = await backend.db.scalar(
        select(AssessmentResult)
        .where(
            AssessmentResult.tenant_id == backend.actor.org_id,
            AssessmentResult.run_id == run_id,
        )
        .order_by(AssessmentResult.version.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if result is None or result.payload.get("native_job_ref") != native:
        raise HTTPException(409, detail={"code": "native_observation_unavailable"})
    manifest = run.manifest or {}
    files = (manifest.get("architecture") or {}).get("files")
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(item, dict) or item.get("source_ref") not in sources for item in files)
        or any(not any(item["source_ref"] == source for item in files) for source in sources)
    ):
        raise HTTPException(409, detail={"code": "native_observation_unavailable"})
    context = {
        "scope": {"tenant_id": backend.actor.org_id, "product_id": product_id, "project_id": project_id},
        "run_id": run_id,
        "run_revision": run.revision,
        "configuration_revision": version.revision,
        "binding_id": binding.binding_id,
        "binding_revision": binding.revision,
        "result_id": result.result_id,
        "result_revision": result.version,
        "provider_identity": {field: binding.provider_identity[field] for field in ("account_id", "region", "role_arn")},
        "native_job_ref": native,
        "source_manifest": manifest,
    }
    fence = json.dumps(
        {
            **context,
            "grant": [grant.id, grant.revision, str(grant.expires_at)],
            "credential": credential_evidence,
            "configuration_hash": version.hash,
            "result": result.payload,
        },
        sort_keys=True,
        default=str,
    )
    return run, version, credential, grant, result, context, fence


def observation_policy(run, version, grant, result, identity):
    profile = run.native_job_ref["profile_arn"]
    prefix = f"arn:aws:wellarchitected:{identity['region']}:{identity['account_id']}:"
    recommendations = [item.get("recommendationArn") for item in result.payload.get("recommendations", [])]
    bucket, key = archive_location(run.manifest["provider_source_uri"], run.manifest["archive"])
    configured_bucket = version.settings["well_architected"]["source_store_ref"]["record_id"].removeprefix("arn:aws:s3:::")
    if (
        not re.fullmatch(re.escape(prefix) + r"agent-profile/[A-Za-z0-9_-]+", profile)
        or len(set(recommendations)) != len(recommendations)
        or any(
            not isinstance(arn, str) or not re.fullmatch(re.escape(prefix) + r"agent-recommendation/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", arn)
            for arn in recommendations
        )
        or bucket != configured_bucket
        or any(character in key for character in "*?")
    ):
        raise ValueError("native observation scope is invalid")
    deadline = min(grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC), datetime.now(UTC) + timedelta(minutes=15))
    condition = {"DateLessThan": {"aws:CurrentTime": deadline.isoformat()}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["sts:GetCallerIdentity"], "Resource": "*", "Condition": condition},
            {
                "Effect": "Allow",
                "Action": [
                    "wellarchitected:GetAgentProfile",
                    "wellarchitected:ListAgentContexts",
                    "wellarchitected:GetAgentRecommendationGeneration",
                    "wellarchitected:ListAgentRecommendations",
                ],
                "Resource": profile,
                "Condition": condition,
            },
            {"Effect": "Allow", "Action": ["wellarchitected:GetAgentRecommendation"], "Resource": recommendations, "Condition": condition},
            {
                "Effect": "Allow",
                "Action": ["s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket}/{key}",
                "Condition": {**condition, "StringEquals": {"s3:VersionId": run.manifest["archive"]["version"]}},
            },
        ],
    }


def observe(provider, context, expected_recommendations):
    identity, job = context["provider_identity"], context["native_job_ref"]
    native = provider.client("wellarchitected", region_name=identity["region"])
    profile = native.get_agent_profile(profileArn=job["profile_arn"])
    contexts = list(_pages(native, "list_agent_contexts", profileArn=job["profile_arn"]))
    generation = native.get_agent_recommendation_generation(profileArn=job["profile_arn"], generationId=job["generation_id"])
    listed = [
        item
        for item in _pages(native, "list_agent_recommendations", profileArn=job["profile_arn"], maxResults=1000)
        if item.get("generationId") == job["generation_id"]
    ]
    if (
        profile.get("arn") != job["profile_arn"]
        or generation.get("profileArn") != job["profile_arn"]
        or generation.get("id") != job["generation_id"]
        or len(listed) != len(expected_recommendations)
        or {item.get("recommendationArn") for item in listed} != set(expected_recommendations)
        or any(item.get("profileArn") != job["profile_arn"] for item in listed)
    ):
        raise ValueError("native observation identity changed")
    manifest = context["source_manifest"]
    archive = manifest["archive"]
    bucket, key = archive_location(manifest["provider_source_uri"], archive)
    response = provider.client("s3", region_name=identity["region"]).get_object(Bucket=bucket, Key=key, VersionId=archive["version"])
    try:
        if response.get("VersionId") != archive["version"] or response.get("ContentLength") != archive["bytes"]:
            raise ValueError("native source version changed")
        content = response["Body"].read(archive["bytes"] + 1)
    finally:
        response["Body"].close()
    read_source_files(manifest, lambda uri, reference: content)
    return {
        "schema_version": 1,
        **context,
        "profile": {key: value for key, value in profile.items() if key != "ResponseMetadata"},
        "contexts": contexts,
        "generation": {key: value for key, value in generation.items() if key != "ResponseMetadata"},
        "recommendations": [],
        "source_archive_base64": base64.b64encode(content).decode("ascii"),
        "limitations": ["provider_source_coverage_unverified"],
    }


def read_recommendations(provider, context, recommendations):
    native = provider.client("wellarchitected", region_name=context["provider_identity"]["region"])
    job = context["native_job_ref"]
    observed = []
    for arn in recommendations:
        detail = native.get_agent_recommendation(recommendationArn=arn)
        if (
            detail.get("recommendationArn") != arn
            or detail.get("profileArn") != job["profile_arn"]
            or detail.get("generationId") != job["generation_id"]
        ):
            raise ValueError("native recommendation identity changed")
        observed.append({key: value for key, value in detail.items() if key != "ResponseMetadata"})
    return observed


async def read_native_observations(service, product_id, project_id, run_id):
    from src.l5_wa_runtime_observations import read_runtime

    backend = service.backend
    run, version, credential, grant, result, context, fence = await read_authority(backend, product_id, project_id, run_id)
    runtime = await read_runtime(backend.db, run, context)

    async def scoped_provider(policy):
        if len(json.dumps(policy, separators=(",", ":"))) > 2048:
            raise ValueError("scoped read policy exceeds STS limit")
        if (await read_authority(backend, product_id, project_id, run_id))[-1] != fence:
            raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
        provider, identity = await service.selected_provider(credential, version.settings, session_policy=policy, audit_run_id=run_id)
        if identity != context["provider_identity"]:
            raise ValueError("selected connection changed")
        if (await read_authority(backend, product_id, project_id, run_id))[-1] != fence:
            raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
        return provider

    try:
        policy = observation_policy(run, version, grant, result, context["provider_identity"])
        details = policy["Statement"].pop(2)
        expected = details["Resource"]
        provider = await scoped_provider(policy)
        observed = await run_in_threadpool(observe, provider, context, expected)
        for offset in range(0, len(expected), 8):
            batch = expected[offset : offset + 8]
            detail_policy = {
                "Version": policy["Version"],
                "Statement": [
                    policy["Statement"][0],
                    {**details, "Resource": batch},
                ],
            }
            provider = await scoped_provider(detail_policy)
            observed["recommendations"].extend(await run_in_threadpool(read_recommendations, provider, context, batch))
        if (await read_authority(backend, product_id, project_id, run_id))[-1] != fence:
            raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
        if await read_runtime(backend.db, run, context) != runtime:
            raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
        observed["runtime"] = runtime
        return observed
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, detail={"code": "native_observation_unavailable"}) from error
