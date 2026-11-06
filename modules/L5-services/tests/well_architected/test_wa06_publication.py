"""Scoped review publication debt is read from durable consumer receipts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from l5_services.api.router import create_router
from l5_services.findings import ResultStore
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def authoritative_publication_journey(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "relationship_revision": "1"}
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "source_connection_id": "connection-example", "record_type": "document",
              "record_id": "source-example", "record_revision": "1"}
    detail = {"kind": "well_architected_result", "result_id": "result-example", "revision": 2,
              "run_id": "run-example", "scope": scope, "review_mode": "source_only",
              "coverage": [{"path": "source-example"}], "recommendations": [], "limitations": ["source_only"]}
    try:
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id=scope["tenant_id"], configuration_id="config-example",
                              product_id=scope["product_id"], project_id=scope["project_id"],
                              service_id="well_architected", relationship_revision="1",
                              owner_id="owner-example", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="config-example",
                                     revision=1, settings={}, inherited_fields=[], gaps=[],
                                     state="ready", hash="a" * 64),
            ])
            await session.flush()
            session.add(Run(tenant_id=scope["tenant_id"], run_id="run-example", product_id=scope["product_id"],
                            project_id=scope["project_id"], service_id="well_architected",
                            configuration_id="config-example", configuration_revision=1,
                            binding_id="binding-example", trigger_id="manual-example", binding_generation=1,
                            input={"sources": [source]}, admission={"scope": scope},
                            deadline_at=now + timedelta(minutes=5)))
            await session.flush()
            session.add(AssessmentResult(tenant_id=scope["tenant_id"], result_id="result-example",
                                         run_id="run-example", version=2, payload={}))
            await session.flush()
            session.add(ResultDetail(tenant_id=scope["tenant_id"], result_id="result-example", payload=detail))
            for destination, state in (("product-context", "unavailable"),
                                       ("engineering-insights", "published")):
                session.add(Publication(tenant_id=scope["tenant_id"], publication_id=destination,
                                        result_id="result-example", result_version=2, destination=destination,
                                        schema_version="1.0", contribution_hash="b" * 64,
                                        payload={"kind": "well_architected_result"}, state=state,
                                        last_error="consumer_schema_unadmitted" if state == "unavailable" else None,
                                        receipt={"receipt_id": "ack-example", "consumer_revision": "7"}
                                        if state == "published" else None, next_due_at=now))
            await session.commit()

        allowed = [True]

        async def backend():
            async with sessions() as session:
                run = await session.get(Run, (scope["tenant_id"], "run-example"))

                async def source_authority(_run):
                    return allowed[0]

                async def result_store(_product_id, _project_id, _service_id):
                    return ResultStore(session, [run], service_id="well_architected",
                                       wa_source_authority=source_authority)

                yield SimpleNamespace(result_store=result_store)

        app = FastAPI()
        app.include_router(create_router(backend))
        path = "/v1/products/product-example/projects/project-example/services/well_architected/results/result-example/publication-status"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://example.test") as client:
            status = await client.get(path)
            assert status.status_code == 200
            assert status.json() == {"result_id": "result-example", "revision": 2, "consumers": [
                {"destination": "product-context", "state": "unavailable", "publication_id": "product-context",
                 "last_error": "consumer_schema_unadmitted", "projection_verified_on_ack": False},
                {"destination": "engineering-insights", "state": "published", "publication_id": "engineering-insights",
                 "receipt_id": "ack-example", "consumer_revision": "7", "projection_verified_on_ack": True},
            ]}
            foreign = await client.get(path.replace("projects/project-example", "projects/foreign-project"))
            assert foreign.status_code == 404 and "ack-example" not in foreign.text
            allowed[0] = False
            revoked = await client.get(path)
            assert revoked.status_code == 404 and "ack-example" not in revoked.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_review_publication_status_reads_consumer_debt_and_denies_revoked_sources(tmp_path):
    await authoritative_publication_journey(tmp_path)
