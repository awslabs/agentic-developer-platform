"""Independent SQL readback of Security publication identity and receipts."""

import json
import asyncio
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from l5_services.events.handler import receive_document_event
from l5_services.models import AssessmentResult, ConfigurationVersion, Finding, Publication, ResultDetail, Run, Work
from l5_services.publication.client import ContextClient
from l5_services.publication.contributions import acknowledge, create_publication
from l5_services.publication.invalidation import fence_source
from l5_services.publication.recovery import claim_publication, repair_invalidation, repair_publication
from l5_services.work_store import finish_work
from l5_services.workers.execution import ExecutionService
from l5_services.workers.recovery import lambda_handler as repair_handler
from test_results import NOW, collect_result, results_scenario


async def seed_other_identity_scopes(scenario, manifest_ref):
    source = scenario["source"]
    cases = (("tenant-one", "product-other", "owner-other", source),
             ("tenant-two", "product-two", "owner-two", {**source, "tenant_id": "tenant-two"}))
    for tenant_id, product_id, owner_id, scoped_source in cases:
        scoped_manifest = {**manifest_ref,
                           "artifact_id": f"{manifest_ref['artifact_id']}-{tenant_id}-{product_id}",
                           "sha256": sha256(f"{tenant_id}/{product_id}".encode()).hexdigest()}
        await collect_result(scenario, f"run-context-{product_id}", capability="threat_model",
                             tenant_id=tenant_id, product_id=product_id, owner_id=owner_id,
                             source=scoped_source, manifest_ref=scoped_manifest, findings=[{
                                 "native_id": "finding-one", "kind": "candidate_threat",
                                 "claim": "scoped candidate", "provider_severity": "medium",
                                 "validation": "candidate", "source_refs": [scoped_source],
                             }])


async def seed_recovery_scope_controls(scenario, manifest_ref):
    await seed_other_identity_scopes(scenario, manifest_ref)
    async with scenario["sessions"]() as independent:
        publications = (await independent.scalars(select(Publication))).all()
        controls = {}
        for publication in publications:
            if publication.payload["run_id"] == "run-repair":
                continue
            result = await independent.get(AssessmentResult, (publication.tenant_id, publication.result_id))
            work = (await independent.scalars(select(Work).where(
                Work.tenant_id == publication.tenant_id, Work.run_id == result.run_id))).one()
            controls[publication.publication_id] = (
                publication.tenant_id, publication.result_id, result.run_id, result.payload,
                publication.state, publication.attempts, publication.revision, publication.receipt,
                work.work_id, work.completed_at)
        return controls


async def seed_visibility_scope_controls(scenario, manifest_ref):
    await seed_other_identity_scopes(scenario, manifest_ref)
    async with scenario["sessions"]() as independent:
        controls = (await independent.scalars(select(Publication).where(
            Publication.tenant_id.in_(("tenant-one", "tenant-two"))))).all()
        return {(item.tenant_id, item.payload["scope"]["product_id"]): (
            item.publication_id, item.result_id, item.payload["source_refs"],
            item.contribution_hash) for item in controls if item.payload["run_id"] != "run-revoked"}


