"""Regressions for scoped scheduled repair and complete Context admission."""

import asyncio
import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from l5_services.models import AssessmentResult, Finding, Publication, Work
from l5_services.publication.client import ContextClient
from l5_services.publication.contributions import acknowledge
from l5_services.publication.invalidation import receive_verified_revocation
from l5_services.publication.recovery import repair_due_invalidations, repair_due_publications
from l5_services.workers.execution import ExecutionService
from l5_services.workers.recovery import lambda_handler as repair_handler
from test_context_contributions import seed_other_identity_scopes
from test_results import NOW, collect_result, results_scenario


SCOPE = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"}
MANIFEST = {"artifact_id": "manifest-one", "version": "1", "sha256": "a" * 64,
            "media_type": "application/json", "bytes": 4}
SCHEMAS = json.loads((Path(__file__).resolve().parents[4] /
                     "docs/l5-services/contracts/v1/schemas.json").read_text())


async def forbid_provider(*args):
    raise AssertionError("Context repair must not submit or authorize Security work")


def service_for(sessions, transport, scope, *, now=NOW):
    client = ContextClient(transport, f"grant:{scope['tenant_id']}:{scope['product_id']}",
                           scope=scope, private_read_fencing=True)
    return ExecutionService(sessions, None, object(), forbid_provider, forbid_provider,
                            clock=lambda: now, context_client=client)


async def collect(scenario, count=1, *, evidence_count=0):
    evidence = [{"artifact_id": f"evidence-{index}", "version": "1", "sha256": "b" * 64,
                 "media_type": "text/plain", "bytes": 1} for index in range(evidence_count)]
    await collect_result(scenario, "run-repair-batch", capability="threat_model", manifest_ref=MANIFEST,
                         findings=[{"native_id": f"claim-{index}", "kind": "candidate_threat",
                                    "claim": f"candidate {index}", "provider_severity": "medium",
                                    "validation": "candidate", "source_refs": [scenario["source"]],
                                    "evidence": evidence} for index in range(count)])


async def publications(sessions):
    async with sessions() as session:
        return (await session.scalars(select(Publication))).all()


class Consumer:
    def __init__(self):
        self.payloads = {}
        self.receipts = {}
        self.calls = []
        self.lose_ack = False
        self.reject_reads = False
        self.reject_invalidation = False
        self.invalidations = {}

    def __call__(self, request):
        self.calls.append(request)
        if request.method == "GET":
            if self.reject_reads:
                return httpx.Response(403)
            payload = self.payloads.get(request.url.path.split("/")[-1])
        elif request.url.path.endswith("/invalidate"):
            payload = self.payloads[request.url.path.split("/")[-2]]
        else:
            payload = json.loads(request.content)
            Draft202012Validator({**SCHEMAS, "$ref": "#/$defs/Contribution"}).validate(payload)
        if payload is None:
            return httpx.Response(404)
        scope = payload["scope"]
        if request.headers["x-l5-delegation-id"] != f"grant:{scope['tenant_id']}:{scope['product_id']}":
            return httpx.Response(404)
        identity = payload["contribution_id"]
        if request.url.path.endswith("/invalidate"):
            if self.reject_invalidation:
                return httpx.Response(503)
            watermark = json.loads(request.content)["invalidation_watermark"]
            self.invalidations[identity] = watermark
            return httpx.Response(200, json={"receipt_id": f"invalidation-{identity}",
                                             "applied_watermark": watermark,
                                             "record_refs": [payload["result_ref"]]})
        if request.method == "POST":
            self.payloads[identity] = payload
            self.receipts[identity] = {"receipt_id": f"receipt-{identity}", "contribution_id": identity,
                                       "admitted_schema": "1.0", "consumer_revision": "1",
                                       "record_refs": [payload["result_ref"]]}
            if self.lose_ack:
                self.lose_ack = False
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
        return httpx.Response(200, json=self.receipts[identity])


