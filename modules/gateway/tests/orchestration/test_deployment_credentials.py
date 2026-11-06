"""Selected AWS role delivery requires the real engine ledger and GitHub job."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.orchestration import deployment_credentials as credentials
from src.orchestration.models import OrchestrationAction, OrchestrationEnvironmentLease, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_deployment_workflows import action, deployment, tick  # noqa: F401
from tests.orchestration.test_merge_controller import merge  # noqa: F401
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)


@pytest.fixture
async def job(deployment, monkeypatch):  # noqa: F811
    ctx = deployment
    ctx.definition = replace(
        ctx.definition, defaults={**ctx.definition.defaults, "adp_correlation": "", "adp_source_revision": "", "adp_definition_revision": ""}
    )
    ctx.provider.definition = AsyncMock(return_value=ctx.definition)
    assert (await tick(ctx)).effects_succeeded == 1
    row = await action(ctx)
    saved = row.detail
    claims = dict(
        repository=ctx.binding.repo,
        repository_id="123",
        event_name="workflow_dispatch",
        workflow_ref=f"{ctx.binding.repo}/{saved['workflow_path']}@refs/heads/main",
        workflow_sha=saved["definition"]["dispatch_revision"],
        sha=saved["definition"]["dispatch_revision"],
        ref="refs/heads/main",
        run_id="42",
        run_attempt="1",
        actor_id="77",
    )
    body = credentials.JobRequest(correlation=saved["correlation"], source_revision=saved["source_revision"], inputs=saved["inputs"])
    run = dict(
        id=42,
        run_attempt=1,
        event="workflow_dispatch",
        status="in_progress",
        repository={"id": 123},
        path=saved["workflow_path"],
        head_sha=saved["definition"]["dispatch_revision"],
        display_title="ADP deployment " + saved["correlation"],
        actor={"id": 77},
        triggering_actor={"id": 77},
    )

    async def request(binding, method, path, **kwargs):
        value = {"slug": "tenant-app"} if path == "/app" else {"type": "Bot", "id": 77} if path.startswith("/users/") else run
        return SimpleNamespace(json=lambda: value)

    ctx.provider.request = AsyncMock(side_effect=request)
    monkeypatch.setattr(credentials, "resolve_tenant_app_credentials", AsyncMock(return_value=("app", "key")))
    monkeypatch.setattr(credentials, "_mint_app_jwt", lambda *args: "test-app-jwt")
    broker = credentials.DeploymentCredentials(ctx.factory, ctx.workflow_services)
    return ctx, broker, body, claims, run


@pytest.mark.parametrize("case", ["missing", "foreign", "early", "future", "wrong_attempt", "valid"])
async def test_grouped_job_lease_requires_exact_preexisting_attempt(job, case):
    from datetime import UTC, datetime, timedelta

    from src.orchestration.environment_leases import LeaseHolder, acquire_lease, shared_release_target

    ctx, broker, body, claims, run = job
    entry = replace(ctx.entry, release_group="atlassian-foundation", mutation_role_arn="arn:aws:iam::000000000000:role/adp-dev-trusted-deployment")
    row = await action(ctx)
    saved = row.detail
    shared = shared_release_target(ctx.target, environment=body.inputs["environment"], group=entry.release_group)
    run["created_at"] = datetime.now(UTC).isoformat()
    if case != "missing":
        async with ctx.factory() as db:
            leased = await acquire_lease(
                db,
                target=shared,
                holder=LeaseHolder(ctx.identity.org_id, "foreign" if case == "foreign" else row.id, ctx.identity.claim_generation),
                manifest_entry_id=entry.entry_id,
                release_ref=saved["source_revision"],
            )
            assert leased.applied
            await db.commit()
        run["created_at"] = (
            datetime.now(UTC) + timedelta(minutes=1)
            if case == "future"
            else datetime.now(UTC) - timedelta(minutes=1)
            if case == "early"
            else datetime.now(UTC)
        ).isoformat()
    if case == "wrong_attempt":
        claims["run_attempt"] = "2"
        run["run_attempt"] = 2
    if case == "valid":
        await broker.require_grouped_lease(row, saved, claims, run, entry, body.inputs)
    else:
        with pytest.raises(HTTPException, match="deployment_credential_authority_refused"):
            await broker.require_grouped_lease(row, saved, claims, run, entry, body.inputs)


async def test_live_job_binds_exact_run_and_selected_role(job):
    ctx, broker, body, claims, _ = job
    row, saved = await broker.authorize(body, claims)
    assert saved["credential_id"] == "delivery-connection"
    assert saved["principal_user_id"] == "human"
    assert (await action(ctx)).detail["credential_job"] == {"run_id": "42", "run_attempt": "1"}
    await broker.authorize(body, claims)  # SDK refresh is idempotent.


@pytest.mark.parametrize(
    "key,value",
    [
        ("repository", "another/repo"),
        ("repository_id", "999"),
        ("event_name", "push"),
        ("workflow_ref", "another/path@refs/heads/main"),
        ("workflow_sha", "a" * 40),
        ("sha", "a" * 40),
        ("ref", "refs/heads/feature"),
        ("run_attempt", "2"),
        ("actor_id", "99"),
    ],
)
async def test_wrong_signed_job_identity_refused(job, key, value):
    _, broker, body, claims, _ = job
    with pytest.raises(HTTPException):
        await broker.authorize(body, {**claims, key: value})


@pytest.mark.parametrize(
    "key,value",
    [
        ("status", "completed"),
        ("path", ".github/workflows/other.yml"),
        ("head_sha", "a" * 40),
        ("display_title", "manual replay"),
        ("triggering_actor", {"id": 99}),
        ("actor", {"id": 99}),
        ("repository", {"id": 999}),
        ("id", 99),
        ("run_attempt", 2),
    ],
)
async def test_provider_run_disagreement_refused(job, key, value):
    _, broker, body, claims, run = job
    run[key] = value
    with pytest.raises(HTTPException):
        await broker.authorize(body, claims)


async def test_source_and_input_selectors_cannot_change_approved_authority(job):
    _, broker, body, claims, _ = job
    for changed in (
        body.model_copy(update={"source_revision": "a" * 40}),
        body.model_copy(update={"inputs": {**body.inputs, "customer_user_id": "another-user"}}),
    ):
        with pytest.raises(HTTPException):
            await broker.authorize(changed, claims)


async def test_second_correlated_run_cannot_take_first_jobs_credentials(job):
    _, broker, body, claims, run = job
    await broker.authorize(body, claims)
    run["id"] = 43
    with pytest.raises(HTTPException):
        await broker.authorize(body, {**claims, "run_id": "43"})


@pytest.mark.parametrize("mutation", ["claim", "lease", "finished", "manifest", "role", "policy"])
async def test_revoked_or_changed_authority_stops_refresh(job, mutation):
    ctx, broker, body, claims, _ = job
    await broker.authorize(body, claims)
    async with ctx.factory() as db:
        if mutation == "claim":
            claim = await db.get(OrchestrationWorkClaim, ctx.identity.claim_id)
            claim.generation += 1
        elif mutation == "lease":
            lease = await db.scalar(select(OrchestrationEnvironmentLease))
            lease.owner_generation += 1
        elif mutation == "finished":
            row = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_workflow"))
            row.detail = {**row.detail, "runtime_verified": True}
        elif mutation == "manifest":
            ctx.entry = replace(ctx.entry, artifact_revision="a" * 40)
        elif mutation == "role":
            resolved = ctx.targets.resolve.return_value
            ctx.targets.resolve.return_value = replace(resolved, role_arn="arn:aws:iam::123456789012:role/different")
        elif mutation == "policy":
            _, claim, _, _ = await state(ctx)
            raw = ctx.store._read(f"TENANT#{ctx.identity.org_id}", f"EXEC#{claim.active_run_id}")
            raw["status"] = {"S": "revoked"}
            ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
        await db.commit()
    with pytest.raises((HTTPException, CycleBlockedError)):
        await broker.authorize(body, claims)