async def test_contribution_identity(results_scenario):
    source = results_scenario["source"]
    manifest_ref = {"artifact_id": "manifest-one", "version": "1", "sha256": "a" * 64,
                    "media_type": "application/json", "bytes": 4}
    claim, provider_result = await collect_result(
        results_scenario, "run-context", capability="threat_model", manifest_ref=manifest_ref, findings=[
        {"native_id": "finding-one", "kind": "candidate_threat", "claim": "potential exposure",
         "provider_severity": "medium", "validation": "candidate", "source_refs": [source]},
        {"native_id": "finding-two", "kind": "candidate_threat", "claim": "confirmed behavior",
         "provider_severity": "high", "validation": "supported", "source_refs": [source]},
    ])
    await seed_other_identity_scopes(results_scenario, manifest_ref)
    async with results_scenario["sessions"]() as session:
        run = await session.get(Run, ("tenant-one", "run-context"))
        result = await session.scalar(select(AssessmentResult).where(AssessmentResult.run_id == run.run_id))
        detail = await session.get(ResultDetail, (run.tenant_id, result.result_id))
        findings = (await session.scalars(select(Finding).where(Finding.result_id == result.result_id))).all()
        publication = await session.get(Publication, (run.tenant_id,
            create_publication(run, result, detail, findings, now=NOW).publication_id))
        assert publication.state == "pending" and publication.receipt is None
        assert publication.payload["result_ref"]["record_id"] == result.result_id
        assert publication.payload["run_id"] == run.run_id
        assert publication.payload["native_job_ref"] == run.native_job_ref
        assert publication.payload["source_refs"] == [source]
        assert publication.payload["manifest_ref"] == detail.payload["manifest_ref"]
        assert {claim["validation"] for claim in publication.payload["claims"]} == {"candidate", "supported"}
        assert all(claim["visibility"] == "only_me" for claim in publication.payload["claims"])
        assert publication.contribution_hash == create_publication(
            run, result, detail, findings, now=NOW).contribution_hash
        schema = json.loads((Path(__file__).resolve().parents[4] /
                             "docs/l5-services/contracts/v1/schemas.json").read_text())
        assert not list(Draft202012Validator({**schema, "$ref": "#/$defs/Contribution"}).iter_errors(
            publication.payload))
        all_publications = (await session.scalars(select(Publication))).all()
        assert len(all_publications) == len({item.publication_id for item in all_publications}) == 3
        assert len({item.contribution_hash for item in all_publications}) == 3
        for item in all_publications:
            assert not list(Draft202012Validator({**schema, "$ref": "#/$defs/Contribution"}).iter_errors(
                item.payload))
            assert item.payload["scope"]["tenant_id"] == item.tenant_id
            assert item.payload["result_ref"]["record_id"] == item.result_id
            assert all(reference["tenant_id"] == item.tenant_id for reference in item.payload["source_refs"])
        assert await finish_work(results_scenario["sessions"], claim, state="succeeded",
                                 provider_state="completed", now=NOW, result=provider_result) is None

    async with results_scenario["sessions"]() as independent:
        owed = await independent.get(Publication, ("tenant-one", publication.publication_id))
        assert owed.payload == publication.payload and owed.contribution_hash == publication.contribution_hash
        with pytest.raises(ValueError, match="durable receipt"):
            acknowledge(owed, {"status": "ok"})
        assert owed.state == "pending" and owed.receipt is None
        with pytest.raises(ValueError, match="does not cover"):
            acknowledge(owed, {"receipt_id": "receipt-one", "contribution_id": "other",
                            "admitted_schema": "1.0", "consumer_revision": "1", "record_refs": [source]})
        consumer = FastAPI()
        admitted = {}

        @consumer.post("/internal/v1/context/l5-contributions")
        async def accept(contribution: dict, request: Request):
            scope = contribution["scope"]
            if request.headers["x-l5-delegation-id"] != (
                    f"grant:{scope['tenant_id']}:{scope['product_id']}"):
                raise HTTPException(404)
            assert request.headers["idempotency-key"] == (
                f"{scope['tenant_id']}/{contribution['result_ref']['record_id']}/1/product-context/1.0")
            if contribution["contribution_id"] in admitted:
                if admitted[contribution["contribution_id"]][0] != contribution:
                    raise HTTPException(409)
                return admitted[contribution["contribution_id"]][1]
            receipt = {"receipt_id": f"receipt-{scope['tenant_id']}-{scope['product_id']}",
                       "contribution_id": contribution["contribution_id"],
                       "admitted_schema": contribution["schema_version"], "consumer_revision": "1",
                       "record_refs": [contribution["result_ref"]]}
            admitted[contribution["contribution_id"]] = (contribution, receipt)
            return JSONResponse(receipt, status_code=201)

        @consumer.get("/internal/v1/context/l5-contributions/{contribution_id}")
        async def readback(contribution_id: str, request: Request):
            if contribution_id not in admitted:
                raise HTTPException(404)
            scope = admitted[contribution_id][0]["scope"]
            if request.headers["x-l5-delegation-id"] != (
                    f"grant:{scope['tenant_id']}:{scope['product_id']}"):
                raise HTTPException(404)
            return admitted[contribution_id][1]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=consumer),
                                     base_url="http://context.test") as client:
            publisher = ContextClient(client, "grant:tenant-one:product-one", private_read_fencing=True)
            posted = await publisher.publish(owed)
            read = await publisher.read_receipt(owed)
            assert posted == read == await publisher.publish(owed)
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await ContextClient(client, "grant:tenant-two:product-two",
                                    private_read_fencing=True).read_receipt(owed)
            assert denied.value.response.status_code == 404
            with pytest.raises(httpx.HTTPStatusError) as denied_write:
                await ContextClient(client, "grant:tenant-two:product-two",
                                    private_read_fencing=True).publish(owed)
            assert denied_write.value.response.status_code == 404
            for other in all_publications:
                if other.publication_id == owed.publication_id:
                    continue
                scope = other.payload["scope"]
                authorized = ContextClient(client, f"grant:{scope['tenant_id']}:{scope['product_id']}",
                                           private_read_fencing=True)
                accepted = await authorized.publish(other)
                assert accepted == await authorized.read_receipt(other)
                assert accepted == await authorized.publish(other)
        assert admitted[owed.publication_id][0] == owed.payload
        assert len(admitted) == 3 and owed.state == "pending"
        acknowledge(owed, read)
        for other in all_publications:
            if other.publication_id == owed.publication_id:
                continue
            stored_other = await independent.get(Publication, (other.tenant_id, other.publication_id))
            acknowledge(stored_other, admitted[other.publication_id][1])
        await independent.commit()

    async with results_scenario["sessions"]() as independent:
        stored = await independent.get(Publication, ("tenant-one", publication.publication_id))
        original = await independent.get(AssessmentResult, ("tenant-one", result.result_id))
        assert (stored.state, stored.receipt["receipt_id"], stored.revision, stored.attempts) == (
            "published", "receipt-tenant-one-product-one", 2, 0)
        assert stored.receipt == admitted[stored.publication_id][1]
        assert original.run_id == run.run_id and original.version == 1
        publications = (await independent.scalars(select(Publication))).all()
        results = (await independent.scalars(select(AssessmentResult))).all()
        findings = (await independent.scalars(select(Finding))).all()
        assert len(publications) == len(results) == 3 and len(findings) == 4
        assert {item.native_id for item in findings} == {"finding-one", "finding-two"}
        assert all(item.state == "published" and item.receipt == admitted[item.publication_id][1]
                   and item.attempts == 0 and item.revision == 2 for item in publications)
        assert len({item.result_id for item in findings if item.native_id == "finding-one"}) == 3