async def test_scheduled_workers_leave_other_scopes_owed(results_scenario):
    await collect(results_scenario)
    await seed_other_identity_scopes(results_scenario, MANIFEST)
    sessions = results_scenario["sessions"]
    original = await publications(sessions)
    consumer = Consumer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(consumer),
                                 base_url="http://context.test") as transport:
        wrong_project = service_for(sessions, transport, {**SCOPE, "project_id": "project-other"})
        assert await asyncio.to_thread(repair_handler, {}, None, service=wrong_project) == {"enqueued": 0}
        unscoped = ContextClient(transport, "grant:tenant-one:product-one", private_read_fencing=True)
        for sweep in (repair_due_publications, repair_due_invalidations):
            with pytest.raises(ValueError, match="verified delegation scope"):
                await sweep(sessions, unscoped, now=NOW)
        for selected in original:
            service = service_for(sessions, transport, selected.payload["scope"])
            assert await repair_due_publications(sessions, service.context_client, now=NOW, limit=1) == 1
            assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 0}
            for row in await publications(sessions):
                if row.publication_id not in consumer.receipts:
                    assert (row.state, row.attempts, row.revision) == ("pending", 0, 1)
        assert len(consumer.receipts) == 3
        async with sessions() as session:
            for row in await session.scalars(select(Publication)):
                row.state = "invalidation_pending"
                row.invalidation_watermark = 1
                row.invalidation = {"source_refs": row.payload["source_refs"], "reason": "source_access_revoked"}
            await session.commit()
        for selected in original:
            service = service_for(sessions, transport, selected.payload["scope"])
            assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
            for row in await publications(sessions):
                if row.publication_id not in consumer.invalidations:
                    assert (row.state, row.invalidation_attempts) == ("invalidation_pending", 0)
        assert len(consumer.invalidations) == 3


async def test_unavailable_after_lost_ack_still_owes_invalidation(results_scenario):
    await collect(results_scenario)
    sessions = results_scenario["sessions"]
    consumer = Consumer()
    consumer.lose_ack = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(consumer),
                                 base_url="http://context.test") as transport:
        first = service_for(sessions, transport, SCOPE)
        assert await asyncio.to_thread(repair_handler, {}, None, service=first) == {"enqueued": 1}
        stored, = await publications(sessions)
        assert stored.state == "retry_wait" and stored.publication_id in consumer.payloads
        consumer.reject_reads = True
        restarted = service_for(async_sessionmaker(sessions.kw["bind"], expire_on_commit=False),
                                transport, SCOPE, now=NOW + timedelta(minutes=1))
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
        stored, = await publications(sessions)
        assert stored.state == "unavailable" and stored.receipt is None

        async def verify(event):
            return {**SCOPE, **event, "revoked": True}

        restarted.verify_source_revocation = verify
        assert await receive_verified_revocation(restarted, {
            "source_ref": results_scenario["source"], "invalidation_watermark": 1}, now=restarted.clock()) == 1
        consumer.reject_invalidation = True
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
        pending, = await publications(sessions)
        assert pending.state == "invalidation_pending" and pending.invalidation_attempts == 1
        consumer.reject_invalidation = False
        restarted.clock = lambda: NOW + timedelta(minutes=2)
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
    final, = await publications(sessions)
    assert (final.state, final.applied_watermark, final.invalidation_attempts) == ("invalidated", 1, 2)
    assert consumer.invalidations == {final.publication_id: 1}
    async with sessions() as session:
        result = await session.get(AssessmentResult, (final.tenant_id, final.result_id))
        assert result is not None and len(result.payload["findings"]) == 1


