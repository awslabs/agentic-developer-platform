"""Offline fixtures for selected, scoped DevOps Context evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from l5_services.devops.context import translate_result


SOURCE = {"tenant_id": "tenant-one", "project_id": "project-one",
          "source_connection_id": "connection-one", "record_type": "runbook",
          "record_id": "source-one", "record_revision": "4"}
NATIVE_JOB = {"record_type": "investigation", "record_id": "native-one", "record_revision": "2"}
OBSERVED_AT = "2026-10-26T09:00:00Z"


@pytest.fixture
def result_fixture():
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "visibility": "product_team", "owner_id": "member-one", "relationship_revision": "3"}
    result = SimpleNamespace(tenant_id="tenant-one", result_id="result-one", payload={
        "result_id": "result-one", "run_id": "run-one", "revision": 2, "scope": scope,
        "native_job_ref": NATIVE_JOB, "observed_at": OBSERVED_AT, "service_id": "devops"})
    finding = SimpleNamespace(tenant_id="tenant-one", result_id="result-one",
                              finding_id="finding-one", native_id="provider-finding-one",
                              kind="static_finding", payload={
                                  "native_id": "provider-finding-one", "native_job_ref": NATIVE_JOB,
                                  "claim": "release latency increased", "validation": "supported",
                                  "confidence": 0.8, "source_refs": [SOURCE],
                                  "evidence": [{"artifact_id": "report-one", "version": "2"}]})
    return result, finding


def test_translation_selected_provenance_and_exports(result_fixture):
    result, finding = result_fixture
    native_skill = {"kind": "skill", "asset_id": "skill-one", "version": 5,
                    "source_ref": SOURCE, "visibility": "product_team", "export_status": "verified",
                    "content_ref": {"artifact_id": "skill-export", "version": "5"},
                    "confidence": 0.7}
    topology = {"version": 3, "source_ref": SOURCE, "export_status": "verified",
                "content_ref": {"artifact_id": "topology-export", "version": "3"}}
    original = deepcopy((result.payload, finding.payload, native_skill, topology))
    contribution = translate_result(result, [finding], selected_finding_ids={"finding-one"},
                                    native_assets=[native_skill], topology=topology)

    assert (contribution["scope"]["product_id"], contribution["scope"]["project_id"]) == (
        "product-one", "project-one")
    assert contribution["authority"] == "provider_observation"
    assert (contribution["result_id"], contribution["result_version"], contribution["run_id"]) == (
        "result-one", 2, "run-one")
    assert contribution["native_job_ref"] == NATIVE_JOB
    assert contribution["observed_at"] == OBSERVED_AT
    assert contribution["findings"] == [{"finding_id": "finding-one", "native_id": "provider-finding-one",
                                         "kind": "static_finding", "claim": "release latency increased",
                                         "validation": "supported", "confidence": 0.8,
                                         "source_refs": [SOURCE],
                                         "evidence": [{"artifact_id": "report-one", "version": "2"}]}]
    assert contribution["native_assets"][0]["content_ref"] == native_skill["content_ref"]
    assert contribution["native_assets"][0]["version"] == 5
    assert contribution["topology"] == {"status": "available", "version": 3,
                                        "source_ref": SOURCE, "content_ref": topology["content_ref"]}
    assert (result.payload, finding.payload, native_skill, topology) == original


def test_unqualified_exports_and_profile_remain_unchanged(result_fixture):
    result, finding = result_fixture
    profile = {"facts": [{"id": "fact-one", "value": "approved configuration"}]}
    profile_before = deepcopy(profile)
    asset = {"kind": "memory", "asset_id": "memory-one", "version": 1,
             "source_ref": SOURCE, "visibility": "product_team"}
    contribution = translate_result(result, [finding], selected_finding_ids={"finding-one"},
                                    native_assets=[asset])
    assert contribution["topology"] == {"status": "unavailable",
                                        "reason": "native_export_unqualified"}
    assert contribution["native_assets"] == [{"kind": "memory", "asset_id": "memory-one",
                                              "version": 1, "status": "unavailable",
                                              "reason": "native_export_unqualified"}]
    assert "facts" not in contribution
    assert profile == profile_before
    with pytest.raises(ValueError, match="selected finding not found"):
        translate_result(result, [finding], selected_finding_ids={"missing"})
    foreign = deepcopy(finding)
    foreign.payload["source_refs"] = [{**SOURCE, "project_id": "sibling-project"}]
    with pytest.raises(ValueError, match="scoped provenance"):
        translate_result(result, [foreign], selected_finding_ids={"finding-one"})
    with pytest.raises(ValueError, match="scoped provenance"):
        translate_result(result, [finding], selected_finding_ids={"finding-one"},
                         native_assets=[{**asset, "source_ref": {**SOURCE, "project_id": "sibling-project"}}])


def test_durable_devops_publication_preserves_exact_source_result_and_receipts(result_fixture):
    from datetime import UTC, datetime
    from hashlib import sha256
    import json

    from l5_services.publication.contributions import create_devops_publications, validate_receipt

    detail, finding = result_fixture
    run = SimpleNamespace(tenant_id="tenant-one", product_id="product-one", project_id="project-one",
                          service_id="devops", run_id="run-one", native_job_ref=NATIVE_JOB,
                          input={"capability": "investigation", "sources": [SOURCE]}, manifest=None,
                          admission={"scope": detail.payload["scope"]})
    result = SimpleNamespace(tenant_id=run.tenant_id, run_id=run.run_id, result_id=detail.result_id, version=2)
    publications = create_devops_publications(run, result, detail, [finding], now=datetime.now(UTC))
    assert len(publications) == 2
    assert {item.destination for item in publications} == {"product-context", "engineering-insights"}
    for publication in publications:
        assert publication.state == "pending"
        assert publication.payload["kind"] == "devops_result"
        assert publication.payload["result"] == translate_result(detail, [finding], selected_finding_ids={finding.finding_id})
        assert publication.payload["source_refs"] == [SOURCE]
        assert publication.contribution_hash == sha256(json.dumps(
            publication.payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt = {"receipt_id": "receipt-example", "contribution_id": publication.publication_id,
                   "admitted_schema": "1.0", "consumer_revision": "1", "record_refs": [publication.payload["result_ref"]]}
        assert validate_receipt(publication, receipt) == receipt
        with pytest.raises(ValueError, match="cover this contribution"):
            validate_receipt(publication, {**receipt, "record_refs": []})
    original = deepcopy(publications[0].payload)
    finding.payload["claim"] = "changed after publication"
    detail.payload["scope"]["visibility"] = "only_me"
    assert publications[0].payload == original


@pytest.mark.parametrize("fault", ["foreign_result", "foreign_scope", "foreign_source", "floating_source", "ungrounded", "no_sources"])
def test_devops_contributions_reject_foreign_lineage_and_retain_unavailable_grounding(result_fixture, fault):
    from datetime import UTC, datetime
    from l5_services.publication.contributions import create_devops_publications

    detail, finding = result_fixture
    run = SimpleNamespace(tenant_id="tenant-one", product_id="product-one", project_id="project-one",
                          service_id="devops", run_id="run-one", native_job_ref=NATIVE_JOB,
                          input={"capability": "investigation", "sources": [deepcopy(SOURCE)]}, manifest=None,
                          admission={"scope": deepcopy(detail.payload["scope"])})
    result = SimpleNamespace(tenant_id=run.tenant_id, run_id=run.run_id, result_id=detail.result_id, version=2)
    findings = [finding]
    if fault == "foreign_result":
        result.run_id = "another-run"
    elif fault == "foreign_scope":
        detail.payload["scope"]["product_id"] = "another-product"
    elif fault == "foreign_source":
        finding.payload["source_refs"] = [{**SOURCE, "record_id": "another-source"}]
    elif fault == "floating_source":
        run.input["sources"][0]["record_revision"] = "HEAD"
    elif fault == "ungrounded":
        finding.payload["source_refs"] = []
    elif fault == "no_sources":
        findings = []
        run.input["sources"] = []
    if fault in {"ungrounded", "no_sources"}:
        publications = create_devops_publications(run, result, detail, findings, now=datetime.now(UTC))
        assert len(publications) == 2
        assert all(row.state == "unavailable" and row.last_error == "source_evidence_unavailable" for row in publications)
        assert all(row.payload["result"]["findings"] == [] for row in publications)
    else:
        with pytest.raises(ValueError):
            create_devops_publications(run, result, detail, findings, now=datetime.now(UTC))


@pytest.mark.asyncio
async def test_devops_collection_commits_two_publications_once_with_result(tmp_path):
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from l5_services.models.base import Base
    from l5_services.models.run import Run
    from l5_services.models.work import Work
    from l5_services.models.evidence import AssessmentResult
    from l5_services.models.publication import Publication
    from l5_services.work_store import claim_work, finish_work

    now = datetime.now(UTC)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "owner_id": "owner-one", "relationship_revision": "1", "visibility": "product_team"}
    try:
        async with sessions() as session:
            session.add(Run(tenant_id="tenant-one", run_id="run-one", product_id="product-one", project_id="project-one",
                            service_id="devops", configuration_id="config-one", configuration_revision=1,
                            binding_id="binding-one", trigger_id="manual", binding_generation=1,
                            input={"capability": "release_readiness", "sources": [SOURCE]},
                            admission={"scope": scope}, state="collecting", native_job_ref=NATIVE_JOB,
                            manifest={"sources": [SOURCE]}, deadline_at=now + timedelta(hours=1)))
            session.add(Work(tenant_id="tenant-one", work_id="collect-one", run_id="run-one", kind="collect",
                             generation=1, step=1, due_at=now))
            await session.commit()
        claim = await claim_work(sessions, "collect-one", 1, now=now, service_id="devops")
        result = {"native_job_ref": NATIVE_JOB, "coverage": "complete", "manifest_ref": {"artifact_id": "retained-one"},
                  "findings": [{"native_id": "native-finding", "kind": "static_finding", "claim": "observed latency",
                                "provider_severity": "medium", "validation": "supported", "source_refs": [SOURCE],
                                "evidence": [{"artifact_id": "native-report", "version": "1"}]}]}
        assert await finish_work(sessions, claim, state="succeeded", provider_state="completed", result=result, now=now) == "completed"
        assert await finish_work(sessions, claim, state="succeeded", result=result, now=now) is None
        async with sessions() as session:
            results = (await session.scalars(select(AssessmentResult))).all()
            publications = (await session.scalars(select(Publication))).all()
            assert len(results) == 1 and len(publications) == 2
            assert all(row.result_id == results[0].result_id and row.state == "pending" for row in publications)
            assert {row.destination for row in publications} == {"product-context", "engineering-insights"}
            for row in publications:
                row.state = "published"
                row.receipt = {"receipt_id": "receipt-" + row.destination,
                               "contribution_id": row.publication_id, "admitted_schema": "1.0",
                               "consumer_revision": "1", "record_refs": [row.payload["result_ref"]]}
            await session.commit()
            from l5_services.findings import ResultStore
            from fastapi import HTTPException

            run = await session.get(Run, ("tenant-one", "run-one"))
            authorizations = []

            async def authorize(run, publication):
                authorizations.append(publication.publication_id)
                return True

            store = ResultStore(session, [run], actor_id="owner-one", service_id="devops",
                                contribution_authority=authorize)
            contribution = await store.devops_contribution(results[0].result_id, "product-context")
            assert contribution["kind"] == "devops_result"
            assert contribution["source_refs"] == [SOURCE]
            assert len(authorizations) == 1
            selected = next(row for row in publications if row.destination == "product-context")
            selected.contribution_hash = "0" * 64
            await session.commit()
            with pytest.raises(HTTPException) as rejected:
                await store.devops_contribution(results[0].result_id, "product-context")
            assert rejected.value.status_code == 404 and len(authorizations) == 1
    finally:
        await engine.dispose()