async def test_missing_receipt_cannot_publish(results_scenario):
    source = results_scenario["source"]
    await collect_result(results_scenario, "run-no-receipt", capability="code_scan", manifest_ref={
        "artifact_id": "manifest-two", "version": "1", "sha256": "b" * 64,
        "media_type": "application/json", "bytes": 4}, findings=[
        {"native_id": "finding", "kind": "static_finding", "claim": "risk",
         "provider_severity": "high", "source_refs": [source]}])
    async with results_scenario["sessions"]() as session:
        publication = (await session.scalars(select(Publication))).one()
        original = await session.get(AssessmentResult, (publication.tenant_id, publication.result_id))
        calls = []

        def transport(request):
            calls.append(request.headers["idempotency-key"])
            return httpx.Response(200, json={"status": "ok"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport),
                                     base_url="http://context.test") as client:
            with pytest.raises(ValueError, match="durable receipt"):
                await ContextClient(client, "fixture-delegation", private_read_fencing=True).publish(publication)
        assert calls == [f"tenant-one/{original.result_id}/1/product-context/1.0"]
        with pytest.raises(ValueError, match="foreign records"):
            acknowledge(publication, {"receipt_id": "receipt-foreign",
                                      "contribution_id": publication.publication_id,
                                      "admitted_schema": "1.0", "consumer_revision": "1",
                                      "record_refs": [{**source, "tenant_id": "tenant-other"}]})
        assert publication.state == "pending" and publication.receipt is None
        assert original.run_id == "run-no-receipt" and original.version == 1


async def test_missing_manifest_retains_result_without_sending_unverified_evidence(results_scenario):
    await collect_result(results_scenario, "run-unavailable", capability="code_scan", findings=[])
    async with results_scenario["sessions"]() as session:
        publication = (await session.scalars(select(Publication))).one()
        result = await session.get(AssessmentResult, (publication.tenant_id, publication.result_id))
        assert publication.state == "unavailable" and publication.receipt is None
        assert result.run_id == "run-unavailable"

        def never_deliver(request):
            raise AssertionError("unverified manifest must never reach Context")

        async with httpx.AsyncClient(transport=httpx.MockTransport(never_deliver),
                                     base_url="http://context.test") as client:
            with pytest.raises(ValueError, match="not eligible"):
                await ContextClient(client, "fixture-delegation", private_read_fencing=True).publish(publication)


