"""Real SQL recovery rows and router; only current authority boundaries are replaced."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.api.bindings import BindingStore
from l5_services.api.runs import create_runs_router
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.run_access import RunStore
from src.l5_integration import GatewayConfigurationBackend
from src.products import l5_devops_recovery
from src.shared.models.vault import CredentialServiceDelegation


@pytest.fixture
async def selected(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    scope = dict(tenant_id="tenant-one", product_id="product-one", project_id="project-one",
                 owner_id="owner-one", relationship_revision="1", visibility="product_team")
    connection_ref = dict(tenant_id=scope["tenant_id"], project_id=scope["project_id"],
                          source_connection_id="connection-one", record_type="connection",
                          record_id="connection-one", record_revision="1")
    space = {**connection_ref, "record_type": "agent_space", "record_id": "space-one"}
    identity = dict(account_id="123456789012", region="us-east-1", role_arn="arn:aws:iam::123456789012:role/Example")

    async def seed(tenant):
        row_scope = {**scope, "tenant_id": tenant}
        admitted = dict(scope=row_scope, connection_ref=connection_ref, provider_identity=identity,
                        native_space_ref=space, configuration_id="configuration-one", configuration_revision=1)
        async with sessions() as db:
            db.add(Configuration(tenant_id=tenant, configuration_id="configuration-one", product_id="product-one",
                                 project_id="project-one", service_id="devops", owner_id="owner-one", current_revision=1,
                                 relationship_revision="1", handoff_state="linked"))
            db.add(ConfigurationVersion(tenant_id=tenant, configuration_id="configuration-one", revision=1,
                                        settings={}, inherited_fields=[], gaps=[], state="ready", hash="a" * 64))
            db.add(Binding(tenant_id=tenant, binding_id="binding-one", configuration_id="configuration-one",
                           product_id="product-one", project_id="project-one", service_id="devops", scope=row_scope,
                           revision=1, generation=2, state="ready", ownership="adopted", connection_ref=connection_ref,
                           connection_key="c" * 64, native_resource_ref=space, native_key="s" * 64,
                           provider_identity=identity, readiness=[], gaps=[]))
            db.add(Run(tenant_id=tenant, run_id="run-one", product_id="product-one", project_id="project-one",
                       service_id="devops", configuration_id="configuration-one", configuration_revision=1,
                       binding_id="binding-one", binding_generation=2, trigger_id="trigger-one", revision=3,
                       state="outcome_unknown", provider_state="unknown", deadline_at=now + timedelta(hours=1),
                       input={"service_id": "devops", "capability": "investigation", "devops_input": {}},
                       admission=admitted, origin="adp_submitted"))
            db.add(WorkerAttempt(tenant_id=tenant, attempt_id="attempt-one", run_id="run-one", operation="submit",
                                 first_work_id="work-one", first_fence=4, idempotency_token="d" * 64,
                                 input_hash="e" * 64, started_at=now, status="outcome_unknown", reconcile_checks=2,
                                 prepared_request={"private": "must-not-be-exported"}))
            db.add(Work(tenant_id=tenant, work_id="work-one", run_id="run-one", kind="reconcile", generation=3,
                        step=7, fence=5, attempts=2, due_at=now, last_enqueued_at=now, blocked_at=now,
                        last_error="private-worker-error", lease_owner="private-worker-owner"))
            db.add(AssessmentResult(tenant_id=tenant, result_id="result-one", run_id="run-one", version=1,
                                    payload={}))
            for destination, suffix in [("product-context", "one"), ("engineering-insights", "two")]:
                db.add(Publication(tenant_id=tenant, publication_id=f"publication-{suffix}", result_id="result-one",
                                   result_version=1, destination=destination, schema_version="1.0", contribution_hash="f" * 64,
                                   payload={"private": "must-not-be-exported"}, state="published", next_due_at=now,
                                   receipt={"receipt_id": f"receipt-{suffix}"}, attempts=2, revision=4))
            await db.commit()

    await seed("tenant-one")
    await seed("tenant-other")  # Same retained IDs must never cross the tenant boundary.
    grant = SimpleNamespace(revision=2, binding_generation=2, capabilities=["investigation"],
                            revoked_at=None, expires_at=now + timedelta(hours=1))
    actor = SimpleNamespace(org_id="tenant-one", user_id="owner-one", account_type="human",
                            auth_source="jwt", token_use="access", expires_at=now + timedelta(hours=1))
    authority = dict(scope=scope, connection_ref=connection_ref, configuration_id="configuration-one",
                     configuration_revision=1, authorization={"delegation_id": "grant-one", "delegation_revision": 2})
    queries, statements = [], []
    hook = {"callback": None, "owner_allowed": True, "owner_checks": 0}

    async def owner(backend, product_id, project_id, service_id):
        hook["owner_checks"] += 1
        if not hook["owner_allowed"]:
            raise HTTPException(404, detail={"code": "not_found"})

    monkeypatch.setattr(l5_devops_recovery, "_owner", owner)

    def forbid(*args, **kwargs):
        raise AssertionError("recovery readback must not create a provider client")

    monkeypatch.setattr(boto3, "Session", forbid)
    monkeypatch.setattr(boto3, "client", forbid)

    async with sessions() as sql:
        class Database:
            def expire_all(self):
                sql.expire_all()

            async def get(self, model, key, **kwargs):
                assert model is CredentialServiceDelegation and key == "grant-one"
                return grant

            async def scalars(self, statement):
                result = await sql.scalars(statement)
                queries.append(statement)
                if hook["callback"]:
                    await hook["callback"](len(queries))
                return result

        class Backend:
            db = Database()
            devops_recovery_state = GatewayConfigurationBackend.devops_recovery_state

            async def binding_authority(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != ("product-one", "project-one", "devops"):
                    raise HTTPException(404, detail={"code": "not_found"})
                return None, deepcopy(authority)

            async def get_run(self, product_id, project_id, service_id, run_id):
                return await RunStore(sql, actor.org_id, product_id, project_id, service_id, actor.user_id, "1").get(run_id)

            async def binding_store(self, product_id, project_id, service_id):
                return BindingStore(sql, actor.org_id, product_id, project_id, service_id)

        backend = Backend()
        backend.actor = actor
        app = FastAPI()
        app.include_router(create_runs_router(lambda: backend), prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def track_sql(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
            yield SimpleNamespace(client=client, actor=actor, authority=authority, grant=grant, queries=queries,
                                  statements=statements, hook=hook, sessions=sessions,
                                  path="/v1/products/product-one/projects/project-one/services/devops/runs/run-one/recovery-state")
    await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_read_preserves_exact_original_rows_and_two_publication_destinations(selected):
    first = await selected.client.get(selected.path)
    again = await selected.client.get(selected.path)
    assert first.status_code == again.status_code == 200
    assert first.headers["cache-control"] == "no-store" and again.json() == first.json()
    result = first.json()
    assert set(result) == {"run", "attempts", "work", "publications"}
    assert result["run"]["run_id"] == "run-one" and result["run"]["revision"] == 3
    assert result["run"]["scope"]["tenant_id"] == "tenant-one"
    assert len(result["attempts"]) == len(result["work"]) == 1
    assert {key: result["attempts"][0][key] for key in ("attempt_id", "first_work_id", "first_fence", "idempotency_token", "input_hash")} == {
        "attempt_id": "attempt-one", "first_work_id": "work-one", "first_fence": 4,
        "idempotency_token": "d" * 64, "input_hash": "e" * 64}
    assert {key: result["work"][0][key] for key in ("work_id", "generation", "step", "fence", "attempts")} == {
        "work_id": "work-one", "generation": 3, "step": 7, "fence": 5, "attempts": 2}
    assert {item["destination"] for item in result["publications"]} == {"product-context", "engineering-insights"}
    assert all(item["revision"] == 4 and item["attempts"] == 2 for item in result["publications"])
    assert "must-not-be-exported" not in first.text and "private-worker" not in first.text
    assert len(selected.queries) == 12
    assert selected.hook["owner_checks"] == 4
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in selected.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["tenant", "owner", "service_actor", "expired", "naive_expiry", "auth_source", "token_use",
                                  "product", "project", "service", "missing_run", "revoked", "expired_grant", "capability",
                                  "binding_generation", "configuration_revision", "native_origin", "stored_scope", "membership"])
async def test_denied_scope_never_reads_recovery_rows(selected, fault):
    path = selected.path
    if fault in {"tenant", "owner", "service_actor", "auth_source", "token_use"}:
        field, value = {"tenant": ("org_id", "tenant-other"), "owner": ("user_id", "another-owner"),
                        "service_actor": ("account_type", "service"), "auth_source": ("auth_source", "api_key"),
                        "token_use": ("token_use", "id")}[fault]
        setattr(selected.actor, field, value)
    elif fault in {"expired", "naive_expiry"}:
        selected.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1) if fault == "expired" else datetime.now()
    elif fault in {"product", "project", "service", "missing_run"}:
        old, new = {"product": ("product-one", "product-other"), "project": ("project-one", "project-other"),
                    "service": ("/devops/", "/finops/"), "missing_run": ("run-one", "run-absent")}[fault]
        path = path.replace(old, new)
    elif fault == "revoked":
        selected.grant.revoked_at = datetime.now(UTC)
    elif fault == "expired_grant":
        selected.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif fault == "capability":
        selected.grant.capabilities = []
    elif fault == "binding_generation":
        selected.grant.binding_generation += 1
    elif fault == "configuration_revision":
        selected.authority["configuration_revision"] += 1
    elif fault == "membership":
        selected.hook["owner_allowed"] = False
    else:
        async with selected.sessions() as db:
            change = {"origin": "native"} if fault == "native_origin" else {"scope": {"tenant_id": "tenant-other"}}
            await db.execute(update(Run).where(Run.tenant_id == "tenant-one").values(**change))
            await db.commit()
    response = await selected.client.get(path)
    assert response.status_code == 404 and response.json() == {"detail": {"code": "not_found"}}
    assert not selected.queries


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revoked", "expired", "authority", "actor", "membership", "work", "attempt", "publication", "run", "binding"])
async def test_concurrent_authority_or_recovery_change_cannot_return_mixed_proof(selected, fault):
    async def change(count):
        if count != 3:
            return
        if fault == "revoked":
            selected.grant.revoked_at = datetime.now(UTC)
        elif fault == "expired":
            selected.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif fault == "authority":
            selected.authority["authorization"]["delegation_revision"] += 1
        elif fault == "actor":
            selected.actor.user_id = "another-owner"
        elif fault == "membership":
            selected.hook["owner_allowed"] = False
        else:
            model, changes = {"work": (Work, {"fence": 6}), "attempt": (WorkerAttempt, {"idempotency_token": "0" * 64}),
                              "publication": (Publication, {"revision": 5}), "run": (Run, {"revision": 4}),
                              "binding": (Binding, {"generation": 3})}[fault]
            async with selected.sessions() as db:
                await db.execute(update(model).where(model.tenant_id == "tenant-one").values(**changes))
                await db.commit()

    selected.hook["callback"] = change
    response = await selected.client.get(selected.path)
    assert response.status_code == (409 if fault in {"work", "attempt", "publication"} else 404)
    assert "attempt-one" not in response.text


@pytest.mark.asyncio
async def test_bounds_fail_closed_without_truncating_original_rows(selected, monkeypatch):
    monkeypatch.setattr(l5_devops_recovery, "ROW_LIMITS", {"attempts": 20, "work": 8192, "publications": 1})
    response = await selected.client.get(selected.path)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "recovery_readback_incomplete"
    monkeypatch.setattr(l5_devops_recovery, "ROW_LIMITS", {"attempts": 20, "work": 8192, "publications": 20})
    monkeypatch.setattr(l5_devops_recovery, "BYTE_LIMIT", 1)
    response = await selected.client.get(selected.path)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "recovery_readback_incomplete"


@pytest.mark.asyncio
async def test_full_reconciliation_and_polling_history_is_exported_without_truncation(selected):
    now = datetime.now(UTC)
    maximum = 2880  # Canonical Limits.max_polls; reconcile has an independent counter.
    async with selected.sessions() as db:
        run = await db.get(Run, ("tenant-one", "run-one"))
        run.admission = {**run.admission, "limits": {"max_polls": maximum}}
        for phase, offset in (("reconcile", 0), ("poll", maximum)):
            db.add_all(Work(tenant_id="tenant-one", work_id=f"{phase}-{index:04d}", run_id="run-one",
                            kind=phase, generation=3, step=offset + index + 8, fence=1, attempts=1,
                            due_at=now + timedelta(seconds=index * 60), completed_at=now,
                            last_enqueued_at=now) for index in range(maximum))
        db.add(Work(tenant_id="tenant-one", work_id="collect-last", run_id="run-one", kind="collect",
                    generation=3, step=2 * maximum + 8, fence=1, attempts=1, due_at=now))
        await db.commit()
    selected.statements.clear()
    response = await selected.client.get(selected.path)
    assert response.status_code == 200
    record = response.json()
    assert len(record["work"]) == 2 * maximum + 2
    assert {item["work_id"] for item in record["work"]} == {
        "work-one", "collect-last", *(f"{phase}-{index:04d}" for phase in ("reconcile", "poll") for index in range(maximum))}
    assert record["attempts"][0]["idempotency_token"] == "d" * 64
    assert len(record["publications"]) == 2
    assert 65536 < len(response.content) < l5_devops_recovery.BYTE_LIMIT
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in selected.statements)
