"""Exchange an engine-dispatched GitHub job's OIDC for its selected AWS connection.

Correlation is a lookup key, never authority. The signed job identity, tenant App
actor, current delivery grant and exclusive target lease must all still agree.
Worker credential authentication stays on its existing internal route.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.agentauth.arc_model import github_claims
from src.internal.assume_role_routes import AssumeRoleRequestBody, assume_bound_role, get_secrets_manager
from src.internal.credential_binding import BindingResult
from src.knowledge.github_app_service import _mint_app_jwt, resolve_tenant_app_credentials
from src.shared.database import get_session_factory

from .deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence
from .deployment_workflow_provider import TRANSPORT_INPUTS
from .deployment_workflows import WORKFLOW_KIND, WorkflowServices
from .environment_leases import LeaseError, shared_release_held, shared_release_target
from .execution_runner import RunnerContext, _identity
from .execution_state import OutcomeKind
from .execution_store import load_execution
from .models import OrchestrationAction, OrchestrationEnvironmentLease, OrchestrationExecution
from .review_cycle import CycleBlockedError

AUDIENCE = "adp-deployment-credentials"
router = APIRouter(prefix="/orchestration/deployment-credentials", tags=["orchestration"])


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlation: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    inputs: dict[str, str] = Field(max_length=20)


def require(condition):
    if not condition:
        raise HTTPException(403, "deployment_credential_authority_refused")


class DeploymentCredentials:
    def __init__(self, factory, services=None):
        self.factory = factory
        self.services = services or WorkflowServices(factory)

    async def authority(self, body, claims):
        async with self.factory() as db:
            # Repository comes from verified GitHub claims, not request selectors.
            rows = (
                await db.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.kind == WORKFLOW_KIND,
                        OrchestrationAction.detail["correlation"].as_string() == body.correlation,
                        OrchestrationAction.detail["repo"].as_string() == claims["repository"],
                    )
                    .limit(2)
                )
            ).all()
            require(len(rows) == 1)
            action = rows[0]
            saved = dict(action.detail or {})
            require(saved.get("dispatch_started") and not saved.get("runtime_verified") and action.status != "failed")
            execution = await db.get(OrchestrationExecution, action.execution_id)
            require(execution is not None and execution.org_id == action.org_id)
            loaded = await load_execution(db, identity=_identity(execution))
            require(loaded.kind is OutcomeKind.APPLIED and loaded.record is not None)
            require(loaded.record.status.value not in {"concluded", "superseded", "blocked"})
            context = RunnerContext(_identity(execution), loaded.record, datetime.now(UTC))
            lease = await db.get(OrchestrationEnvironmentLease, saved.get("lease_id"))
            require(lease is not None and lease.state == "held")
            require(
                (
                    lease.owner_org_id,
                    lease.owner_action_id,
                    lease.owner_generation,
                    lease.manifest_entry_id,
                    lease.release_ref,
                    lease.canonical_target_key,
                )
                == (
                    action.org_id,
                    action.id,
                    context.identity.claim_generation,
                    saved["manifest_entry_id"],
                    saved["source_revision"],
                    saved["target_key"],
                )
            )
            require(lease.lease_expires_at is not None and lease.lease_expires_at > datetime.now(UTC))
        # This rechecks live claim, plan, protected policy, manifest, merge and
        # registered connection. Do not use snapshot(reconcile=True): it permits
        # observations after revocation, whereas issuing credentials must stop.
        fresh, binding, _, _, _, latest = await self.services.snapshot(context)
        require(latest is not None and latest.id == action.id)
        for key in (
            "operation_key",
            "policy_hash",
            "manifest_digest",
            "binding_id",
            "binding_revision",
            "accepted_scope",
            "source_revision",
            "target_key",
            "inputs",
            "credential_id",
            "credential_role_arn",
            "principal_user_id",
        ):
            require(key in saved and fresh.get(key) == saved[key])
        require(datetime.fromisoformat(saved["observation_deadline"]) > datetime.now(UTC))
        effective_inputs = {k: v for k, v in {**saved["definition"]["defaults"], **saved["inputs"]}.items() if k not in TRANSPORT_INPUTS}
        require(body.source_revision == saved["source_revision"] and body.inputs == effective_inputs)
        definition = saved["definition"]
        require(claims.get("event_name") == "workflow_dispatch")
        require(claims.get("repository_id") == str(saved["provider_repository_id"]))
        require(claims.get("workflow_ref") == f"{saved['repo']}/{saved['workflow_path']}@refs/heads/{definition['dispatch_ref']}")
        require(claims.get("workflow_sha") == definition["dispatch_revision"])
        require(claims.get("sha") == definition["dispatch_revision"])
        require(claims.get("ref") == f"refs/heads/{definition['dispatch_ref']}")
        # Reruns require a new engine action; an old successful run is not authority.
        require(claims.get("run_attempt") == "1")
        return action, saved, binding

    async def verify_run(self, claims, saved, binding):
        provider = self.services.provider
        app_id, key = await resolve_tenant_app_credentials(binding.org_id)
        app = (await provider.request(binding, "GET", "/app", token=_mint_app_jwt(app_id, key))).json()
        bot = (await provider.request(binding, "GET", f"/users/{app['slug']}[bot]")).json()
        require(bot.get("type") == "Bot" and str(bot.get("id")) == claims["actor_id"])
        run = (await provider.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{claims['run_id']}")).json()
        require(run.get("id") == int(claims["run_id"]) and run.get("run_attempt") == 1)
        require(run.get("event") == "workflow_dispatch" and run.get("status") == "in_progress")
        require(run.get("repository", {}).get("id") == int(claims["repository_id"]))
        require(run.get("path") == saved["workflow_path"] and run.get("head_sha") == saved["definition"]["dispatch_revision"])
        require(run.get("display_title") == "ADP deployment " + saved["correlation"])
        require(str(run.get("actor", {}).get("id")) == claims["actor_id"])
        require(str(run.get("triggering_actor", {}).get("id")) == claims["actor_id"])
        return run

    async def require_grouped_lease(self, action, saved, claims, run, entry, inputs):
        try:
            raw_target = saved["target"]
            physical = PhysicalTarget(**{**raw_target, "evidence": TargetEvidence(**raw_target["evidence"])})
            shared = shared_release_target(physical, environment=inputs["environment"], group=entry.release_group)
            started_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, LeaseError):
            require(False)
        async with self.factory() as db:
            execution = await db.get(OrchestrationExecution, action.execution_id)
            require(execution is not None and execution.org_id == action.org_id)
            require(
                await shared_release_held(
                    db,
                    target=shared,
                    org_id=action.org_id,
                    action_id=action.id,
                    release_ref=saved["source_revision"],
                    manifest_entry_id=entry.entry_id,
                    generation=execution.claim_generation,
                    started_at=started_at,
                )
            )
            require(claims["run_attempt"] == "1" and str(run["run_attempt"]) == claims["run_attempt"])

    async def bind_run(self, action, claims):
        identity = {"run_id": claims["run_id"], "run_attempt": claims["run_attempt"]}
        async with self.factory() as db:
            row = await db.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.id == action.id,
                    OrchestrationAction.org_id == action.org_id,
                )
                .with_for_update()
            )
            detail = dict(row.detail)
            require(not detail.get("runtime_verified") and row.status != "failed")
            require(detail.get("credential_job", identity) == identity)
            row.detail = {**detail, "credential_job": identity}
            await db.commit()

    async def authorize(self, body, claims):
        action, saved, binding = await self.authority(body, claims)
        run = await self.verify_run(claims, saved, binding)
        entry = self.services.manifest_loader().entry(saved["manifest_entry_id"])
        require(entry is not None and entry.status is EntryStatus.ENABLED and not entry.docs_only)
        if entry.release_group is not None:
            await self.require_grouped_lease(action, saved, claims, run, entry, body.inputs)
        await self.bind_run(action, claims)
        return action, saved


def service():
    return DeploymentCredentials(get_session_factory())


async def identity(request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and bool(token))
    return await github_claims(token, audience=AUDIENCE)


@router.post("/context")
async def job_context(body: JobRequest, request: Request, response: Response, broker=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    try:
        _, saved = await broker.authorize(body, await identity(request))
        return {key: saved[key] for key in ("source_revision", "inputs", "target")}
    except CycleBlockedError:
        raise HTTPException(403, "deployment_credential_authority_refused") from None


@router.post("/assume")
async def job_assume(body: JobRequest, request: Request, response: Response, broker=Depends(service), sm=Depends(get_secrets_manager)):
    response.headers["Cache-Control"] = "no-store"
    try:
        claims = await identity(request)
        action, saved = await broker.authorize(body, claims)
        principal = saved["principal_user_id"]

        async def validate(credential, **_):
            _, fresh = await broker.authorize(body, claims)
            require(fresh["credential_id"] == saved["credential_id"] == credential.id)
            require(credential.org_id == action.org_id and credential.user_id == principal)
            require((credential.scopes or {}).get("role_arn") == saved["credential_role_arn"])

        async with broker.factory() as db:
            result = await assume_bound_role(
                AssumeRoleRequestBody(
                    user_id=principal,
                    agent_id="deployment-workflow",
                    task_id=action.id,
                    label=saved["inputs"]["customer_aws_label"],
                    purpose="Engine-approved deployment",
                ),
                request,
                db,
                sm,
                binding=BindingResult(principal, False, False, principal, tenant_id=action.org_id),
                validate=validate,
                audit_fields={"deployment_action_id": action.id, "github_run_id": claims["run_id"], "credential_permission_mode": "user_configured"},
            )
            await db.commit()
            return result
    except CycleBlockedError:
        raise HTTPException(403, "deployment_credential_authority_refused") from None