async def test_publication_recovery(results_scenario):
    source = results_scenario["source"]
    manifest_ref = {"artifact_id": "manifest-repair", "version": "1", "sha256": "c" * 64,
                    "media_type": "application/json", "bytes": 4}
    await collect_result(results_scenario, "run-repair", capability="threat_model", manifest_ref=manifest_ref,
                         findings=[
        {"native_id": "finding-repair", "kind": "candidate_threat", "claim": "potential risk",
         "provider_severity": "medium", "source_refs": [source]}])
    controls = await seed_recovery_scope_controls(results_scenario, manifest_ref)
    assert len(controls) == 2
    async with results_scenario["sessions"]() as session:
        publication = await session.scalar(select(Publication).join(AssessmentResult).where(
            AssessmentResult.run_id == "run-repair"))
        original_result = await session.get(AssessmentResult, (publication.tenant_id, publication.result_id))
        stored_payload = original_result.payload.copy()
        original_work = (await session.scalars(select(Work).where(Work.run_id == "run-repair"))).one()
        assert publication.state == "pending"
        tenant_id, publication_id = publication.tenant_id, publication.publication_id

    consumer = FastAPI()
    admitted = {}
    attempts = []

    @consumer.get("/internal/v1/context/l5-contributions/{contribution_id}")
    async def readback(contribution_id: str, request: Request):
        if contribution_id not in admitted:
            raise HTTPException(404)
        scope = admitted[contribution_id][0]["scope"]
        if request.headers["x-l5-delegation-id"] != f"grant:{scope['tenant_id']}:{scope['product_id']}":
            raise HTTPException(404)
        return admitted[contribution_id][1]

    @consumer.post("/internal/v1/context/l5-contributions")
    async def accept(contribution: dict, request: Request):
        scope = contribution["scope"]
        if request.headers["x-l5-delegation-id"] != f"grant:{scope['tenant_id']}:{scope['product_id']}":
            raise HTTPException(404)
        attempts.append(request.headers["idempotency-key"])
        if len(attempts) == 1:
            raise httpx.ReadTimeout("response lost")
        if len(attempts) == 2:
            raise HTTPException(503)
        receipt = {"receipt_id": "receipt-repair", "contribution_id": contribution["contribution_id"],
                   "admitted_schema": "1.0", "consumer_revision": "7",
                   "record_refs": [contribution["result_ref"]]}
        admitted[contribution["contribution_id"]] = (contribution, receipt)
        return JSONResponse(receipt, status_code=201)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=consumer),
                                 base_url="http://context.test") as transport:
        client = ContextClient(transport, "grant:tenant-one:product-one", private_read_fencing=True)
        assert await repair_publication(results_scenario["sessions"], client, tenant_id, publication_id,
                                        now=NOW)
        async with results_scenario["sessions"]() as independent:
            owed = await independent.get(Publication, (tenant_id, publication_id))
            assert (owed.state, owed.attempts, owed.revision, owed.receipt, owed.last_error) == (
                "retry_wait", 1, 3, None, "transport_unavailable")
            assert owed.next_due_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(seconds=60)
        assert not await repair_publication(results_scenario["sessions"], client, tenant_id, publication_id,
                                            now=NOW + timedelta(seconds=59))
        restarted = async_sessionmaker(results_scenario["sessions"].kw["bind"], expire_on_commit=False)
        assert await repair_publication(restarted, client, tenant_id, publication_id,
                                        now=NOW + timedelta(seconds=60))
        async with restarted() as independent:
            owed = await independent.get(Publication, (tenant_id, publication_id))
            assert (owed.state, owed.attempts, owed.revision, owed.receipt, owed.last_error) == (
                "retry_wait", 2, 5, None, "consumer_unavailable")
            assert owed.next_due_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(seconds=180)
        assert await repair_publication(restarted, client, tenant_id, publication_id,
                                        now=NOW + timedelta(seconds=180))
        assert (await client.read_receipt(owed))["receipt_id"] == "receipt-repair"
        with pytest.raises(httpx.HTTPStatusError) as denied:
            await ContextClient(transport, "grant:tenant-two:product-two",
                                private_read_fencing=True).read_receipt(owed)
        assert denied.value.response.status_code == 404

    async with restarted() as independent:
        published = await independent.get(Publication, (tenant_id, publication_id))
        original = await independent.get(AssessmentResult, (tenant_id, published.result_id))
        work = (await independent.scalars(select(Work))).all()
        assert (published.state, published.attempts, published.revision) == ("published", 3, 7)
        assert published.receipt == admitted[publication_id][1]
        assert admitted[publication_id][0] == published.payload and len(admitted) == 1
        assert len(set(attempts)) == 1 and len(attempts) == 3
        assert original.payload == stored_payload and original.run_id == original_result.run_id
        assert len(work) == 3 and any(item.work_id == original_work.work_id for item in work)
        for control_id, before in controls.items():
            other = await independent.get(Publication, (before[0], control_id))
            result = await independent.get(AssessmentResult, (before[0], before[1]))
            other_work = (await independent.scalars(select(Work).where(
                Work.tenant_id == before[0], Work.run_id == before[2]))).one()
            assert (other.tenant_id, other.result_id, result.run_id, result.payload,
                    other.state, other.attempts, other.revision, other.receipt,
                    other_work.work_id, other_work.completed_at) == before
            assert control_id not in admitted


async def test_lost_acknowledgement_repaired_by_minute_sweep(results_scenario):
    await collect_result(results_scenario, "run-lost-ack", capability="code_scan", manifest_ref={
        "artifact_id": "manifest-lost-ack", "version": "1", "sha256": "d" * 64,
        "media_type": "application/json", "bytes": 4}, findings=[])
    async with results_scenario["sessions"]() as session:
        publication = (await session.scalars(select(Publication))).one()
        original = await session.get(AssessmentResult, (publication.tenant_id, publication.result_id))
        result_payload = original.payload.copy()
        publication_id = publication.publication_id

    consumer = FastAPI()
    admitted = {}
    calls = []

    @consumer.get("/internal/v1/context/l5-contributions/{contribution_id}")
    async def readback(contribution_id: str):
        if contribution_id not in admitted:
            raise HTTPException(404)
        return admitted[contribution_id][1]

    @consumer.post("/internal/v1/context/l5-contributions")
    async def accept(contribution: dict, request: Request):
        calls.append(request.headers["idempotency-key"])
        receipt = {"receipt_id": "receipt-lost-ack", "contribution_id": contribution["contribution_id"],
                   "admitted_schema": "1.0", "consumer_revision": "3", "record_refs": []}
        admitted[contribution["contribution_id"]] = (contribution, receipt)
        raise httpx.ReadTimeout("response lost after durable admission")

    async def forbidden_publish(envelope, delay):
        raise AssertionError("repair cannot enqueue a new provider assessment")

    async def forbidden_authorize(run, claim, action):
        raise AssertionError("repair cannot request provider authority")

    class ForbiddenAdapter:
        async def submit(self, *args):
            raise AssertionError("repair cannot submit a provider job")

    clock = {"now": NOW}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=consumer),
                                 base_url="http://context.test") as transport:
        client = ContextClient(transport, "fixture-delegation", private_read_fencing=True,
                               scope={"tenant_id": "tenant-one", "product_id": "product-one",
                                      "project_id": "project-one"})
        service = ExecutionService(results_scenario["sessions"], None, ForbiddenAdapter(),
                                   forbidden_publish, forbidden_authorize,
                                   clock=lambda: clock["now"], context_client=client)
        assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
        async with results_scenario["sessions"]() as independent:
            owed = await independent.get(Publication, ("tenant-one", publication_id))
            assert (owed.state, owed.attempts, owed.receipt) == ("retry_wait", 1, None)
        assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 0}
        restarted_sessions = async_sessionmaker(results_scenario["sessions"].kw["bind"], expire_on_commit=False)
        clock["now"] = NOW + timedelta(seconds=60)
        restarted = ExecutionService(restarted_sessions, None, ForbiddenAdapter(), forbidden_publish,
                                     forbidden_authorize, clock=lambda: clock["now"], context_client=client)
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 0}

    async with restarted_sessions() as independent:
        repaired = await independent.get(Publication, ("tenant-one", publication_id))
        result = await independent.get(AssessmentResult, ("tenant-one", repaired.result_id))
        work = (await independent.scalars(select(Work))).all()
        assert (repaired.state, repaired.attempts, repaired.revision) == ("published", 2, 5)
        assert repaired.receipt == admitted[publication_id][1]
        assert result.payload == result_payload and result.run_id == "run-lost-ack"
        assert len(work) == 1 and work[0].completed_at is not None
        assert len(calls) == 1 and admitted[publication_id][0] == repaired.payload


