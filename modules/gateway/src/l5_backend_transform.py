"""Request-bound Transform admission and retained result access."""

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import boto3
from botocore.config import Config
from fastapi import HTTPException
from l5_services.adapters.transform.artifacts import TransformArtifacts
from l5_services.adapters.transform.baseline import pinned_test_policy
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE
from l5_services.adapters.transform.qualification import ProviderQualification
from l5_services.configuration_readiness import effective_monitoring
from l5_services.models.run import Run
from l5_services.transform_contract import TransformSettings
from sqlalchemy import select


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise HTTPException(503, detail={"code": "transform_configuration_unavailable"})
    return value


def _aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _s3():
    return boto3.client("s3", region_name=_required("AWS_REGION"), config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 0}))


class TransformQualificationReader:
    def __init__(self, backend=None):
        self.backend = backend

    def bind_backend(self, backend):
        return type(self)(backend)

    async def __call__(self, tenant_id, product_id, project_id, selected):
        from src.products.routes import scoped_product

        backend = self.backend
        if (
            backend is None
            or backend.actor.account_type != "human"
            or tenant_id != backend.actor.org_id
            or not isinstance(selected, dict)
            or selected.get("tenant_id") != tenant_id
            or selected.get("project_id") != project_id
            or selected.get("record_type") != "executor_binding"
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        await scoped_product(backend.db, backend.actor, product_id, project_id)
        return await read_qualification(tenant_id, product_id, project_id, selected)


async def read_qualification(tenant_id, product_id, project_id, selected):
    """Read immutable evidence only after the caller establishes current scope."""
    if (
        not isinstance(selected, dict)
        or selected.get("tenant_id") != tenant_id
        or selected.get("project_id") != project_id
        or selected.get("record_type") != "executor_binding"
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    reference = json.loads(_required("L5_TRANSFORM_QUALIFICATION_REF"))
    if (
        not isinstance(reference, dict)
        or set(reference) != {"bucket", "key", "version", "sha256"}
        or any(not isinstance(value, str) or not value for value in reference.values())
        or reference["version"] == "null"
        or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
    ):
        raise HTTPException(503, detail={"code": "transform_qualification_unavailable"})
    client = _s3()
    response = await asyncio.to_thread(client.get_object, Bucket=reference["bucket"], Key=reference["key"], VersionId=reference["version"])
    try:
        contents = await asyncio.to_thread(response["Body"].read, 1024 * 1024 + 1)
    finally:
        response["Body"].close()
    if len(contents) > 1024 * 1024 or response.get("VersionId") != reference["version"] or sha256(contents).hexdigest() != reference["sha256"]:
        raise HTTPException(503, detail={"code": "transform_qualification_unavailable"})
    records = json.loads(contents)
    if not isinstance(records, list):
        raise HTTPException(503, detail={"code": "transform_qualification_unavailable"})
    scope = {"tenant_id": tenant_id, "product_id": product_id, "project_id": project_id}
    matches = [
        record for record in records if isinstance(record, dict) and record.get("scope") == scope and record.get("executor_binding_ref") == selected
    ]
    if len(matches) != 1:
        raise HTTPException(409, detail={"code": "native_unqualified"})
    record = matches[0]
    try:
        expiry = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        qualification = ProviderQualification.model_validate(record["transform_qualification"])
        if (
            expiry.tzinfo is None
            or expiry <= datetime.now(UTC)
            or not qualification.ready_for_submission
            or qualification.interface != NDJSON_INTERFACE
            or qualification.cli_version != "3.19.0"
            or qualification.binary_sha256 != NDJSON_BINARY_SHA256
        ):
            raise ValueError("qualification is not current")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(409, detail={"code": "native_unqualified"}) from None
    return {**record, "status": "qualified"}


class TransformWorkAuthority:
    def __init__(self, backend=None):
        self.backend = backend

    def bind_backend(self, backend):
        return type(self)(backend)

    async def __call__(self, configuration, version, binding, run_input, authority):
        from src.auth.aws_connection_authority import verified_connection_evidence
        from src.auth.l5_delegation import issue_l5_work_delegation
        from src.l5_service_identity import worker_id
        from src.products.l5_admission_capture import GatewayAdmissionSourceClient
        from src.shared.models.vault import CredentialServiceDelegation, UserCredential

        backend = self.backend
        if (
            backend is None
            or backend.actor.account_type != "human"
            or configuration.service_id != "transform"
            or configuration.tenant_id != backend.actor.org_id
            or configuration.owner_id != backend.actor.user_id
            or configuration.current_revision != version.revision
            or configuration.handoff_state != "linked"
            or version.settings.get("enabled") is not True
            or binding.service_id != "transform"
            or binding.state != "ready"
            or binding.gaps
            or binding.configuration_id != configuration.configuration_id
            or binding.scope != authority.get("scope")
            or binding.connection_ref != authority.get("connection_ref")
            or not backend.admission_key
            or backend.admission_operation not in {"submit", "retry"}
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        settings = version.settings
        transform = TransformSettings.model_validate(settings["transform"]).model_dump(exclude_none=True)
        connection, sources = transform["connection_ref"], run_input.get("sources")
        if (
            connection != binding.connection_ref
            or not isinstance(sources, list)
            or len(sources) != 1
            or any(source not in settings.get("source_refs", []) for source in sources)
            or run_input.get("capability") not in settings.get("capabilities", [])
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        triggers = [
            trigger
            for trigger in settings.get("triggers", [])
            if trigger.get("kind") == "manual"
            and trigger.get("enabled") is True
            and trigger.get("capability") == run_input["capability"]
            and trigger.get("binding_id") == binding.binding_id
            and trigger.get("source_refs") == sources
        ]
        if len(triggers) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        record = await backend.transform_qualification_reader(
            backend.actor.org_id, configuration.product_id, configuration.project_id, transform["executor_binding_ref"]
        )
        identity = binding.provider_identity
        if (
            record.get("provider_identity") != identity
            or not isinstance(identity, dict)
            or identity.get("region") != transform["region"]
            or any(identity.get(key) != authority.get(key) for key in ("account_id", "role_arn", "region"))
            or (transform.get("definition_ref") or {}).get("record_revision") != record["transform_qualification"]["definition_revision"]
        ):
            raise HTTPException(409, detail={"code": "native_unqualified"})
        policy = record.get("transform_test_policy")
        try:
            pinned_test_policy({"transform_settings": transform, "transform_test_policy": policy})
        except (TypeError, ValueError):
            raise HTTPException(409, detail={"code": "transform_test_policy_unavailable"}) from None
        credential = await backend.db.get(UserCredential, connection["record_id"], populate_existing=True)
        if (
            credential is None
            or credential.org_id != backend.actor.org_id
            or credential.user_id != backend.actor.user_id
            or credential.id != connection["source_connection_id"]
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
            or credential.aws_verified_version_id != connection["record_revision"]
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        verified_connection_evidence(credential)
        if any((credential.scopes or {}).get(key) != identity[key] for key in ("account_id", "role_arn")):
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
        templates = (
            await backend.db.scalars(
                select(CredentialServiceDelegation)
                .where(
                    CredentialServiceDelegation.tenant_id == backend.actor.org_id,
                    CredentialServiceDelegation.product_id == configuration.product_id,
                    CredentialServiceDelegation.project_id == configuration.project_id,
                    CredentialServiceDelegation.created_by == backend.actor.user_id,
                    CredentialServiceDelegation.credential_id == credential.id,
                    CredentialServiceDelegation.service_id == worker_id("transform"),
                    CredentialServiceDelegation.parent_delegation_id.is_(None),
                    CredentialServiceDelegation.revoked_at.is_(None),
                    CredentialServiceDelegation.expires_at > datetime.now(UTC),
                )
                .limit(2)
            )
        ).all()
        if len(templates) != 1:
            raise HTTPException(403, detail={"code": "delegation_denied"})
        grant = await issue_l5_work_delegation(
            backend.db,
            backend.actor,
            templates[0],
            credential,
            binding,
            connection,
            sources,
            run_input["capability"],
            settings["limits"]["deadline_seconds"],
            backend.admission_operation,
            backend.admission_key,
            previous_id=backend.previous_delegation_id,
        )
        verified = {
            "scope": binding.scope,
            "service_id": "transform",
            "actor_id": backend.actor.user_id,
            "actor_type": "human",
            "configuration_id": configuration.configuration_id,
            "configuration_revision": version.revision,
            "configuration_hash": version.hash,
            "configuration_enabled": True,
            "binding_id": binding.binding_id,
            "binding_generation": binding.generation,
            "binding_state": binding.state,
            "connection_ref": connection,
            "binding_connection_ref": binding.connection_ref,
            "delegation_id": grant.id,
            "delegation_version": grant.revision,
            "delegation_active": True,
            "trigger": triggers[0],
            "source_refs": settings["source_refs"],
            "capabilities": settings["capabilities"],
            "transform_settings": transform,
            "qualified_executor_ref": transform["executor_binding_ref"],
            "transform_qualification": record["transform_qualification"],
            "transform_test_policy": policy,
            "provider_identity": identity,
            "profile_ref": settings["profile_ref"],
            "adapter_revision": "transform-v1",
            "limits": settings["limits"],
            "visibility": settings["visibility"],
            "classification": settings["classification"],
            "causal_origin": "manual",
            "exclusions": settings.get("exclusions", []),
            "monitoring_policy": effective_monitoring(settings),
            "max_bytes": 6 * 1024 * 1024,
            "max_files": 1000,
        }
        return verified, GatewayAdmissionSourceClient(
            backend.db, backend.actor, grant.id, verified, backend.admission_operation, backend.admission_key, service_id="transform"
        )


class TransformArtifactReader:
    def __init__(self, backend=None):
        self.backend = backend

    def bind_backend(self, backend):
        return type(self)(backend)

    async def read(self, *, reference, authority):
        from src.shared.identity.product_scope import current_l5_parent
        from src.shared.models.vault import CredentialServiceDelegation

        backend = self.backend
        if (
            backend is None
            or backend.actor.account_type != "human"
            or not isinstance(authority, dict)
            or authority.get("tenant_id") != backend.actor.org_id
            or authority.get("actor_id") != backend.actor.user_id
        ):
            raise PermissionError("Transform result authority differs from the authenticated reader")
        product_id, project_id = authority.get("product_id"), authority.get("project_id")
        store = await backend.result_store(product_id, project_id, "transform")
        matches = [result for result in await store.list() if reference in result.get("retained_artifacts", {}).values()]
        if len(matches) != 1:
            raise PermissionError("Transform artifact is not in an authorized result")
        run = await backend.db.get(Run, (backend.actor.org_id, matches[0]["run_id"]), populate_existing=True)
        grant = await backend.db.get(CredentialServiceDelegation, run.admission["delegation_id"], populate_existing=True)
        if grant is None or not await current_l5_parent(backend.db, grant):
            raise PermissionError("Transform result delegation was revoked")

        def assert_current():
            now = datetime.now(UTC)
            if (
                _aware(grant.expires_at) <= now
                or grant.revoked_at is not None
                or backend.actor.expires_at is not None
                and _aware(backend.actor.expires_at) <= now
            ):
                raise PermissionError("Transform result authority expired")

        scoped = SimpleNamespace(
            scope={key: authority[key] for key in ("tenant_id", "product_id", "project_id")},
            source_request={"operation_id": run.run_id},
            assert_current=assert_current,
        )
        storage = TransformArtifacts(_s3(), _required("L5_TRANSFORM_ARTIFACT_BUCKET"))
        contents = await storage.read(reference=reference, authority=scoped)
        if run not in await backend.run_source_rights(project_id, [run]) or not await current_l5_parent(backend.db, grant):
            raise PermissionError("Transform result authority changed during readback")
        return contents


def gateway_backends():
    return {
        "l5_transform_work_authority": TransformWorkAuthority(),
        "l5_transform_executor_qualification_reader": TransformQualificationReader(),
        "l5_transform_artifact_store": TransformArtifactReader(),
    }