@pytest.mark.parametrize(("count", "evidence_count"), [(101, 0), (205, 2)])
async def test_large_result_pages_require_complete_receipt(results_scenario, count, evidence_count):
    await collect(results_scenario, count, evidence_count=evidence_count)
    sessions = results_scenario["sessions"]
    original, = await publications(sessions)
    pages = {}
    admitted = {}
    keys = {}
    faults = {"reject_second": True, "lose_final_ack": True}
    revoked = {"watermark": 0}
    receipt = {"receipt_id": "complete-receipt", "contribution_id": original.publication_id,
               "admitted_schema": "1.0", "consumer_revision": "1", "record_refs": [original.payload["result_ref"]],
               "contribution_hash": original.contribution_hash, "claim_count": count}

    def consumer(request):
        assert request.headers["x-l5-delegation-id"] == "grant:tenant-one:product-one"
        if request.url.path == "/readback":
            return httpx.Response(200, json=admitted if not revoked["watermark"] else {})
        if request.url.path.endswith("/invalidate"):
            assert request.url.path.split("/")[-2] == original.publication_id
            revoked["watermark"] = json.loads(request.content)["invalidation_watermark"]
            return httpx.Response(200, json={"receipt_id": "all-pages-invalidated",
                                             "applied_watermark": revoked["watermark"],
                                             "record_refs": receipt["record_refs"]})
        if revoked["watermark"]:
            return httpx.Response(410)
        if request.method == "GET":
            return httpx.Response(200, json=receipt) if admitted else httpx.Response(404)
        payload = json.loads(request.content)
        assert Draft202012Validator({**SCHEMAS, "$ref": "#/$defs/Contribution"}).is_valid(payload)
        page = payload["claim_page"]
        assert page["contribution_hash"] == original.contribution_hash
        assert page["total_claims"] == count
        assert payload["contribution_id"] == original.publication_id
        index = page["index"]
        key = request.headers["idempotency-key"]
        assert keys.setdefault(index, key) == key
        if index == 1 and faults["reject_second"]:
            faults["reject_second"] = False
            return httpx.Response(503)
        assert pages.setdefault(index, payload) == payload
        if len(pages) != page["count"]:
            return httpx.Response(202)
        complete = {**pages[0], "claims": [claim for number in sorted(pages) for claim in pages[number]["claims"]],
                    "evidence": [ref for number in sorted(pages) for ref in pages[number]["evidence"]]}
        complete.pop("claim_page")
        digest = sha256(json.dumps(complete, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert digest == original.contribution_hash
        admitted.update(complete)
        if faults["lose_final_ack"]:
            faults["lose_final_ack"] = False
            raise httpx.ReadTimeout("complete admission but lost acknowledgement", request=request)
        return httpx.Response(201, json=receipt)

    async with httpx.AsyncClient(transport=httpx.MockTransport(consumer),
                                 base_url="http://context.test") as transport:
        service = service_for(sessions, transport, SCOPE)
        assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
        partial, = await publications(sessions)
        assert partial.state == "retry_wait" and partial.receipt is None and not admitted
        restarted = service_for(async_sessionmaker(sessions.kw["bind"], expire_on_commit=False),
                                transport, SCOPE, now=NOW + timedelta(minutes=1))
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
        lost_ack, = await publications(sessions)
        assert lost_ack.state == "retry_wait" and lost_ack.receipt is None
        restarted.clock = lambda: NOW + timedelta(minutes=3)
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 0}
        completed, = await publications(sessions)
        assert completed.state == "published" and completed.receipt == receipt and completed.attempts == 3
        readback = await transport.get("/readback", headers={"X-L5-Delegation-ID": "grant:tenant-one:product-one"})
        assert readback.json() == original.payload

        async def verify(event):
            return {**SCOPE, **event, "revoked": True}

        restarted.verify_source_revocation = verify
        assert await receive_verified_revocation(restarted, {
            "source_ref": results_scenario["source"], "invalidation_watermark": 1}, now=restarted.clock()) == 1
        assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
        with pytest.raises(httpx.HTTPStatusError) as replay:
            await restarted.context_client.publish(original)
        assert replay.value.response.status_code == 410
        after_revocation = await transport.get("/readback", headers={
            "X-L5-Delegation-ID": "grant:tenant-one:product-one"})
        assert after_revocation.json() == {}
    invalidated, = await publications(sessions)
    assert invalidated.state == "invalidated" and invalidated.applied_watermark == 1
    assert len(keys) == len(set(keys.values())) == len(pages)
    async with sessions() as session:
        findings = (await session.scalars(select(Finding))).all()
        work = (await session.scalars(select(Work))).one()
        assert work.completed_at is not None
    assert len(admitted["claims"]) == len(findings) == count
    assert {claim["finding_id"] for claim in admitted["claims"]} == {finding.finding_id for finding in findings}
    assert admitted == original.payload


@pytest.mark.parametrize("receipt_fields", [{}, {"contribution_hash": "a" * 64, "claim_count": 101},
                                          {"claim_count": 100}, {"claim_count": "101"}])
async def test_partial_page_receipt_cannot_acknowledge(results_scenario, receipt_fields):
    await collect(results_scenario, 101)
    publication, = await publications(results_scenario["sessions"])
    receipt = {"receipt_id": "partial", "contribution_id": publication.publication_id,
               "admitted_schema": "1.0", "consumer_revision": "1", "record_refs": [publication.payload["result_ref"]]}
    if receipt_fields:
        receipt["contribution_hash"] = publication.contribution_hash
    with pytest.raises(ValueError, match="every claim page"):
        acknowledge(publication, receipt | receipt_fields)
    assert publication.state == "pending" and publication.receipt is None


async def test_accepted_pages_without_final_receipt_remain_owed(results_scenario):
    await collect(results_scenario, 101)
    sessions = results_scenario["sessions"]
    received = []

    def consumer(request):
        if request.method == "GET":
            return httpx.Response(404)
        received.append(json.loads(request.content))
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(consumer),
                                 base_url="http://context.test") as transport:
        service = service_for(sessions, transport, SCOPE)
        assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
    owed, = await publications(sessions)
    assert len(received) == 2 and sum(len(page["claims"]) for page in received) == 101
    assert (owed.state, owed.receipt, owed.last_error) == ("retry_wait", None, "receipt_invalid")