async def test_expired_lease_reconciles_receipt_without_another_post(results_scenario):
    await collect_result(results_scenario, "run-expired-lease", capability="code_scan", manifest_ref={
        "artifact_id": "manifest-expired", "version": "1", "sha256": "e" * 64,
        "media_type": "application/json", "bytes": 4}, findings=[])
    async with results_scenario["sessions"]() as session:
        publication = (await session.scalars(select(Publication))).one()
        publication_id = publication.publication_id
    assert await claim_publication(results_scenario["sessions"], "tenant-one", publication_id, now=NOW)
    assert not await claim_publication(results_scenario["sessions"], "tenant-one", publication_id,
                                       now=NOW + timedelta(seconds=59))
    receipt = {"receipt_id": "receipt-expired", "contribution_id": publication_id,
               "admitted_schema": "1.0", "consumer_revision": "4", "record_refs": []}

    def transport(request):
        assert request.method == "GET" and request.url.path.endswith(publication_id)
        return httpx.Response(200, json=receipt)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport),
                                 base_url="http://context.test") as client:
        assert await repair_publication(results_scenario["sessions"], ContextClient(
            client, "delegation", private_read_fencing=True),
                                        "tenant-one", publication_id, now=NOW + timedelta(seconds=61))
    async with results_scenario["sessions"]() as independent:
        repaired = await independent.get(Publication, ("tenant-one", publication_id))
        assert (repaired.state, repaired.attempts, repaired.revision, repaired.receipt) == (
            "published", 2, 4, receipt)


async def test_private_gate_and_scoped_revocation_intent(results_scenario):
    source = results_scenario["source"]
    foreign_source = {**source, "tenant_id": "tenant-two"}
    manifest = {"artifact_id": "manifest-scoped", "version": "1", "sha256": "f" * 64,
                "media_type": "application/json", "bytes": 4}
    await collect_result(results_scenario, "run-scoped", capability="code_scan",
                         manifest_ref=manifest, findings=[])
    await collect_result(results_scenario, "run-other-product", capability="code_scan",
                         product_id="product-other", manifest_ref=manifest, findings=[])
    await collect_result(results_scenario, "run-other-tenant", capability="code_scan",
                         tenant_id="tenant-two", product_id="product-two", source=foreign_source,
                         manifest_ref=manifest, findings=[])
    async with results_scenario["sessions"]() as session:
        publications = (await session.scalars(select(Publication))).all()
        scoped = next(row for row in publications if row.payload["run_id"] == "run-scoped")
        original_hash = scoped.contribution_hash
        scoped_id = scoped.publication_id
        assert len(publications) == 3 and all(row.state == "pending" for row in publications)

    def never_send(request):
        raise AssertionError("unqualified private source must not reach Context")

    async with httpx.AsyncClient(transport=httpx.MockTransport(never_send),
                                 base_url="http://context.test") as transport:
        unqualified = ContextClient(transport, "fixture-delegation")
        assert not await repair_publication(results_scenario["sessions"], unqualified,
                                            "tenant-one", scoped_id, now=NOW)
        with pytest.raises(ValueError, match="private Context read fencing"):
            await unqualified.publish(scoped)
        async with results_scenario["sessions"]() as session:
            with pytest.raises(ValueError, match="positive watermark"):
                await fence_source(session, tenant_id="tenant-one", product_id="product-one",
                                   project_id="project-one", source_ref=source, watermark=0, now=NOW)
            with pytest.raises(ValueError, match="scoped source"):
                await fence_source(session, tenant_id="tenant-two", product_id="product-two",
                                   project_id="project-one", source_ref=source, watermark=4, now=NOW)
            assert await fence_source(session, tenant_id="tenant-one", product_id="product-one",
                                      project_id="project-one", source_ref=source, watermark=4, now=NOW) == 1
            await session.commit()

        qualified = ContextClient(transport, "fixture-delegation", private_read_fencing=True)
        assert not await repair_publication(results_scenario["sessions"], qualified,
                                            "tenant-one", scoped_id, now=NOW)
        async with results_scenario["sessions"]() as session:
            assert await fence_source(session, tenant_id="tenant-one", product_id="product-one",
                                      project_id="project-one", source_ref=source, watermark=4, now=NOW) == 0
            assert await fence_source(session, tenant_id="tenant-one", product_id="product-one",
                                      project_id="project-one", source_ref=source, watermark=5, now=NOW) == 1
            await session.commit()

    async with results_scenario["sessions"]() as independent:
        revoked = await independent.get(Publication, ("tenant-one", scoped_id))
        others = (await independent.scalars(select(Publication).where(
            Publication.publication_id != scoped_id))).all()
        result = await independent.get(AssessmentResult, ("tenant-one", revoked.result_id))
        assert (revoked.state, revoked.invalidation_watermark, revoked.applied_watermark,
                revoked.receipt, revoked.revision) == ("invalidation_pending", 5, 0, None, 3)
        assert revoked.payload["invalidation_watermark"] == 0
        assert revoked.contribution_hash == original_hash and result.run_id == "run-scoped"
        assert len(others) == 2 and all(row.state == "pending" and row.invalidation_watermark == 0
                                        for row in others)


