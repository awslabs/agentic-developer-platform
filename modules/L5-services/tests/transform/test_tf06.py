"""Deterministic Transform publication, reuse and recurrence scenarios."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from l5_services.publication.contributions import create_transform_publications
from test_tf03_attempts import work_db


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_transform_publication_intents_are_scoped_and_immutable():
    source = {"tenant_id": "tenant-example", "project_id": "project-example",
              "source_connection_id": "repository", "record_type": "commit",
              "record_id": "example/widget", "record_revision": "a" * 40}
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    run = SimpleNamespace(tenant_id="tenant-example", product_id="product-example",
                          project_id="project-example", run_id="run-example", service_id="transform",
                          input={"sources": [source], "transform_input": {"goal": {"kind": "refactor"}}})
    result = SimpleNamespace(tenant_id=run.tenant_id, run_id=run.run_id,
                             result_id="result-example", version=1)
    artifact = {"tenant_id": run.tenant_id, "project_id": run.project_id,
                "artifact_id": "original-patch", "version": "1", "sha256": "b" * 64,
                "expires_at": "2027-01-01T00:00:00Z"}
    detail = SimpleNamespace(tenant_id=run.tenant_id, result_id=result.result_id, payload={
        "run_id": run.run_id, "result_id": result.result_id, "revision": 1,
        "scope": scope, "source_ref": [source], "retained_artifacts": {"original_patch": artifact},
        "retention_expires_at": artifact["expires_at"], "verification_status": "partial",
        "source_trees": {"base_tree": "a" * 40, "candidate_tree": "b" * 40},
    })
    intents = create_transform_publications(run, result, detail, now=NOW)
    assert {intent.destination for intent in intents} == {"product-context", "engineering-insights"}
    assert len({intent.publication_id for intent in intents}) == 2
    assert [intent.publication_id for intent in intents] == [
        intent.publication_id for intent in create_transform_publications(run, result, detail, now=NOW)]
    for intent in intents:
        assert intent.state == "pending" and intent.attempts == 0 and intent.receipt is None
        assert intent.payload["result_ref"]["record_revision"] == "1"
        assert intent.payload["result"]["source_trees"]["candidate_tree"] == "b" * 40
        assert intent.payload["source_refs"] == [source]
        assert intent.payload["evidence_refs"] == [{"kind": "original_patch", "artifact": artifact}]
        assert intent.contribution_hash == sha256(json.dumps(
            intent.payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    original = deepcopy(intents[0].payload)
    detail.payload["source_trees"]["candidate_tree"] = "c" * 40
    assert intents[0].payload == original
    detail.payload["scope"] = {**scope, "project_id": "foreign-project"}
    with pytest.raises(ValueError, match="scope"):
        create_transform_publications(run, result, detail, now=NOW)
    detail.payload["scope"] = {**scope, "visibility": None}
    assert all(intent.state == "unavailable" for intent in create_transform_publications(
        run, result, detail, now=NOW))


@pytest.mark.asyncio
async def test_publication_recovery(tmp_path):
    import httpx
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.models import AssessmentResult, Configuration, ConfigurationVersion, Publication, ResultDetail, Run, Work
    from l5_services.models.base import Base
    from l5_services.publication.client import TransformConsumerClient
    from l5_services.workers.relay import repair_once

    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "source_connection_id": "repository", "record_type": "repository_commit",
              "record_id": "example/widget", "record_revision": "a" * 40}
    retained = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                "artifact_id": "retained-patch", "version": "1", "sha256": "b" * 64,
                "expires_at": "2027-01-01T00:00:00Z"}
    run = Run(tenant_id=scope["tenant_id"], product_id=scope["product_id"],
              project_id=scope["project_id"], service_id="transform", run_id="run-example",
              configuration_id="config-example", configuration_revision=1,
              binding_id="binding-example", binding_generation=1, trigger_id="manual-example",
              deadline_at=NOW.replace(year=2027), state="partial", provider_state="completed",
              input={"sources": [source], "transform_input": {"goal": {"kind": "refactor"}}},
              admission={"scope": scope})
    result = AssessmentResult(tenant_id=run.tenant_id, run_id=run.run_id,
                              result_id="result-example", version=1, payload={"original_patch": retained})
    detail = ResultDetail(tenant_id=run.tenant_id, result_id=result.result_id, payload={
        "run_id": run.run_id, "result_id": result.result_id, "revision": 1,
        "scope": scope, "source_ref": [source], "retained_artifacts": {"original_patch": retained},
        "retention_expires_at": retained["expires_at"], "verification_status": "partial",
        "source_trees": {"base_tree": "a" * 40, "candidate_tree": "b" * 40},
    })
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    admitted = {}
    post_counts = {"product-context": 0, "engineering-insights": 0}
    lost_ack = True

    def peer(request):
        nonlocal lost_ack
        destination = "product-context" if "/context/" in request.url.path else "engineering-insights"
        assert request.headers["x-l5-delegation-id"] == f"grant:{destination}"
        assert "l5-transform-contributions" in request.url.path
        if request.method == "POST":
            contribution = json.loads(request.content)
            assert contribution["kind"] == "transform_result"
            assert request.headers["idempotency-key"] == (
                f"tenant-example/result-example/1/{destination}/1.0")
            post_counts[destination] += 1
            key = (destination, contribution["contribution_id"])
            if key in admitted:
                assert admitted[key][0] == contribution
            receipt = {"receipt_id": f"receipt-{destination}",
                       "contribution_id": contribution["contribution_id"],
                       "admitted_schema": "1.0", "consumer_revision": "1",
                       "record_refs": [contribution["result_ref"]]}
            admitted[key] = (contribution, receipt)
            if destination == "product-context" and lost_ack:
                lost_ack = False
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return httpx.Response(201, json=receipt)
        publication_id = request.url.path.split("/")[-2 if request.url.path.endswith("/projection") else -1]
        key = (destination, publication_id)
        if key not in admitted:
            return httpx.Response(404)
        contribution, receipt = admitted[key]
        if request.url.path.endswith("/projection"):
            return httpx.Response(200, json={**contribution, "contribution_hash": sha256(json.dumps(
                contribution, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
        return httpx.Response(200, json=receipt)

    async def forbidden_provider(*args):
        raise AssertionError("publication must not dispatch provider work")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id=run.tenant_id, configuration_id=run.configuration_id,
                              product_id=run.product_id, project_id=run.project_id,
                              service_id="transform", owner_id=scope["owner_id"],
                              relationship_revision="1", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id=run.tenant_id, configuration_id=run.configuration_id,
                                     revision=1, settings={}, inherited_fields=[], gaps=[], state="ready",
                                     hash="a" * 64), run, result, detail,
                *create_transform_publications(run, result, detail, now=NOW),
            ])
            await session.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(peer), base_url="https://consumer.test") as transport:
            clients = {destination: TransformConsumerClient(
                transport, f"grant:{destination}", scope=scope, destination=destination,
                private_read_fencing=True) for destination in post_counts}
            service = SimpleNamespace(sessions=sessions, clock=lambda: NOW, publish=forbidden_provider,
                                      context_client=None, transform_context_client=clients["product-context"],
                                      transform_insights_client=clients["engineering-insights"])
            assert await repair_once(service, now=NOW) == 2
            async with sessions() as session:
                rows = (await session.scalars(select(Publication))).all()
                assert {row.destination: row.state for row in rows} == {
                    "product-context": "retry_wait", "engineering-insights": "published"}
                assert next(row for row in rows if row.destination == "product-context").receipt is None
                assert await session.scalar(select(func.count()).select_from(Work)) == 0
            retry_at = NOW.replace(minute=2)
            assert await repair_once(service, now=retry_at) == 1
            assert await repair_once(service, now=retry_at) == 0
            assert post_counts == {"product-context": 1, "engineering-insights": 1}
            async with sessions() as session:
                rows = (await session.scalars(select(Publication))).all()
                assert {row.destination for row in rows} == set(post_counts)
                for row in rows:
                    assert row.state == "published"
                    assert row.receipt["receipt_id"] == f"receipt-{row.destination}"
                    contribution, receipt = admitted[(row.destination, row.publication_id)]
                    assert row.receipt == receipt
                    assert row.contribution_hash == sha256(json.dumps(
                        contribution, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    assert row.payload == contribution
                stored = (await session.scalars(select(Run))).one()
                assert (stored.state, stored.revision, stored.provider_state) == ("partial", 1, "completed")
                assert await session.scalar(select(func.count()).select_from(AssessmentResult)) == 1
                assert await session.scalar(select(func.count()).select_from(Work)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transform_consumer_requires_scope_and_matching_projection():
    import httpx

    from l5_services.publication.client import ContextClient, TransformConsumerClient
    from l5_services.publication.contributions import validate_receipt

    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example"}
    reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "source_connection_id": "l5-transform", "record_type": "transform_result",
                 "record_id": "result-example", "record_revision": "1"}
    publication = SimpleNamespace(tenant_id=scope["tenant_id"], result_id="result-example",
                                  result_version=1, destination="product-context", schema_version="1.0",
                                  publication_id="contribution-example", contribution_hash="a" * 64,
                                  state="pending", payload={"kind": "transform_result", "scope": scope,
                                      "result_ref": reference, "result": {"revision": 1},
                                      "goal": {"kind": "refactor"}, "source_refs": [],
                                      "evidence_refs": [], "contribution_id": "contribution-example"})
    receipt = {"receipt_id": "receipt-example", "contribution_id": publication.publication_id,
               "admitted_schema": "1.0", "consumer_revision": "1", "record_refs": [reference]}
    def peer(request):
        if request.method == "POST":
            return httpx.Response(201, json=receipt)
        if request.url.path.endswith("/projection"):
            return httpx.Response(200, json={**publication.payload,
                                             "contribution_hash": "changed-digest"})
        return httpx.Response(200, json=receipt)

    async with httpx.AsyncClient(transport=httpx.MockTransport(peer), base_url="https://consumer.test") as transport:
        client = TransformConsumerClient(transport, "grant-example", destination="product-context",
                                         scope=scope, private_read_fencing=True)
        assert not TransformConsumerClient(transport, "grant-example", destination="product-context",
                                           scope=scope).can_publish(publication)
        assert not TransformConsumerClient(transport, "grant-example", destination="product-context",
                                           scope={**scope, "project_id": "foreign"},
                                           private_read_fencing=True).can_publish(publication)
        assert not ContextClient(transport, "grant-example", private_read_fencing=True).can_publish(publication)
        with pytest.raises(ValueError, match="projection differs"):
            await client.publish(publication)
        with pytest.raises(ValueError, match="cover this contribution"):
            validate_receipt(publication, {**receipt, "record_refs": [{**reference, "record_id": "other"}]})


@pytest.mark.asyncio
async def test_entitled_later_reuse(work_db):
    import httpx
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import func, select

    from l5_services.api.results import create_results_router
    from l5_services.findings import ResultStore
    from l5_services.models import AssessmentResult, Publication, ResultDetail, Run, Work
    from l5_services.publication.contributions import acknowledge
    from l5_services.run_access import RunStore
    from test_tf03_attempts import SOURCE

    scope = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
             "owner_id": "owner-a", "relationship_revision": "1", "visibility": "product_team"}
    artifact = {"tenant_id": "tenant-a", "project_id": "project-a", "artifact_id": "original-patch",
                "version": "1", "sha256": "b" * 64, "expires_at": "2027-01-01T00:00:00Z"}
    current = {"tenant_id": "tenant-a", "actor_id": "later-task-actor", "sources_allowed": True,
               "relationship_revision": "1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.admission = {**run.admission, "scope": scope}
        run.state = "partial"
        result = AssessmentResult(tenant_id="tenant-a", run_id=run.run_id,
                                  result_id="result-later", version=1, payload={"original_patch": artifact})
        detail = ResultDetail(tenant_id="tenant-a", result_id=result.result_id, payload={
            "schema_version": "1.0", "service_id": "transform", "run_id": run.run_id,
            "result_id": result.result_id, "revision": 1, "scope": scope,
            "source_ref": [SOURCE], "retained_artifacts": {"original_patch": artifact},
            "retention_expires_at": artifact["expires_at"], "verification_status": "partial",
            "source_trees": {"base_tree": "a" * 40, "candidate_tree": "b" * 40},
        })
        session.add_all([result, detail])
        await session.flush()
        publications = create_transform_publications(run, result, detail, now=NOW)
        for publication in publications:
            acknowledge(publication, {"receipt_id": f"receipt-{publication.destination}",
                                      "contribution_id": publication.publication_id,
                                      "admitted_schema": "1.0", "consumer_revision": "1",
                                      "record_refs": [publication.payload["result_ref"]]})
        session.add_all(publications)
        await session.commit()

    class Backend:
        def __init__(self, session):
            self.session = session

        async def result_store(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id) != ("product-a", "project-a", "transform"):
                raise HTTPException(404, detail={"code": "not_found"})
            runs = await RunStore(self.session, current["tenant_id"], product_id, project_id,
                                  service_id, current["actor_id"],
                                  current["relationship_revision"]).list()
            if not current["sources_allowed"]:
                runs = []
            async def contribution_authority(run, publication):
                return current["sources_allowed"]

            return ResultStore(self.session, runs, actor_id=current["actor_id"], clock=lambda: NOW,
                               contribution_authority=contribution_authority)

    async def backend():
        async with work_db() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_results_router(backend),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    path = ("/v1/products/product-a/projects/project-a/services/transform/"
            "results/result-later/transform-contributions/product-context")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://local.test") as client:
        assert (await client.get(path.replace("product-a", "foreign-product"))).status_code == 404
        assert (await client.get(path.replace("transform", "security"))).status_code == 404
        assert (await client.get(path.rsplit("/", 1)[0] + "/unknown")).status_code == 404
        assert (await client.get(path.split("/transform-contributions/", 1)[0])).status_code == 503
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        contribution = response.json()
        assert contribution["result"]["source_trees"]["candidate_tree"] == "b" * 40
        assert contribution["source_refs"] == [SOURCE]
        assert contribution["citation"] == {
            "result_ref": contribution["result_ref"],
            "evidence_refs": [{"kind": "original_patch", "artifact": artifact}]}
        use_trace = {"task_id": "later-task", "goal": contribution["goal"],
                     "candidate_source": contribution["result"]["source_trees"]["candidate_tree"],
                     "citation": contribution["citation"]}
        assert use_trace["task_id"] != run.trigger_id
        assert use_trace["citation"]["result_ref"]["record_revision"] == "1"
        assert (await client.get(path.replace("product-context", "engineering-insights"))).json()[
            "citation"] == use_trace["citation"]
        current["relationship_revision"] = "2"
        assert (await client.get(path)).status_code == 404
        current["relationship_revision"] = "1"
        async with work_db() as session:
            publication = (await session.scalars(select(Publication).where(
                Publication.destination == "product-context"))).one()
            publication.state = "pending"
            await session.commit()
        assert (await client.get(path)).status_code == 404
        async with work_db() as session:
            publication = (await session.scalars(select(Publication).where(
                Publication.destination == "product-context"))).one()
            publication.state = "published"
            await session.commit()
        assert (await client.get(path)).status_code == 200
        for changed in ({"tenant_id": "foreign-tenant"}, {"actor_id": None},
                        {"relationship_revision": "2"}, {"sources_allowed": False}):
            original = current.copy()
            current.update(changed)
            assert (await client.get(path)).status_code == 404
            current.update(original)
        async with work_db() as session:
            publication = (await session.scalars(select(Publication).where(
                Publication.destination == "product-context"))).one()
            publication.state = "invalidation_pending"
            publication.invalidation_watermark = 1
            await session.commit()
        assert (await client.get(path)).status_code == 404
        async with work_db() as session:
            publication = (await session.scalars(select(Publication).where(
                Publication.destination == "engineering-insights"))).one()
            publication.payload = {**publication.payload, "goal": {"kind": "changed"}}
            await session.commit()
        assert (await client.get(path.replace("product-context", "engineering-insights"))).status_code == 404
        async with work_db() as session:
            assert await session.scalar(select(func.count()).select_from(AssessmentResult)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finops_gate", ["false", "true"])
async def test_transform_schedule_selection(tmp_path, monkeypatch, finops_gate):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.events.schedules import receive_schedule_tick
    from l5_services.models import Binding, Configuration, ConfigurationVersion, EventReceipt, Run
    from l5_services.models.base import Base
    from test_tf03_attempts import SOURCE

    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", finops_gate)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schedule.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    goal = {"kind": "refactor", "summary": "Simplify the example widget"}
    trigger = {"trigger_id": "hourly-transform", "kind": "schedule", "enabled": True,
               "schedule": "0 * * * *", "capability": "refactor", "binding_id": "binding-a",
               "goal": goal, "source_refs": [SOURCE]}
    options = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
               "trigger_id": "hourly-transform", "service_id": "transform"}
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                              project_id="project-a", service_id="transform", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "triggers": [trigger], "source_refs": [SOURCE]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id="tenant-a", binding_id="binding-a", configuration_id="config-a",
                        product_id="product-a", project_id="project-a", service_id="transform",
                        scope={"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"},
                        revision=1, generation=3, state="ready", ownership="customer_linked",
                        connection_ref={"record_id": "connection-a"}, connection_key="a" * 64,
                        readiness=[], gaps=[]),
            ])
            await session.commit()
            receipt = await receive_schedule_tick(session, **options, scheduled_at=NOW, now=NOW)
            assert receipt.state == "pending" and receipt.reason is None
            assert receipt.event_kind == "schedule"
            assert receipt.run_input == {
                "service_id": "transform", "configuration_revision": 1, "binding_id": "binding-a",
                "capability": "refactor", "sources": [SOURCE], "transform_input": {"goal": goal}}
            assert receipt.target == {**trigger, "binding_generation": 3,
                                      "configuration_revision": 1, "scheduled_at": "2026-01-01T00:00:00Z",
                                      "pinned_source": SOURCE}
            assert receipt.event_id == "schedule:2026-01-01T00:00:00Z:revision:1"
            assert receipt.idempotency_key
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 1
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
            assert await receive_schedule_tick(session, **options, scheduled_at=NOW, now=NOW) is receipt
        async with sessions() as session:
            original = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "transform",
                                                       "hourly-transform", receipt.event_id))
            assert original.target == receipt.target and original.run_input == receipt.run_input
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transform_occurrence_pins_branch_once(work_db):
    from datetime import timedelta, timezone

    from sqlalchemy import func, select

    from l5_services.events.schedules import receive_schedule_tick
    from l5_services.models import Binding, Configuration, ConfigurationVersion, EventReceipt, Run, Work
    from test_tf03_attempts import SOURCE

    selector = {**SOURCE, "record_type": "repository_branch", "record_revision": "main"}
    trigger = {"trigger_id": "branch-transform", "kind": "schedule", "enabled": True,
               "schedule": "0 * * * *", "capability": "refactor", "binding_id": "binding-schedule",
               "goal": {"kind": "refactor", "summary": "Modernize a pinned commit"},
               "source_policy": "branch", "branch_selector": selector, "source_refs": [SOURCE]}
    options = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
               "service_id": "transform", "trigger_id": "branch-transform"}
    async with work_db() as session:
        version = await session.get(ConfigurationVersion, ("tenant-a", "config-a", 1))
        version.settings = {"enabled": True, "triggers": [trigger], "source_refs": [SOURCE]}
        session.add(Binding(tenant_id="tenant-a", binding_id="binding-schedule",
                            configuration_id="config-a", product_id="product-a", project_id="project-a",
                            service_id="transform", scope={"tenant_id": "tenant-a", "product_id": "product-a",
                                                           "project_id": "project-a"},
                            revision=1, generation=1, state="ready", ownership="customer_linked",
                            connection_ref={"record_id": "repo"}, connection_key="a" * 64,
                            readiness=[], gaps=[]))
        await session.commit()

    resolved = ["b" * 40]
    calls = []

    async def resolve_branch(**kwargs):
        calls.append(kwargs)
        assert kwargs["branch_selector"] == selector
        assert kwargs["scheduled_at"] == NOW
        return {**SOURCE, "record_revision": resolved[0]}

    async with work_db() as session:
        receipt = await receive_schedule_tick(session, **options, scheduled_at=NOW,
                                              now=NOW, resolve_branch=resolve_branch)
        assert receipt.state == "pending" and receipt.reason is None
        assert receipt.target["pinned_source"]["record_revision"] == "b" * 40
        assert receipt.run_input["sources"] == [{**SOURCE, "record_revision": "b" * 40}]
        assert receipt.event_id.endswith(":revision:1")
        assert calls[0]["configuration_revision"] == 1
        resolved[0] = "c" * 40
        replay = await receive_schedule_tick(session, **options,
                                             scheduled_at=NOW.astimezone(timezone(timedelta(hours=2))),
                                             now=NOW, resolve_branch=resolve_branch)
        assert replay.event_id == receipt.event_id and replay.run_input == receipt.run_input
        assert len(calls) == 1
        configuration = await session.get(Configuration, ("tenant-a", "config-a"))
        configuration.current_revision = 2
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a",
                                         revision=2, settings={"enabled": True, "triggers": [trigger],
                                                               "source_refs": [SOURCE]},
                                         inherited_fields=[], gaps=[], state="ready", hash="c" * 64))
        await session.commit()
        revised = await receive_schedule_tick(session, **options, scheduled_at=NOW,
                                              now=NOW, resolve_branch=resolve_branch)
        assert revised.event_id.endswith(":revision:2") and revised.event_id != receipt.event_id
        assert revised.run_input["sources"] == [{**SOURCE, "record_revision": "c" * 40}]
        assert len(calls) == 2
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(Work)) == 1
        configuration.current_revision = 3
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a",
                                         revision=3, settings={"enabled": True, "triggers": [trigger],
                                                               "source_refs": [SOURCE]},
                                         inherited_fields=[], gaps=[], state="ready", hash="d" * 64))
        await session.commit()
        unavailable = await receive_schedule_tick(session, **options, scheduled_at=NOW, now=NOW)
        assert (unavailable.state, unavailable.reason, unavailable.run_input) == (
            "rejected", "source_resolution_unavailable", None)
        assert len(calls) == 2
        configuration.current_revision = 4
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a",
                                         revision=4, settings={"enabled": True, "triggers": [trigger],
                                                               "source_refs": [SOURCE]},
                                         inherited_fields=[], gaps=[], state="ready", hash="e" * 64))
        await session.commit()

        async def foreign_branch(**kwargs):
            return {**SOURCE, "record_id": "foreign-repository", "record_revision": "d" * 40}

        foreign = await receive_schedule_tick(session, **options, scheduled_at=NOW,
                                              now=NOW, resolve_branch=foreign_branch)
        assert (foreign.state, foreign.reason, foreign.run_input) == (
            "rejected", "source_scope_conflict", None)
        assert await session.scalar(select(func.count()).select_from(Run)) == 1


@pytest.fixture
async def recurring_transform(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.models import Binding, Configuration, ConfigurationVersion
    from l5_services.models.base import Base
    from test_tf03_attempts import SOURCE
    from test_tf06_admission import GOAL, SCOPE

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recurrence.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    pinned = {**SOURCE, "record_revision": "b" * 40}
    branch = {**SOURCE, "record_type": "repository_branch", "record_revision": "main"}
    trigger = {"trigger_id": "branch-transform", "kind": "schedule", "enabled": True,
               "schedule": "0 * * * *", "capability": "refactor", "binding_id": "binding-a",
               "goal": GOAL, "source_policy": "branch", "branch_selector": branch,
               "source_refs": [SOURCE]}
    manual = {"trigger_id": "manual-transform", "kind": "manual", "enabled": True,
              "capability": "refactor", "binding_id": "binding-a", "source_refs": [pinned]}
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                              project_id="project-a", service_id="transform", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "triggers": [trigger, manual],
                                               "source_refs": [SOURCE, pinned]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id="tenant-a", binding_id="binding-a", configuration_id="config-a",
                        product_id="product-a", project_id="project-a", service_id="transform",
                        scope=SCOPE, revision=1, generation=3, state="ready", ownership="customer_linked",
                        connection_ref=SOURCE, connection_key="a" * 64, readiness=[], gaps=[]),
            ])
            await session.commit()
        yield sessions, trigger, manual, pinned
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recurrence_admission(recurring_transform):
    from datetime import timedelta, timezone
    from types import SimpleNamespace

    from sqlalchemy import func, select

    from l5_services.admission import submit_and_dispatch
    from l5_services.events.recovery import repair_pending_events
    from l5_services.events.schedules import receive_schedule_tick
    from l5_services.events.sources import admit_source_receipt
    from l5_services.models import Configuration, ConfigurationVersion, EventReceipt, Run, Work
    from l5_services.models.receipt import RunReceipt
    from test_tf03_attempts import SOURCE
    from test_tf06_admission import SCOPE, SourceClient, authority

    sessions, trigger, manual_trigger, pinned = recurring_transform
    source_client = SourceClient()
    branch_calls = []
    budget_checks = []
    published = []
    head = [pinned["record_revision"]]
    options = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
               "service_id": "transform", "trigger_id": "branch-transform"}

    async def resolve_branch(**kwargs):
        branch_calls.append(kwargs)
        return {**SOURCE, "record_revision": head[0]}

    async def verify_target(receipt):
        verified = authority(receipt)
        verified.update(trigger=deepcopy(trigger), source_refs=[pinned])
        return verified, source_client

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append((deepcopy(limits), deepcopy(scope), now))
        return await replay()

    async def publish(envelope, delay):
        published.append((envelope, delay))

    async with sessions() as session:
        receipt = await receive_schedule_tick(session, **options, scheduled_at=NOW,
                                              now=NOW, resolve_branch=resolve_branch)
        assert (receipt.state, receipt.reason, receipt.run_input["sources"]) == (
            "pending", None, [pinned])
        assert receipt.target["pinned_source"] == pinned
        assert branch_calls == [{"tenant_id": "tenant-a", "product_id": "product-a",
                                 "project_id": "project-a", "branch_selector": trigger["branch_selector"],
                                 "scheduled_at": NOW, "configuration_revision": 1}]
        head[0] = "c" * 40
        duplicate = await receive_schedule_tick(
            session, **options, scheduled_at=NOW.astimezone(timezone(timedelta(hours=2))),
            now=NOW, resolve_branch=resolve_branch)
        assert duplicate.event_id == receipt.event_id and duplicate.run_input["sources"] == [pinned]
        assert len(branch_calls) == 1

        manual_authority = {**authority(receipt), "actor_id": "owner-a", "actor_type": "human",
                            "trigger": manual_trigger, "event_id": None, "source_refs": [pinned]}
        manual = await submit_and_dispatch(session, sessions, source_client, publish,
                                           receipt.run_input, manual_authority, "manual-once",
                                           now=NOW, budget_guard=budget_guard)

    service = SimpleNamespace(sessions=sessions, verify_event_target=verify_target,
                              budget_guard=budget_guard, publish=publish)
    assert await repair_pending_events(service, now=NOW) == 1
    assert await repair_pending_events(service, now=NOW) == 0
    async with sessions() as session:
        stored = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "transform",
                                                  "branch-transform", receipt.event_id))
        scheduled = await session.get(Run, ("tenant-a", stored.run_id))
        assert stored.state == "admitted" and stored.recovery_attempts == 1
        assert manual.run_id != scheduled.run_id
        assert scheduled.admission["event_id"] == receipt.event_id
        assert "event_id" not in manual.admission
        for run in (manual, scheduled):
            assert run.input["sources"] == [pinned]
            assert run.input["transform_input"]["goal"] == trigger["goal"]
            assert run.admission["source_snapshots"][0]["source_ref"] == pinned
        assert budget_checks == [({"deadline_seconds": 3600}, SCOPE, NOW)] * 2
        assert [capture[2]["source_ref"] for capture in source_client.captures] == [pinned, pinned]
        assert source_client.captures[0][3] != source_client.captures[1][3]
        assert len(published) == 2 and len(branch_calls) == 1
        assert await session.scalar(select(func.count()).select_from(Run)) == 2
        assert await session.scalar(select(func.count()).select_from(Work)) == 2
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 2

        configuration = await session.get(Configuration, ("tenant-a", "config-a"))
        configuration.current_revision = 2
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=2,
                                         settings={"enabled": False, "triggers": [trigger]},
                                         inherited_fields=[], gaps=[], state="disabled", hash="d" * 64))
        await session.commit()
        future = await receive_schedule_tick(session, **options, scheduled_at=NOW + timedelta(hours=1),
                                             now=NOW + timedelta(hours=1), resolve_branch=resolve_branch)
        assert (future.state, future.reason, future.run_input) == ("rejected", "trigger_disabled", None)
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert await session.scalar(select(func.count()).select_from(Run)) == 2
        assert await session.scalar(select(func.count()).select_from(Work)) == 2
        assert len(source_client.captures) == len(published) == len(budget_checks) == 2
        assert len(branch_calls) == 1
