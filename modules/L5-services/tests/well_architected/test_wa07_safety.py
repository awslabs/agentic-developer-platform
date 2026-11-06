"""Local WA07 safety regressions; provider effects need installed readback."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.api.bindings import BindingStore, _digest
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.models.profile_claim import ProfileClaim
from l5_services.models.run import Run
from l5_services.source_retention import pin_source_hold, release_source_hold, source_cleanup_allowed
from l5_services.wa_fence import active_generation


NOW = datetime(2026, 10, 29, tzinfo=UTC)
IDENTITY = {"account_id": "000000000000", "region": "us-east-1"}
PROFILE = "arn:aws:wellarchitected:us-east-1:000000000000:agent-profile/owned-example"


def run_for(tenant, run_id, binding_id="binding-example"):
    return Run(tenant_id=tenant, run_id=run_id, product_id="product-example",
               project_id="project-example", service_id="well_architected",
               configuration_id="configuration-example", configuration_revision=1,
               binding_id=binding_id, binding_generation=1, trigger_id="manual",
               input={"capability": "architecture_review"},
               state="failed", provider_state="not_started", deadline_at=NOW + timedelta(hours=1))


@pytest.mark.asyncio
async def test_durable_unknown_start_blocks_scope_change_and_owned_profile_cleanup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    binding = SimpleNamespace(binding_id="binding-example", native_resource_ref={"record_id": PROFILE},
                              provider_identity=IDENTITY)
    try:
        async with sessions() as session:
            session.add(run_for("tenant-example", "run-example"))
            session.add(ProfileClaim(service_id="well_architected",
                                     native_identity_key=_digest((IDENTITY["account_id"], IDENTITY["region"], PROFILE)),
                                     tenant_id="tenant-example", product_id="product-example",
                                     project_id="project-example"))
            await session.commit()
            store = BindingStore(session, "tenant-example", "product-example", "project-example", "well_architected")
            assert not await active_generation(session, "tenant-example", binding_id="binding-example")
            await store._wa_cleanup_safe(binding)
            session.add(WorkerAttempt(tenant_id="tenant-example", run_id="run-example", attempt_id="attempt-example",
                                      operation="submit", first_work_id="work-example", first_fence=1,
                                      idempotency_token="a" * 64, input_hash="b" * 64,
                                      started_at=NOW, status="outcome_unknown"))
            await session.commit()
            assert await active_generation(session, "tenant-example", binding_id="binding-example")
            with pytest.raises(ValueError, match="native_run_not_terminal"):
                await store._wa_cleanup_safe(binding)
            assert await session.get(Run, ("tenant-example", "run-example")) is not None
            attempt = await session.get(WorkerAttempt, ("tenant-example", "attempt-example"))
            attempt.status = "definitive_failure"
            await session.commit()
            assert not await active_generation(session, "tenant-example", binding_id="binding-example")
            await store._wa_cleanup_safe(binding)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_source_remains_held_until_foreign_native_run_is_terminal():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    artifact = {"artifact_id": "shared-immutable-archive", "version": "version-example", "sha256": "a" * 64}
    try:
        async with sessions() as session:
            holds = []
            for tenant in ("tenant-owner", "tenant-foreign"):
                run = run_for(tenant, f"run-{tenant}")
                run.native_job_ref = {"generation_id": f"generation-{tenant}"}
                session.add(run)
                await session.flush()
                hold = await pin_source_hold(session, run, artifact, required_until=NOW + timedelta(days=1),
                                             policy_expires_at=NOW + timedelta(days=2), now=NOW)
                holds.append((run, hold))
            await session.commit()
            owner, owner_hold = holds[0]
            foreign, foreign_hold = holds[1]
            terminal = NOW + timedelta(days=1)
            release_source_hold(owner_hold, owner, {"tenant_id": owner.tenant_id, "run_id": owner.run_id,
                                                    "generation_id": owner.native_job_ref["generation_id"],
                                                    "provider_state": "completed", "revision": 1}, now=terminal)
            await session.commit()
            assert not await source_cleanup_allowed(session, owner.tenant_id, artifact, now=terminal)
            assert not await source_cleanup_allowed(session, "unrelated-tenant", artifact, now=terminal)
            release_source_hold(foreign_hold, foreign, {"tenant_id": foreign.tenant_id, "run_id": foreign.run_id,
                                                        "generation_id": foreign.native_job_ref["generation_id"],
                                                        "provider_state": "completed", "revision": 1}, now=terminal)
            await session.commit()
            assert await source_cleanup_allowed(session, owner.tenant_id, artifact, now=terminal)
    finally:
        await engine.dispose()