async def test_visibility_and_invalidation(results_scenario):
    source = results_scenario["source"]
    manifest_ref = {
        "artifact_id": "manifest-revoked", "version": "1", "sha256": "a" * 64,
        "media_type": "application/json", "bytes": 4}
    await collect_result(results_scenario, "run-revoked", capability="threat_model",
                         manifest_ref=manifest_ref, findings=[
        {"native_id": "threat-revoked", "kind": "candidate_threat", "claim": "candidate only",
         "provider_severity": "high", "source_refs": [source]}])
    controls = await seed_visibility_scope_controls(results_scenario, manifest_ref)
    assert set(controls) == {("tenant-one", "product-other"), ("tenant-two", "product-two")}
    async with results_scenario["sessions"]() as session:
        publication = await session.scalar(select(Publication).join(AssessmentResult).where(
            AssessmentResult.run_id == "run-revoked"))
        publication_id = publication.publication_id
        run = await session.get(Run, ("tenant-one", "run-revoked"))
        original_revision = run.revision

    consumer = FastAPI()
    stored = {}
    state = {"revoked_scopes": set(), "invalidation_attempts": 0}

    @consumer.get("/internal/v1/context/l5-contributions/{contribution_id}")
    async def readback(contribution_id: str, request: Request):
        if contribution_id not in stored:
            raise HTTPException(404)
        scope = stored[contribution_id][0]["scope"]
        if request.headers["x-l5-delegation-id"] != f"grant:{scope['tenant_id']}:{scope['product_id']}":
            raise HTTPException(404)
        return stored[contribution_id][1]

    @consumer.post("/internal/v1/context/l5-contributions")
    async def admit(contribution: dict, request: Request):
        scope = contribution["scope"]
        if request.headers["x-l5-delegation-id"] != f"grant:{scope['tenant_id']}:{scope['product_id']}":
            raise HTTPException(404)
        receipt = {"receipt_id": f"receipt-{scope['tenant_id']}-{scope['product_id']}",
                   "contribution_id": contribution["contribution_id"],
                   "admitted_schema": "1.0", "consumer_revision": "5",
                   "record_refs": [contribution["result_ref"]]}
        stored[contribution["contribution_id"]] = (contribution, receipt)
        return JSONResponse(receipt, status_code=201)

    @consumer.post("/internal/v1/context/l5-contributions/{contribution_id}/invalidate")
    async def invalidate(contribution_id: str, request: Request):
        if request.headers["x-l5-delegation-id"] != "grant:tenant-one:product-one":
            raise HTTPException(404)
        state["invalidation_attempts"] += 1
        body = await request.json()
        assert contribution_id == publication_id
        assert body == {"source_refs": [source], "invalidation_watermark": 9,
                        "reason": "source_access_revoked"}
        if state["invalidation_attempts"] == 1:
            raise HTTPException(503)
        receipt = {"receipt_id": "invalidation-receipt", "applied_watermark": 9,
                   "record_refs": stored[contribution_id][1]["record_refs"]}
        stored[contribution_id] = (*stored[contribution_id][:2], receipt)
        return receipt

    @consumer.post("/v1/products/{product_id}/projects/{project_id}/context/query")
    async def query(product_id: str, project_id: str, request: Request):
        body = await request.json()
        matches = [contribution["result_ref"] for contribution, *_ in stored.values() if
                   request.headers.get("x-test-tenant") == contribution["scope"]["tenant_id"]
                   and product_id == contribution["scope"]["product_id"]
                   and project_id == contribution["scope"]["project_id"]
                   and request.headers.get("x-test-actor") == contribution["scope"]["owner_id"]
                   and (contribution["scope"]["tenant_id"], product_id) not in state["revoked_scopes"]
                   and all(reference in body["source_refs"] for reference in contribution["source_refs"])]
        return {"snapshot_ref": {
                    "tenant_id": request.headers["x-test-tenant"], "project_id": project_id,
                    "source_connection_id": "context", "record_type": "context_snapshot",
                    "record_id": f"snapshot-{product_id}", "record_revision": "1"},
                "record_refs": matches, "evidence_refs": [], "gaps": [],
                "observed_at": NOW.isoformat().replace("+00:00", "Z")}

    async def forbidden_publish(envelope, delay):
        raise AssertionError("invalidation cannot schedule an assessment")

    async def forbidden_authorize(run, claim, action):
        raise AssertionError("invalidation cannot request provider authority")

    async def verify_source_revocation(event):
        if event.get("source_ref") != source or event.get("invalidation_watermark") != 9:
            return {"source_ref": source, "invalidation_watermark": 9, "revoked": False}
        state["revoked_scopes"].add(("tenant-one", "product-one"))
        return {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
                "source_ref": source, "invalidation_watermark": 9, "revoked": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=consumer),
                                 base_url="http://context.test") as transport:
        client = ContextClient(transport, "grant:tenant-one:product-one", private_read_fencing=True,
                               scope={"tenant_id": "tenant-one", "product_id": "product-one",
                                      "project_id": "project-one"})
        service = ExecutionService(results_scenario["sessions"], None, object(), forbidden_publish,
                                   forbidden_authorize, clock=lambda: NOW, context_client=client,
                                   verify_source_revocation=verify_source_revocation)
        assert await repair_publication(results_scenario["sessions"], client,
                                        "tenant-one", publication_id, now=NOW)
        for (control_tenant, control_product), (control_id, *_) in controls.items():
            scoped_client = ContextClient(transport, f"grant:{control_tenant}:{control_product}",
                                          private_read_fencing=True)
            assert await repair_publication(results_scenario["sessions"], scoped_client,
                                            control_tenant, control_id, now=NOW)
        assert len(stored) == 3
        path = "/v1/products/product-one/projects/project-one/context/query"
        other_path = path.replace("product-one", "product-other")
        foreign_path = path.replace("product-one", "product-two")
        body = {"topic": "Security", "source_refs": [source], "max_records": 10,
                "required_freshness_seconds": 60}

        async def answer(route, tenant_id, actor_id, *, source_refs=None):
            response = await transport.post(route, json={**body,
                "source_refs": source_refs if source_refs is not None else body["source_refs"]}, headers={
                "X-Test-Tenant": tenant_id, "X-Test-Actor": actor_id})
            assert response.status_code == 200
            schema = json.loads((Path(__file__).resolve().parents[4] /
                                 "docs/l5-services/contracts/v1/schemas.json").read_text())
            assert not list(Draft202012Validator({**schema, "$ref": "#/$defs/ContextAnswer"}).iter_errors(
                response.json()))
            assert response.json()["snapshot_ref"]["tenant_id"] == tenant_id
            assert all(item["tenant_id"] == tenant_id and item["project_id"] == "project-one"
                       for item in response.json()["record_refs"])
            return response.json()["record_refs"]

        other_id = controls[("tenant-one", "product-other")][0]
        foreign_id = controls[("tenant-two", "product-two")][0]
        foreign_source = {**source, "tenant_id": "tenant-two"}
        assert await answer(path, "tenant-one", "owner-one") == [stored[publication_id][0]["result_ref"]]
        assert await answer(other_path, "tenant-one", "owner-other") == [stored[other_id][0]["result_ref"]]
        assert await answer(foreign_path, "tenant-two", "owner-two",
                            source_refs=[foreign_source]) == [stored[foreign_id][0]["result_ref"]]
        assert not await answer(path, "tenant-one", "owner-other")
        assert not await answer(path, "tenant-two", "owner-two", source_refs=[foreign_source])
        assert not await answer(other_path, "tenant-one", "owner-one")
        with pytest.raises(ValueError, match="verified current authority"):
            await service.revoke_source({"source_ref": {**source, "tenant_id": "tenant-two"},
                                         "invalidation_watermark": 9})
        assert await service.revoke_source({"source_ref": source, "invalidation_watermark": 9}) == 1
        assert not await answer(path, "tenant-one", "owner-one")
        assert await answer(other_path, "tenant-one", "owner-other") == [stored[other_id][0]["result_ref"]]
        assert await answer(foreign_path, "tenant-two", "owner-two",
                            source_refs=[foreign_source]) == [stored[foreign_id][0]["result_ref"]]
        assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
        async with results_scenario["sessions"]() as independent:
            pending = await independent.get(Publication, ("tenant-one", publication_id))
            assert pending.state == "invalidation_pending" and pending.invalidation_receipt is None
            assert pending.receipt["receipt_id"] == "receipt-tenant-one-product-one"
        assert await service.revoke_source({"source_ref": source, "invalidation_watermark": 9}) == 0
        service.clock = lambda: NOW + timedelta(seconds=60)
        assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
        assert not await answer(path, "tenant-one", "owner-one")
        assert await answer(other_path, "tenant-one", "owner-other") == [stored[other_id][0]["result_ref"]]
        assert await answer(foreign_path, "tenant-two", "owner-two",
                            source_refs=[foreign_source]) == [stored[foreign_id][0]["result_ref"]]

    async with results_scenario["sessions"]() as independent:
        publication = await independent.get(Publication, ("tenant-one", publication_id))
        run = await independent.get(Run, ("tenant-one", "run-revoked"))
        result = await independent.get(AssessmentResult, ("tenant-one", publication.result_id))
        work = (await independent.scalars(select(Work))).all()
        assert (publication.state, publication.invalidation_watermark, publication.applied_watermark,
                publication.invalidation_receipt["receipt_id"], publication.invalidation_attempts,
                state["invalidation_attempts"]) == (
            "invalidated", 9, 9, "invalidation-receipt", 2, 2)
        assert result.run_id == run.run_id and run.revision == original_revision
        assert len(work) == 3 and all(item.completed_at is not None for item in work)
        for (control_tenant, _), (control_id, result_id, source_refs, digest) in controls.items():
            control = await independent.get(Publication, (control_tenant, control_id))
            stored_result = await independent.get(AssessmentResult, (control_tenant, result_id))
            assert (control.state, control.invalidation_watermark, control.applied_watermark,
                    control.contribution_hash, control.payload["source_refs"], stored_result.run_id) == (
                "published", 0, 0, digest, source_refs, control.payload["run_id"])
            assert control.receipt == stored[control_id][1]

    document = {**source, "record_type": "document"}
    async with results_scenario["sessions"]() as session:
        version = await session.get(ConfigurationVersion, ("tenant-one", "config-one", 1))
        version.settings = {"enabled": True, "source_refs": [document], "triggers": [{
            "trigger_id": "context-trigger", "kind": "document_revision", "enabled": True,
            "capability": "threat_model", "binding_id": "binding", "source_refs": [document]}]}
        await session.commit()
        causal_origin = {"source": "context", "service_id": "security", "run_id": "run-revoked"}
        ignored = await receive_document_event(
            session, tenant_id="tenant-one", product_id="product-one", project_id="project-one",
            trigger_id="context-trigger", event_id="causal-update", source_ref={
                **document, "record_revision": "next-revision"}, causal_origin=causal_origin, now=NOW)
        assert (ignored.state, ignored.reason, ignored.causal_origin) == (
            "rejected", "self_caused", causal_origin)
    async with results_scenario["sessions"]() as independent:
        assert len((await independent.scalars(select(Run))).all()) == 3
        assert len((await independent.scalars(select(Work))).all()) == 3


