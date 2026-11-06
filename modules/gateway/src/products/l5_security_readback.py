"""Product-authorized native Security evidence without runner AWS credentials."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from l5_services.adapters.security.native_readback import native_job, native_jobs
from l5_services.api.bindings import _intent_hash
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.receipt import RunReceipt
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run, view_run
from l5_services.models.setup import SetupIntent
from l5_services.models.work import Work
from l5_services.publication.contributions import validate_receipt
from l5_services.security_registration import current_model, current_registration
from sqlalchemy import select

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.internal.l5_credential_routes import _security_space_arn
from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.l5_service_identity import worker_id
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper


def denied():
    return HTTPException(404, detail={"code": "not_found"})


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stable_authority(authority):
    """Compare authority, excluding only the timestamp of each fresh read."""
    return {
        **authority,
        "authorization": {key: value for key, value in authority["authorization"].items() if key != "authorized_at"},
    }


def native_spaces(client, owned, linked, receipt, run_id, state):
    expected = {owned["record_id"], linked["record_id"]}
    if len(expected) != 2:
        raise ValueError("selected native spaces are not distinct")
    response = client.batch_get_agent_spaces(agentSpaceIds=sorted(expected))
    spaces, missing = response.get("agentSpaces"), response.get("notFound", [])
    if (
        not isinstance(spaces, list)
        or not isinstance(missing, list)
        or any(not isinstance(space, dict) for space in spaces)
        or any(not isinstance(item, str) for item in missing)
        or len({space.get("agentSpaceId") for space in spaces}) != len(spaces)
        or len(missing) != len(set(missing))
        or {space.get("agentSpaceId") for space in spaces} | set(missing) != expected
        or set(missing) & {space.get("agentSpaceId") for space in spaces}
        or linked["record_id"] in missing
    ):
        raise ValueError("selected native spaces have incomplete readback")
    if state == "cleaned" and owned["record_id"] not in missing:
        raise ValueError("cleaned native space remains present")
    for space in spaces:
        reference = owned if space["agentSpaceId"] == owned["record_id"] else linked
        created = space.get("createdAt")
        if not isinstance(created, datetime) or created.isoformat() != reference["record_revision"]:
            raise ValueError("selected native space identity changed")
        if reference is owned and (
            space.get("name") != f"adp-{receipt['operation_id']}" or space.get("description") != f"ADP setup {receipt['intent_hash']}"
        ):
            raise ValueError("owned native space identity changed")
    return {
        "run_id": run_id,
        "ownership": {owned["record_id"]: "scenario_owned", linked["record_id"]: "customer_linked"},
        "resources": {owned["record_id"]: "absent" if owned["record_id"] in missing else "present", linked["record_id"]: "retained"},
    }


async def read_native(backend, product_id, project_id, run_id, action, *, owned_binding_id=None):
    if (
        action not in {"job", "inventory", "spaces"}
        or (action == "spaces") != (owned_binding_id is not None)
        or backend.actor.account_type != "human"
        or backend.actor.auth_source != "jwt"
        or backend.actor.token_use != "access"
        or backend.actor.expires_at is None
        or backend.actor.expires_at.tzinfo is None
        or backend.actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
    ):
        raise denied()

    async def selection():
        backend.db.expire_all()
        _, authority = await backend.binding_authority(product_id, project_id, "security")
        run = await backend.get_run(product_id, project_id, "security", run_id)
        if not isinstance(run.admission, dict) or not isinstance(run.input, dict):
            raise denied()
        store = await backend.binding_store(product_id, project_id, "security")
        binding = await store.binding(run.binding_id)
        grant = await backend.db.get(CredentialServiceDelegation, authority["authorization"]["delegation_id"], populate_existing=True)
        connection = authority["connection_ref"]
        credential = await backend.db.get(UserCredential, connection["record_id"], populate_existing=True)
        capability = run.input.get("capability")
        # Setup freshness gates a new assessment. Reading its existing job
        # retains the admission pins; current consent and generation are still
        # required below, including on the post-read authority check.
        try:
            admitted_at = datetime.fromisoformat(run.admission["admitted_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise denied() from None
        if admitted_at.tzinfo is None or admitted_at > datetime.now(UTC):
            raise denied()
        registration = current_registration(binding, now=admitted_at)
        model = current_model(binding, now=admitted_at) if capability == "threat_model" else None
        if (
            run.tenant_id != backend.actor.org_id
            or run.state not in {"running", "collecting", "succeeded", "partial", "failed", "expired", "outcome_unknown"}
            or not isinstance(run.native_job_ref, dict)
            or capability not in {"code_review", "threat_model"}
            or not isinstance(binding.provider_identity, dict)
            or run.admission.get("native_space_ref") != binding.native_resource_ref
            or run.admission.get("provider_identity") != binding.provider_identity
            or run.admission.get("connection_ref") != connection
            or run.admission.get("scope") != authority["scope"]
            or run.configuration_id != authority["configuration_id"]
            or run.configuration_revision != authority["configuration_revision"]
            or binding.configuration_id != run.configuration_id
            or binding.state != "ready"
            or binding.gaps
            or binding.generation != run.binding_generation
            or binding.connection_ref != connection
            or binding.scope != authority["scope"]
            or registration is None
            or capability == "code_review"
            and run.admission.get("source_registration") != registration
            or capability == "threat_model"
            and (model is None or run.admission.get("native_model_ref") != model)
            or capability == "threat_model"
            and (
                run.native_job_ref.get("agent_space_id") != binding.native_resource_ref.get("record_id")
                or run.native_job_ref.get("threat_model_id") != (model or {}).get("record_id")
                or run.native_job_ref.get("tenant_id") != run.tenant_id
                or run.native_job_ref.get("project_id") != run.project_id
            )
            or grant is None
            or capability not in (grant.capabilities or [])
            or grant.binding_generation != binding.generation
            or grant.revoked_at is not None
            or aware(grant.expires_at) <= datetime.now(UTC)
            or credential is None
            or credential.user_id != backend.actor.user_id
            or credential.aws_verified_version_id != connection["record_revision"]
            or any(binding.provider_identity.get(field) != (credential.scopes or {}).get(field) for field in ("account_id", "role_arn"))
        ):
            raise denied()
        owned = None
        receipt = None
        if action == "spaces":
            if owned_binding_id == run.binding_id:
                raise denied()
            owned = await store.binding(owned_binding_id)
            receipt = owned.creation_receipt or {}
            intent = await backend.db.get(SetupIntent, (run.tenant_id, receipt.get("operation_id")), populate_existing=True)
            active = await backend.db.scalar(select(Run.run_id).where(Run.tenant_id == run.tenant_id, Run.binding_id == owned_binding_id).limit(1))
            if (
                owned.ownership != "adp_created"
                or owned.state not in {"ready", "cleanup_pending", "cleaned"}
                or owned.gaps
                or owned.configuration_id != binding.configuration_id
                or owned.scope != binding.scope
                or owned.connection_ref != binding.connection_ref
                or owned.provider_identity != binding.provider_identity
                or active is not None
                or not isinstance(owned.native_resource_ref, dict)
                or owned.native_resource_ref == binding.native_resource_ref
                or intent is None
                or intent.configuration_id != owned.configuration_id
                or (intent.product_id, intent.project_id, intent.service_id, intent.actor_id)
                != (product_id, project_id, "security", backend.actor.user_id)
                or intent.scope != owned.scope
                or not isinstance(intent.request, dict)
                or intent.request.get("action") != "create"
                or intent.operation_id != receipt.get("operation_id")
                or intent.owned_resource_receipt is not None
                or intent.intent_hash != _intent_hash(intent)
                or receipt.get("intent_hash") != intent.intent_hash
                or receipt.get("native_resource_ref") != owned.native_resource_ref
                or receipt.get("provider_identity") != owned.provider_identity
            ):
                raise denied()
        verified_connection_evidence(credential)
        return run, binding, grant, credential, owned, receipt

    run, binding, grant, credential, owned, receipt = await selection()
    original = (
        run.revision,
        json.dumps(run.native_job_ref, sort_keys=True),
        binding.generation,
        grant.revision,
        (owned.revision, owned.state, json.dumps(owned.native_resource_ref, sort_keys=True), json.dumps(receipt, sort_keys=True)) if owned else None,
    )
    identity = binding.provider_identity
    resource = _security_space_arn(identity, binding.native_resource_ref)
    now = datetime.now(UTC)
    expiry = min(now + timedelta(minutes=15), aware(grant.expires_at))
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Resource": ([resource, _security_space_arn(identity, owned.native_resource_ref)] if owned else resource),
                    "Condition": {"DateLessThan": {"aws:CurrentTime": expiry.isoformat()}},
                    "Action": (
                        ["securityagent:BatchGetAgentSpaces"]
                        if action == "spaces"
                        else [
                            "securityagent:ListCodeReviews",
                            "securityagent:ListCodeReviewJobsForCodeReview",
                            "securityagent:BatchGetCodeReviewJobs",
                            "securityagent:ListFindings",
                            "securityagent:BatchGetFindings",
                            "securityagent:ListCodeReviewJobTasks",
                        ]
                        if run.input["capability"] == "code_review"
                        else [
                            "securityagent:ListThreatModelJobs",
                            "securityagent:BatchGetThreatModelJobs",
                            "securityagent:ListThreats",
                            "securityagent:BatchGetThreats",
                            "securityagent:ListThreatModelJobTasks",
                        ]
                    ),
                }
            ],
        },
        separators=(",", ":"),
    )
    store = SecretsManagerHelper()
    try:
        async with asyncio.timeout(90):
            version = credential.aws_verified_version_id
            if await asyncio.to_thread(store.current_version_id, credential.secret_arn) != version:
                raise ValueError("selected AWS connection changed")
            payload, served = await asyncio.to_thread(store.get_secret_at_version, credential.secret_arn, version)
            if served != version:
                raise ValueError("selected AWS connection changed")
            role, external_id, account = connection_material(json.loads(payload), credential.scopes or {}, credential)
            if (role, account) != (identity["role_arn"], identity["account_id"]):
                raise ValueError("selected AWS role changed")
            assumed = await asyncio.to_thread(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=identity["region"],
                aws_region=identity["region"],
                user_id=backend.actor.user_id,
                agent_id=worker_id("security"),
                task_id=run_id,
                label=credential.label,
                session_policy=policy,
            )
            require_assumed_identity(assumed, role)
            client = boto3.Session(
                aws_access_key_id=assumed.access_key_id,
                aws_secret_access_key=assumed.secret_access_key,
                aws_session_token=assumed.session_token,
                region_name=identity["region"],
            ).client("securityagent")
            from l5_services.models.run import view_run

            selected = view_run(run)
            result = (
                await asyncio.to_thread(native_spaces, client, owned.native_resource_ref, binding.native_resource_ref, receipt, run_id, owned.state)
                if action == "spaces"
                else await asyncio.to_thread(native_job if action == "job" else native_jobs, client, selected)
            )
            if await asyncio.to_thread(store.current_version_id, credential.secret_arn) != version:
                raise ValueError("selected AWS connection changed")
            current, current_binding, current_grant, _, current_owned, _ = await selection()
            if (
                current.revision,
                json.dumps(current.native_job_ref, sort_keys=True),
                current_binding.generation,
                current_grant.revision,
                (
                    current_owned.revision,
                    current_owned.state,
                    json.dumps(current_owned.native_resource_ref, sort_keys=True),
                    json.dumps(current_owned.creation_receipt, sort_keys=True),
                )
                if current_owned
                else None,
            ) != original:
                raise ValueError("Product authorization changed during native read")
            return result
    except (TimeoutError, ValueError, KeyError, STSAssumeError, BotoCoreError, ClientError):
        raise HTTPException(409, detail={"code": "native_readback_unavailable"}) from None


async def read_recovery_state(backend, product_id, project_id, run_id):
    actor = backend.actor
    if (
        actor.account_type != "human"
        or actor.auth_source != "jwt"
        or actor.token_use != "access"
        or actor.expires_at is None
        or actor.expires_at.tzinfo is None
        or actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
    ):
        raise denied()
    backend.db.expire_all()
    _, authority = await backend.binding_authority(product_id, project_id, "security")
    run = await backend.get_run(product_id, project_id, "security", run_id)
    binding = await (await backend.binding_store(product_id, project_id, "security")).binding(run.binding_id)
    grant = await backend.db.get(CredentialServiceDelegation, authority["authorization"]["delegation_id"], populate_existing=True)
    if (
        not isinstance(run.admission, dict)
        or not isinstance(run.input, dict)
        or not isinstance(run.admission.get("scope"), dict)
        or run.tenant_id != actor.org_id
        or run.admission["scope"].get("owner_id") != actor.user_id
        or run.admission.get("scope") != authority["scope"]
        or run.admission.get("connection_ref") != authority["connection_ref"]
        or run.admission.get("provider_identity") != binding.provider_identity
        or run.admission.get("native_space_ref") != binding.native_resource_ref
        or run.configuration_id != authority["configuration_id"]
        or run.configuration_revision != authority["configuration_revision"]
        or run.binding_generation != binding.generation
        or binding.state != "ready"
        or binding.gaps
        or binding.configuration_id != run.configuration_id
        or binding.connection_ref != authority["connection_ref"]
        or binding.scope != authority["scope"]
        or grant is None
        or grant.revoked_at is not None
        or aware(grant.expires_at) <= datetime.now(UTC)
        or grant.binding_generation != binding.generation
        or run.input.get("capability") not in (grant.capabilities or [])
    ):
        raise denied()
    selected_authority = deepcopy(stable_authority(authority))
    selected_run = deepcopy(view_run(run))
    selected_run_revision = run.revision
    selected_binding = (
        binding.generation,
        binding.state,
        deepcopy(binding.scope),
        deepcopy(binding.connection_ref),
        deepcopy(binding.provider_identity),
    )
    selected_grant = (grant.revision, grant.binding_generation, tuple(grant.capabilities or []), grant.revoked_at, grant.expires_at)
    selected_binding_id = run.binding_id
    attempts = (
        await backend.db.scalars(
            select(WorkerAttempt)
            .where(WorkerAttempt.tenant_id == run.tenant_id, WorkerAttempt.run_id == run_id)
            .order_by(WorkerAttempt.attempt_id)
            .limit(21)
        )
    ).all()
    work = (
        await backend.db.scalars(select(Work).where(Work.tenant_id == run.tenant_id, Work.run_id == run_id).order_by(Work.work_id).limit(21))
    ).all()
    publications = (
        await backend.db.scalars(
            select(Publication)
            .join(AssessmentResult, (Publication.tenant_id == AssessmentResult.tenant_id) & (Publication.result_id == AssessmentResult.result_id))
            .where(AssessmentResult.tenant_id == run.tenant_id, AssessmentResult.run_id == run_id)
            .order_by(Publication.publication_id)
            .limit(2)
        )
    ).all()
    if len(attempts) > 20 or len(work) > 20 or len(publications) > 1:
        raise HTTPException(409, detail={"code": "recovery_readback_incomplete"})
    response = deepcopy(
        {
            "run": selected_run,
            "publications": [
                {
                    "publication_id": item.publication_id,
                    "result_id": item.result_id,
                    "result_version": item.result_version,
                    "state": item.state,
                    "schema_version": item.schema_version,
                    "attempts": item.attempts,
                    "receipt": item.receipt,
                }
                for item in publications
            ],
            "attempts": [
                {
                    "attempt_id": item.attempt_id,
                    "operation": item.operation,
                    "status": item.status,
                    "first_work_id": item.first_work_id,
                    "native_job_ref": item.native_job_ref,
                    "reconcile_checks": item.reconcile_checks,
                    "started_at": item.started_at.isoformat(),
                }
                for item in attempts
            ],
            "work": [
                {
                    "work_id": item.work_id,
                    "kind": item.kind,
                    "generation": item.generation,
                    "fence": item.fence,
                    "attempts": item.attempts,
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                    "last_enqueued_at": item.last_enqueued_at.isoformat() if item.last_enqueued_at else None,
                }
                for item in work
            ],
        }
    )
    backend.db.expire_all()
    _, current_authority = await backend.binding_authority(product_id, project_id, "security")
    current_run = await backend.get_run(product_id, project_id, "security", run_id)
    current_binding = await (await backend.binding_store(product_id, project_id, "security")).binding(selected_binding_id)
    current_grant = await backend.db.get(CredentialServiceDelegation, current_authority["authorization"]["delegation_id"], populate_existing=True)
    if (
        actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
        or stable_authority(current_authority) != selected_authority
        or current_run.revision != selected_run_revision
        or view_run(current_run) != selected_run
        or (
            current_binding.generation,
            current_binding.state,
            current_binding.scope,
            current_binding.connection_ref,
            current_binding.provider_identity,
        )
        != selected_binding
        or current_grant is None
        or current_grant.revoked_at is not None
        or aware(current_grant.expires_at) <= datetime.now(UTC)
        or (
            current_grant.revision,
            current_grant.binding_generation,
            tuple(current_grant.capabilities or []),
            current_grant.revoked_at,
            current_grant.expires_at,
        )
        != selected_grant
    ):
        raise denied()
    return response


async def read_submission_receipt(backend, product_id, project_id, receipt_id):
    actor = backend.actor
    if (
        actor.account_type != "human"
        or actor.auth_source != "jwt"
        or actor.token_use != "access"
        or actor.expires_at is None
        or actor.expires_at.tzinfo is None
        or actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
    ):
        raise denied()
    backend.db.expire_all()
    receipt = await backend.db.get(
        RunReceipt, (actor.org_id, product_id, project_id, "security", actor.user_id, "submit", receipt_id), populate_existing=True
    )
    if receipt is None:
        raise denied()
    selected_run_id = receipt.run_id
    state = await read_recovery_state(backend, product_id, project_id, selected_run_id)
    current_receipt = await backend.db.get(
        RunReceipt, (actor.org_id, product_id, project_id, "security", actor.user_id, "submit", receipt_id), populate_existing=True
    )
    if (
        current_receipt is None
        or current_receipt.run_id != selected_run_id
        or state["run"]["run_id"] != selected_run_id
        or state["run"].get("origin") != "adp_submitted"
    ):
        raise denied()
    return {"run_id": state["run"]["run_id"]}


def _publication_snapshot(publication):
    return deepcopy(
        {
            field: getattr(publication, field)
            for field in (
                "publication_id",
                "result_id",
                "result_version",
                "destination",
                "schema_version",
                "contribution_hash",
                "payload",
                "state",
                "receipt",
                "invalidation_watermark",
                "revision",
            )
        }
    )


async def read_publication(backend, product_id, project_id, run_id, publication_id):
    """Release the retained contribution only while its owner and sources remain authorized."""
    selected = await read_recovery_state(backend, product_id, project_id, run_id)
    run = selected["run"]
    if run["state"] not in {"succeeded", "partial"}:
        raise denied()
    metadata = [item for item in selected["publications"] if item["publication_id"] == publication_id]
    if len(metadata) != 1 or metadata[0]["state"] != "published":
        raise denied()
    key = (backend.actor.org_id, publication_id)
    publication = await backend.db.get(Publication, key, populate_existing=True)
    if publication is None:
        raise denied()
    snapshot = _publication_snapshot(publication)
    result_key = (backend.actor.org_id, snapshot["result_id"])
    result = await backend.db.get(AssessmentResult, result_key, populate_existing=True)
    detail = await backend.db.get(ResultDetail, result_key, populate_existing=True)
    payload = snapshot["payload"]
    if (
        result is None
        or detail is None
        or not isinstance(payload, dict)
        or result.run_id != run_id
        or result.version != snapshot["result_version"]
        or snapshot["destination"] != "product-context"
        or snapshot["state"] != "published"
        or snapshot["invalidation_watermark"] != 0
        or any(snapshot[field] != metadata[0][field] for field in ("result_id", "result_version", "schema_version", "receipt"))
        or payload.get("kind") is not None
        or payload.get("contribution_id") != publication_id
        or payload.get("schema_version") != snapshot["schema_version"]
        or payload.get("scope") != run["scope"]
        or payload.get("scope") != detail.payload.get("scope")
        or payload.get("run_id") != run_id
        or detail.payload.get("run_id") != run_id
        or detail.payload.get("revision") != result.version
        or payload.get("native_job_ref") != detail.payload.get("native_job_ref")
        or payload.get("native_job_ref") != run["native_job_ref"]
        or payload.get("source_refs") != run["input"].get("sources")
        or not payload.get("source_refs")
        or not payload.get("manifest_ref")
        or payload.get("result_ref")
        != {
            "tenant_id": backend.actor.org_id,
            "project_id": project_id,
            "source_connection_id": "l5-security",
            "record_type": "security_result",
            "record_id": result.result_id,
            "record_revision": str(result.version),
        }
        or sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != snapshot["contribution_hash"]
    ):
        raise denied()
    try:
        receipt = validate_receipt(SimpleNamespace(**snapshot), snapshot["receipt"])
        if payload["result_ref"] not in receipt["record_refs"]:
            raise ValueError("receipt lacks the result revision")
    except (ValueError, KeyError, TypeError):
        raise denied() from None
    current = await read_recovery_state(backend, product_id, project_id, run_id)
    retained = await backend.db.get(Publication, key, populate_existing=True)
    if (
        current != selected
        or retained is None
        or _publication_snapshot(retained) != snapshot
        or backend.actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
    ):
        raise denied()
    return {"run_id": run_id, "publication": snapshot}