async def test_invalidation_requires_current_watermark_and_fences_late_reply(results_scenario):
    source = results_scenario["source"]
    await collect_result(results_scenario, "run-watermark", capability="code_scan", manifest_ref={
        "artifact_id": "manifest-watermark", "version": "1", "sha256": "d" * 64,
        "media_type": "application/json", "bytes": 4}, findings=[])
    async with results_scenario["sessions"]() as session:
        publication = (await session.scalars(select(Publication))).one()
        publication_id = publication.publication_id
        assert await fence_source(session, tenant_id="tenant-one", product_id="product-one",
                                  project_id="project-one", source_ref=source, watermark=1, now=NOW) == 1
        await session.commit()

    def incomplete_response(request):
        return httpx.Response(200, json={"receipt_id": "invalid", "applied_watermark": 0,
                                         "record_refs": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(incomplete_response),
                                 base_url="http://context.test") as transport:
        assert await repair_invalidation(results_scenario["sessions"], ContextClient(
            transport, "fixture-delegation"), "tenant-one", publication_id, now=NOW)
    async with results_scenario["sessions"]() as independent:
        pending = await independent.get(Publication, ("tenant-one", publication_id))
        assert pending.state == "invalidation_pending" and pending.invalidation_receipt is None
        assert pending.last_error == "receipt_invalid"

    class RacingClient:
        async def invalidate(self, publication):
            async with results_scenario["sessions"]() as session:
                assert await fence_source(session, tenant_id="tenant-one", product_id="product-one",
                                          project_id="project-one", source_ref=source, watermark=2,
                                          now=NOW + timedelta(seconds=60)) == 1
                await session.commit()
            return {"receipt_id": "stale", "applied_watermark": 1, "record_refs": []}

    assert not await repair_invalidation(results_scenario["sessions"], RacingClient(),
                                         "tenant-one", publication_id, now=NOW + timedelta(seconds=60))
    async with results_scenario["sessions"]() as independent:
        pending = await independent.get(Publication, ("tenant-one", publication_id))
        assert (pending.state, pending.invalidation_watermark, pending.applied_watermark,
                pending.invalidation_receipt) == ("invalidation_pending", 2, 0, None)

    def latest_response(request):
        assert json.loads(request.content)["invalidation_watermark"] == 2
        return httpx.Response(200, json={"receipt_id": "current", "applied_watermark": 2,
                                         "record_refs": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(latest_response),
                                 base_url="http://context.test") as transport:
        assert await repair_invalidation(results_scenario["sessions"], ContextClient(
            transport, "fixture-delegation"), "tenant-one", publication_id,
            now=NOW + timedelta(seconds=60))
    async with results_scenario["sessions"]() as independent:
        completed = await independent.get(Publication, ("tenant-one", publication_id))
        assert (completed.state, completed.invalidation_watermark, completed.applied_watermark,
                completed.invalidation_receipt["receipt_id"], completed.invalidation_attempts) == (
            "invalidated", 2, 2, "current", 3)
