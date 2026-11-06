"""Well-Architected native result attribution regressions."""

from datetime import UTC, datetime
from datetime import timedelta
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from l5_services.adapters.well_architected.generation import GenerationAdapter
from l5_services.adapters.well_architected.results import (
    compare_wa_recommendations, normalize_wa_result, persist_wa_comparison, repair_wa_comparisons,
    wa_recommendation_findings,
)
from l5_services.api.results import create_results_router
from l5_services.findings import ResultStore
from l5_services.models.evidence import AssessmentResult
from l5_services.models.finding import Finding
from l5_services.models.publication import Publication
from l5_services.models.result import ResultDetail
from l5_services.models.source_retention import SourceRetention
from l5_services.models.triage import Triage
from l5_services.models.wa_usage import WellArchitectedUsage
from l5_services.publication.client import WellArchitectedConsumerClient
from l5_services.publication.contributions import create_wa_publications
from l5_services.publication.invalidation import fence_source
from l5_services.publication.recovery import activate_wa_publications
from l5_services.source_retention import pin_source_hold, release_source_hold, source_cleanup_allowed
from l5_services.workers.relay import repair_once
from l5_services.source_manifest import package_architecture_sources
from test_wa04 import NOW, SCOPE, _database
from test_wa04_repairs import CollectionProvider, seed_collection, worker_for


async def current_source_allowed(run):
    return True


class MixedGenerationProvider(CollectionProvider):
    def __init__(self, clock):
        super().__init__(clock, count=1)
        original = self.items[0]
        self.items = [original, {**original, "generationId": "other-generation",
                                 "recommendationArn": original["recommendationArn"] + "-other"},
                      {**original, "generationId": None,
                       "recommendationArn": original["recommendationArn"] + "-unattributed"}]

    def list_agent_recommendations(self, **request):
        self.lists.append(request)
        if request.get("nextToken") is None:
            return {"items": self.items[1:], "nextToken": "second-page"}
        assert request["nextToken"] == "second-page"
        return {"items": self.items[:1]}

    def get_agent_recommendation(self, *, recommendationArn):
        self.details.append(recommendationArn)
        item = next(item for item in self.items if item["recommendationArn"] == recommendationArn)
        return {**item, "sources": [{"source": "original native source"}],
                "tradeOffs": ["higher operating cost"], "createdAt": NOW,
                "ResponseMetadata": {"RequestId": "private-transport-metadata"}}


async def test_ac_01(tmp_path):
    engine, sessions = await _database(tmp_path)
    clock, published = [NOW], []
    provider = MixedGenerationProvider(clock)
    provider.items[0]["title"] = "Encrypt stored data"
    for suffix, title in (("contradicted", "Firewall is enabled"),
                          ("unsupported", "Global availability is guaranteed")):
        provider.items.append({**provider.items[0], "title": title,
                               "recommendationArn": provider.items[0]["recommendationArn"] + f"-{suffix}"})
    pinned_run, _, archived = _pinned_review()
    worker, _ = worker_for(sessions, clock, provider, published)
    try:
        await seed_collection(sessions, provider.native)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = deepcopy(pinned_run.manifest)
            run.input = {**run.input, "sources": [entry["source_ref"] for entry in
                                                 pinned_run.manifest["architecture"]["sources"]]}
            run.admission = {**run.admission, "generation_scope": pinned_run.admission["generation_scope"]}
            await session.commit()
        for _ in range(12):
            envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1} if not published else published.pop(0)
            await worker.execute(envelope)
            if not published:
                break
        assert [request.get("nextToken") for request in provider.lists] == [None, "second-page"]
        assert all(request["profileArn"] == provider.native["profile_arn"] and
                   request["maxResults"] == 1000 for request in provider.lists)
        assert provider.details == [provider.items[index]["recommendationArn"] for index in (3, 4, 0)]
        assert not provider.starts
        async with sessions() as session:
            result = (await session.scalars(select(AssessmentResult))).one()
            detail = (await session.scalars(select(ResultDetail))).one()
            assert result.payload["page_count"] == 2
            assert result.payload["excluded"] == [
                {"recommendationArn": provider.items[1]["recommendationArn"],
                 "generationId": "other-generation", "reason": "different_generation"},
                {"recommendationArn": provider.items[2]["recommendationArn"],
                 "generationId": None, "reason": "unattributed"}]
            assert {item["recommendationArn"] for item in result.payload["recommendations"]} == set(provider.details)
            assert all(item["sources"] == [{"source": "original native source"}]
                       and item["tradeOffs"] == ["higher operating cost"]
                       and item["createdAt"] == NOW.isoformat()
                       and "ResponseMetadata" not in item for item in result.payload["recommendations"])
            assert all("sources" not in item for item in detail.payload["recommendations"])
            assert detail.payload["review_mode"] == "source_only"
            assert all(item["comparison"]["status"] == "not_checked"
                       for item in detail.payload["recommendations"])
            assert "deployed state was not inspected" in detail.payload["limitations"][0]
            original_id = result.result_id

        class ArchiveStore:
            def __init__(self):
                self.reads = 0

            async def get_archive(self, run_id, reference):
                assert run_id == "run-example" and reference == pinned_run.manifest["archive"]
                self.reads += 1
                return archived

        archive_store = ArchiveStore()

        def fabricated_verifier(recommendation, files, goals):
            return {"status": "supported", "verifier_ref": "wrong-case-fixture",
                    "citations": [{"archive_path": "sources/0/deploy/main.tf", "line": 1,
                                   "quote": "encrypt = false"}]}

        with pytest.raises(ValueError, match="citation does not match pinned source line"):
            await persist_wa_comparison(sessions, SCOPE["tenant_id"], "run-example",
                                        archive_store, fabricated_verifier, now=NOW)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            altered = deepcopy(run.manifest)
            altered["architecture"]["files"][0]["sha256"] = "f" * 64
            run.manifest = altered
            await session.commit()
        with pytest.raises(ValueError, match="source archive manifest changed"):
            await persist_wa_comparison(sessions, SCOPE["tenant_id"], "run-example",
                                        archive_store, _independent_verifier, now=NOW)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = deepcopy(pinned_run.manifest)
            assert [(item.result_id, item.version) for item in
                    (await session.scalars(select(AssessmentResult))).all()] == [(original_id, 1)]
            await session.commit()

        compared_id = await persist_wa_comparison(sessions, SCOPE["tenant_id"], "run-example",
                                                 archive_store, _independent_verifier, now=NOW)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            compared = await ResultStore(session, [run], service_id="well_architected",
                                         wa_source_authority=current_source_allowed).get(compared_id)
            statuses = {item["title"]: item["comparison"] for item in compared["recommendations"]}
            assert {title: verdict["status"] for title, verdict in statuses.items()} == {
                "Encrypt stored data": "supported", "Firewall is enabled": "contradicted",
                "Global availability is guaranteed": "not_verifiable"}
            assert statuses["Encrypt stored data"]["citations"][0]["quote"] == "encrypt = true"
            assert statuses["Firewall is enabled"]["citations"][0]["quote"] == "firewall = false"
            assert statuses["Global availability is guaranteed"]["citations"] == []
            assert compared["based_on_result_id"] == original_id and compared["revision"] == 2
            assert compared["review_mode"] == "source_only"
            assert "deployed state was not inspected" in compared["limitations"][0]
            assert all("sources" not in item for item in compared["recommendations"])
            assert len((await session.scalars(select(AssessmentResult))).all()) == 2
        assert archive_store.reads == 3 and not provider.starts
    finally:
        await engine.dispose()


async def test_wrong_generation_detail_fails_closed():
    provider = MixedGenerationProvider([datetime(2026, 10, 28, tzinfo=UTC)])

    class Session:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return provider

    original = provider.items[0]
    provider.get_agent_recommendation = lambda **request: {
        **original, "generationId": "other-generation"}
    with pytest.raises(ValueError, match="another generation"):
        await GenerationAdapter(None).collect_detail(
            None, provider.native, None, Session(), recommendation=original)


def _pinned_review():
    source_content = b"encrypt = true\nfirewall = false\n"
    goals_content = b"Protect stored data\n"
    sources = []
    for index, (path, content) in enumerate((("deploy/main.tf", source_content),
                                             ("goals.txt", goals_content))):
        revision = sha256(content).hexdigest()
        sources.append({"source_ref": {**SCOPE, "record_type": "document",
                                       "source_connection_id": "connection-example",
                                       "record_id": f"source-example-{index}",
                                       "record_revision": revision},
                        "snapshot_id": f"snapshot-{index}", "revision": revision,
                        "selected_paths": [path], "selected_hashes": {path: revision},
                        "files": [{"path": path, "content": content, "component": "review",
                                   "exclusion": None, "entry_point": index == 0}]})
    archived, manifest = package_architecture_sources(sources, scope=SCOPE, max_files=2, max_bytes=4096)
    native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "profile_arn": "arn:aws:wellarchitected:us-east-1:000000000000:agent-profile/example",
              "generation_id": "original-generation", "name": "adp-wa-example"}
    run = SimpleNamespace(tenant_id=SCOPE["tenant_id"], run_id="review-example",
                          configuration_revision=3, binding_generation=2,
                          native_job_ref=native,
                          admission={"scope": SCOPE, "generation_model_hash": "a" * 64,
                                     "generation_scope": {"pillars": ["SECURITY"],
                                                          "goalIds": ["pinned-goal-example"]}},
                          manifest={"provider_coverage": "verified", "architecture": manifest,
                                    "archive": {"sha256": sha256(archived).hexdigest(),
                                                "bytes": len(archived), "artifact_id": "pinned-review"}})
    recommendations = [
        {"recommendationArn": "native-supported", "profileArn": native["profile_arn"],
         "generationId": native["generation_id"], "title": "Encrypt stored data",
         "sources": [{"unverified": "native provider source is not a citation"}]},
        {"recommendationArn": "native-contradicted", "profileArn": native["profile_arn"],
         "generationId": native["generation_id"], "title": "Firewall is enabled"},
        {"recommendationArn": "native-unknown", "profileArn": native["profile_arn"],
         "generationId": native["generation_id"], "title": "Global availability is guaranteed"},
    ]
    return run, {"native_job_ref": native, "recommendations": recommendations}, archived


def _independent_verifier(recommendation, files, goals):
    assert goals == ["pinned-goal-example"]
    lines = files["sources/0/deploy/main.tf"]["lines"]
    title = recommendation["title"]
    if title == "Encrypt stored data" and lines[0] == "encrypt = true":
        return {"status": "supported", "explanation": "Pinned IaC enables encryption",
                "verifier_ref": "fixture-independent-oracle",
                "citations": [{"archive_path": "sources/0/deploy/main.tf", "line": 1,
                               "quote": lines[0]}]}
    if title == "Firewall is enabled" and lines[1] == "firewall = false":
        return {"status": "contradicted", "explanation": "Pinned IaC disables firewall",
                "verifier_ref": "fixture-independent-oracle",
                "citations": [{"archive_path": "sources/0/deploy/main.tf", "line": 2,
                               "quote": lines[1]}]}
    return {"status": "not_verifiable", "explanation": "No pinned source supports the claim",
            "citations": []}


def test_independent_source_comparison_uses_pinned_bytes_not_native_sources():
    run, collected, archived = _pinned_review()
    detail = normalize_wa_result(run, "result-example", 1, collected, NOW)
    assert detail.payload["recommendations"][0]["comparison"]["status"] == "not_checked"
    assert detail.payload["goals"] == ["pinned-goal-example"]
    assert detail.payload["recommendations"][0]["native_id"] == "native-supported"
    assert "sources" not in detail.payload["recommendations"][0]
    comparisons = compare_wa_recommendations(run, collected, archived, _independent_verifier)
    assert [item["status"] for item in comparisons] == [
        "supported", "contradicted", "not_verifiable"]
    assert comparisons[0]["citations"][0]["sha256"] == sha256(b"encrypt = true\nfirewall = false\n").hexdigest()
    assert comparisons[1]["citations"][0]["quote"] == "firewall = false"
    assert comparisons[2]["citations"] == []


def test_independent_comparison_rejects_fabricated_or_changed_source():
    run, collected, archived = _pinned_review()
    def fabricated(recommendation, files, goals):
        return {"status": "supported", "explanation": "invented", "citations": [
            {"archive_path": "sources/0/deploy/main.tf", "line": 1, "quote": "encrypt = false"}]}

    with pytest.raises(ValueError, match="citation does not match"):
        compare_wa_recommendations(run, collected, archived, fabricated)
    run.manifest["architecture"]["files"][0]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="source archive manifest changed"):
        compare_wa_recommendations(run, collected, archived, _independent_verifier)


def test_independent_comparison_requires_citation_for_claimed_support():
    run, collected, archived = _pinned_review()
    def unsupported(recommendation, files, goals):
        return {"status": "supported", "citations": []}

    comparisons = compare_wa_recommendations(run, collected, archived, unsupported)
    assert all(item["status"] == "not_verifiable" and not item["citations"]
               for item in comparisons)


async def test_compared_revision_survives_owned_archive_cleanup_and_replay(tmp_path):
    engine, sessions = await _database(tmp_path)
    clock, published = [NOW], []
    provider = MixedGenerationProvider(clock)
    provider.items[0]["title"] = "Encrypt stored data"
    worker, _ = worker_for(sessions, clock, provider, published)
    pinned, _, archived = _pinned_review()

    class ArchiveStore:
        def __init__(self):
            self.reads = 0
            self.corrupt = False

        async def get_archive(self, run_id, reference):
            assert run_id == "run-example" and reference == pinned.manifest["archive"]
            self.reads += 1
            return b"changed" if self.corrupt else archived

    archive_store = ArchiveStore()
    try:
        await seed_collection(sessions, provider.native)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = {**run.manifest, **pinned.manifest}
            run.input = {**run.input, "sources": [source["source_ref"] for source in
                                                   pinned.manifest["architecture"]["sources"]]}
            run.admission = {**run.admission, "generation_scope": pinned.admission["generation_scope"]}
            await session.commit()
        for _ in range(6):
            envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1} if not published else published.pop(0)
            await worker.execute(envelope)
            if not published:
                break
        async with sessions() as session:
            original = (await session.scalars(select(AssessmentResult))).one()
            original_id = original.result_id
            assert original.payload["recommendations"][0]["title"] == "Encrypt stored data"
        archive_store.corrupt = True
        with pytest.raises(ValueError, match="pinned source archive"):
            await persist_wa_comparison(
                sessions, SCOPE["tenant_id"], "run-example", archive_store, _independent_verifier, now=NOW)
        archive_store.corrupt = False
        async with sessions() as session:
            assert [(item.result_id, item.version) for item in
                    (await session.scalars(select(AssessmentResult))).all()] == [(original_id, 1)]
        service = SimpleNamespace(sessions=sessions, archive_store=archive_store,
                                  well_architected_verifier=_independent_verifier)
        assert await repair_wa_comparisons(service, now=NOW) == 1
        assert await repair_wa_comparisons(service, now=NOW) == 0
        compared_id = await persist_wa_comparison(
            sessions, SCOPE["tenant_id"], "run-example", archive_store, _independent_verifier, now=NOW)
        assert compared_id != original_id and archive_store.reads == 2
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            original = await session.get(ResultDetail, (SCOPE["tenant_id"], original_id))
            assert original.payload["recommendations"][0]["comparison"]["status"] == "not_checked"
            assert (await ResultStore(session, [run], service_id="well_architected",
                                      wa_source_authority=current_source_allowed).get(compared_id))["recommendations"][0][
                "comparison"]["status"] == "supported"
            debts = (await session.scalars(select(Publication))).all()
            assert len(debts) == 4
            assert all(item.state == "unavailable" and item.last_error == "consumer_schema_unadmitted"
                       for item in debts)
            assert {(item.result_version, item.destination) for item in debts} == {
                (1, "product-context"), (1, "engineering-insights"),
                (2, "product-context"), (2, "engineering-insights")}
        archived = None
        assert await persist_wa_comparison(
            sessions, SCOPE["tenant_id"], "run-example", None, None, now=NOW) == compared_id
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            readback = await ResultStore(session, [run], service_id="well_architected",
                                         wa_source_authority=current_source_allowed).get(compared_id)
            assert readback["based_on_result_id"] == original_id
            assert readback["revision"] == 2 and readback["review_mode"] == "source_only"
            assert readback["recommendations"][0]["comparison"]["citations"][0]["path"] == "deploy/main.tf"
            assert len((await session.scalars(select(AssessmentResult))).all()) == 2
        assert archive_store.reads == 2 and not provider.starts
    finally:
        await engine.dispose()


async def test_ac_02(tmp_path, monkeypatch):
    engine, sessions = await _database(tmp_path)
    clock, published = [NOW], []
    provider = MixedGenerationProvider(clock)
    provider.items[0]["title"] = "Encrypt stored data"
    pinned, _, archived = _pinned_review()
    pinned.manifest["archive"] = {**pinned.manifest["archive"], "version": "archive-version-example"}
    cleanup_at = NOW + timedelta(minutes=2)
    worker, _ = worker_for(sessions, clock, provider, published)
    owned_archive = {"bytes": archived, "reads": 0}

    async def read_archived(run_id, reference):
        assert run_id == "run-example" and reference == pinned.manifest["archive"]
        owned_archive["reads"] += 1
        if owned_archive["bytes"] is None:
            raise FileNotFoundError("owned archive removed")
        return owned_archive["bytes"]

    worker.archive_store = SimpleNamespace(get_archive=read_archived)
    worker.well_architected_verifier = _independent_verifier
    try:
        await seed_collection(sessions, provider.native)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = {**run.manifest, **pinned.manifest}
            run.input = {**run.input, "sources": [source["source_ref"] for source in
                                                   pinned.manifest["architecture"]["sources"]]}
            run.admission = {**run.admission, "generation_scope": pinned.admission["generation_scope"]}
            await pin_source_hold(session, run, pinned.manifest["archive"],
                                  required_until=NOW + timedelta(minutes=1),
                                  policy_expires_at=NOW + timedelta(days=1), now=NOW)
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"],
                                                    pinned.manifest["archive"], now=NOW)
            await session.commit()
        for _ in range(6):
            envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1} if not published else published.pop(0)
            await worker.execute(envelope)
            if not published:
                break
        async with sessions() as session:
            assert {item.version for item in (await session.scalars(select(AssessmentResult))).all()} == {1, 2}
        assert len(provider.lists) == 2
        assert provider.details == [provider.items[0]["recommendationArn"]]
        assert not provider.starts

        class ArchiveStore:
            async def get_archive(self, run_id, reference):
                return await read_archived(run_id, reference)

        compared_id = await persist_wa_comparison(sessions, SCOPE["tenant_id"], "run-example",
                                                 ArchiveStore(), _independent_verifier, now=NOW)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            hold = await session.get(SourceRetention, (
                SCOPE["tenant_id"], pinned.manifest["archive"]["artifact_id"], run.run_id))
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"],
                                                    pinned.manifest["archive"],
                                                    now=cleanup_at)
            hold.required_until = NOW + timedelta(minutes=1)
            release_source_hold(hold, run, {"tenant_id": run.tenant_id, "run_id": run.run_id,
                                            "generation_id": run.native_job_ref["generation_id"],
                                            "provider_state": "completed", "revision": run.revision},
                                now=cleanup_at)
            assert await source_cleanup_allowed(session, SCOPE["tenant_id"],
                                                pinned.manifest["archive"],
                                                now=cleanup_at)
            await session.commit()
        owned_archive["bytes"] = None
        archive_reads_after_cleanup = owned_archive["reads"]
        with pytest.raises(FileNotFoundError, match="owned archive removed"):
            await read_archived("run-example", pinned.manifest["archive"])
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            store = ResultStore(session, [run], service_id="well_architected",
                                wa_source_authority=current_source_allowed)
            assert (await store.get(compared_id))["recommendations"][0]["comparison"]["status"] == "supported"
            assert (await store.get((await session.scalars(select(AssessmentResult).where(
                AssessmentResult.version == 1))).one().result_id))["revision"] == 1
            debts = (await session.scalars(select(Publication).where(
                Publication.result_id == compared_id))).all()
            assert len(debts) == 2
            assert all(item.state == "unavailable" and
                       item.last_error == "consumer_schema_unadmitted" for item in debts)
        admissions = {}
        lost_ack = {"engineering-insights"}
        reads = {"product-context": 0, "engineering-insights": 0}

        def peer(request):
            destination = ("product-context" if request.url.path.startswith("/internal/v1/context/")
                           else "engineering-insights")
            if request.method == "POST":
                payload = json.loads(request.content)
                publication_id = payload["contribution_id"]
                key = (destination, publication_id)
                if key in admissions:
                    raise AssertionError("duplicate consumer admission")
                receipt = {"receipt_id": f"receipt-{destination}", "contribution_id": publication_id,
                           "admitted_schema": "1.0", "consumer_revision": "1",
                           "record_refs": [payload["result_ref"]]}
                admissions[key] = (payload, receipt)
                if destination in lost_ack:
                    lost_ack.remove(destination)
                    raise httpx.ConnectError("acknowledgement lost")
                return httpx.Response(201, json=receipt)
            publication_id = request.url.path.split("/")[-2 if request.url.path.endswith("/projection") else -1]
            if not request.url.path.endswith("/projection"):
                reads[destination] += 1
            stored = admissions.get((destination, publication_id))
            if stored is None:
                return httpx.Response(404)
            payload, receipt = stored
            if request.url.path.endswith("/projection"):
                return httpx.Response(200, json={**payload, "contribution_hash": sha256(json.dumps(
                    payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
            return httpx.Response(200, json=receipt)

        async with httpx.AsyncClient(transport=httpx.MockTransport(peer),
                                     base_url="https://peer.example.invalid") as transport:
            clients = {}
            for destination, prefix in (("product-context", "context"),
                                        ("engineering-insights", "insights")):
                admission = {"kind": "well_architected_result", "schema_version": "1.0",
                             "private_read_fencing": True, "scope": SCOPE,
                             "path": f"/internal/v1/{prefix}/example-wa-contributions"}
                clients[destination] = WellArchitectedConsumerClient(
                    transport, "delegation-example", scope=SCOPE, destination=destination,
                    peer_admission=admission)
            async def forbidden_provider(*args):
                raise AssertionError("publication recovery must not invoke provider Start")

            service = SimpleNamespace(sessions=sessions, clock=lambda: NOW, publish=forbidden_provider,
                                      well_architected_context_client=clients["product-context"],
                                      well_architected_insights_client=clients["engineering-insights"])
            from l5_services.publication import recovery

            finish = recovery._finish
            write_fault = {"product-context"}

            async def lose_receipt_write(*args, **kwargs):
                if args[1].destination in write_fault and kwargs.get("state") == "published":
                    write_fault.remove(args[1].destination)
                    raise RuntimeError("injected receipt write boundary")
                return await finish(*args, **kwargs)

            monkeypatch.setattr(recovery, "_finish", lose_receipt_write)
            with pytest.raises(RuntimeError, match="injected receipt write boundary"):
                await repair_once(service, now=cleanup_at)
            monkeypatch.setattr(recovery, "_finish", finish)
            assert not write_fault
            async with sessions() as session:
                debts = (await session.scalars(select(Publication).where(
                    Publication.result_id == compared_id))).all()
                assert {item.destination: item.state for item in debts} == {
                    "product-context": "publishing", "engineering-insights": "unavailable"}
                assert all(item.receipt is None for item in debts)
            assert await repair_once(service, now=cleanup_at + timedelta(seconds=61)) == 2
            async with sessions() as session:
                debts = (await session.scalars(select(Publication).where(
                    Publication.result_id == compared_id))).all()
                assert {item.destination: item.state for item in debts} == {
                    "product-context": "published", "engineering-insights": "retry_wait"}
            assert await repair_once(service, now=cleanup_at + timedelta(seconds=122)) == 1
            async with sessions() as session:
                receipts = (await session.scalars(select(Publication).where(
                    Publication.result_id == compared_id))).all()
                assert all(item.state == "published" and item.receipt for item in receipts)
                assert all(item.receipt["record_refs"] == [item.payload["result_ref"]]
                           and item.payload["result_ref"]["record_revision"] == "2"
                           for item in receipts)
                assert len((await session.scalars(select(Publication))).all()) == 4
                from l5_services.models.run import Run

                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                store = ResultStore(session, [run], service_id="well_architected",
                                    wa_source_authority=current_source_allowed)
                assert (await store.get(compared_id))["revision"] == 2
            assert (len(admissions), reads, owned_archive["reads"]) == (
                2, {"product-context": 3, "engineering-insights": 2},
                archive_reads_after_cleanup + 1)
            assert not lost_ack and len(provider.lists) == 2
            assert provider.details == [provider.items[0]["recommendationArn"]]
            assert not provider.starts
    finally:
        await engine.dispose()


async def test_comparison_failure_keeps_native_result_intact(tmp_path):
    engine, sessions = await _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="completed original native generation"):
            await persist_wa_comparison(sessions, SCOPE["tenant_id"], "run-example",
                                        None, None, now=NOW)
        async with sessions() as session:
            assert not (await session.scalars(select(AssessmentResult))).all()
    finally:
        await engine.dispose()


async def test_wa_api_rechecks_source_visibility_and_revocation_before_any_read(tmp_path):
    engine, sessions = await _database(tmp_path)
    provider = MixedGenerationProvider([NOW])
    published = []
    worker, _ = worker_for(sessions, [NOW], provider, published)
    pinned, _, _ = _pinned_review()
    current = {"visible": True}
    try:
        await seed_collection(sessions, provider.native)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = {**run.manifest, **pinned.manifest}
            run.input = {**run.input, "sources": [source["source_ref"] for source in
                                                   pinned.manifest["architecture"]["sources"]]}
            await session.commit()
        for _ in range(6):
            envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1} if not published else published.pop(0)
            await worker.execute(envelope)
            if not published:
                break
        async with sessions() as session:
            original = (await session.scalars(select(AssessmentResult))).one()
            publication = (await session.scalars(select(Publication).where(
                Publication.result_id == original.result_id))).first()

        async def source_authority(run):
            return current["visible"]

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != (
                        SCOPE["product_id"], SCOPE["project_id"], "well_architected"):
                    raise HTTPException(404, detail={"code": "not_found"})
                from l5_services.models.run import Run

                run = await self.session.get(Run, (SCOPE["tenant_id"], "run-example"))
                return ResultStore(self.session, [run], service_id="well_architected",
                                   wa_source_authority=source_authority)

        async def backend():
            async with sessions() as session:
                yield Backend(session)

        app = FastAPI()
        app.include_router(create_results_router(backend), prefix=(
            "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"))
        path = "/v1/products/product-example/projects/project-example/services/well_architected"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://local.example.invalid") as client:
            assert (await client.get(f"{path}/results/{original.result_id}")).status_code == 200
            listed = await client.get(f"{path}/results", params={"limit": 1})
            assert listed.status_code == 200 and len(listed.json()["items"]) == 1
            assert (await client.get(f"{path}/publications/{publication.publication_id}")).status_code == 200
            current["visible"] = False
            denied = [await client.get(f"{path}/results"),
                      await client.get(f"{path}/results", params={"cursor": original.result_id}),
                      await client.get(f"{path}/results/{original.result_id}"),
                      await client.get(f"{path}/publications/{publication.publication_id}"),
                      await client.get(f"{path}/artifacts/readback-{original.result_id}"),
                      await client.get(f"{path}/artifacts/readback-{original.result_id}/content")]
            assert all(response.status_code == 404 and response.json() == {
                "detail": {"code": "not_found"}} for response in denied)
            assert all("native" not in response.text and "receipt" not in response.text
                       for response in denied)
            current["visible"] = True
            async with sessions() as session:
                from l5_services.models.run import Run

                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                assert await fence_source(session, tenant_id=run.tenant_id,
                                          product_id=run.product_id, project_id=run.project_id,
                                          source_ref=run.input["sources"][0], watermark=1, now=NOW) == 2
                await session.commit()
            assert (await client.get(f"{path}/results/{original.result_id}")).status_code == 404
            assert (await client.get(f"{path}/results")).status_code == 404
            assert (await client.get(f"{path}/publications/{publication.publication_id}")).status_code == 404
            assert (await client.get(path.replace("product-example", "other-product") +
                                     f"/results/{original.result_id}")).status_code == 404
        assert not provider.starts
    finally:
        await engine.dispose()


async def test_wa_reuse_does_not_treat_normal_cleanup_as_revocation(tmp_path):
    engine, sessions = await _database(tmp_path)
    try:
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.input = {**run.input, "sources": [{**SCOPE, "source_connection_id": "connection-example",
                                                   "record_type": "document", "record_id": "source-example",
                                                   "record_revision": "pinned-revision"}]}
            hold = SourceRetention(tenant_id=SCOPE["tenant_id"], run_id=run.run_id,
                                   project_id=SCOPE["project_id"], artifact_id="source-artifact",
                                   artifact_ref={"artifact_id": "source-artifact", "version": "1"},
                                   reason="remote_generation", required_until=NOW,
                                   policy_expires_at=NOW + timedelta(hours=1), released_at=NOW)
            session.add(hold)
            await session.commit()
            store = ResultStore(session, [run], service_id="well_architected",
                                wa_source_authority=current_source_allowed)
            assert await store.list() == []
            hold.revoked_at = NOW
            await session.commit()
            with pytest.raises(HTTPException) as error:
                await store.list()
            assert error.value.status_code == 404
    finally:
        await engine.dispose()


async def test_ac_03(tmp_path):
    engine, sessions = await _database(tmp_path)
    state = {"visible": True, "actor": "design-owner", "bad_receipt": False, "lost_ack": True, "reuse_active": True}
    reuse_admissions, publication_admissions, calls = {}, {}, []
    invalidations = {}
    result_id = "compared-result-example"
    task_id = "later-design-task"
    pinned, collected, archived = _pinned_review()
    selected_sources = [entry["source_ref"] for entry in pinned.manifest["architecture"]["sources"]]
    tasks = {identity: {"actor_id": "design-owner", "scope": SCOPE,
                        "source_refs": selected_sources}
             for identity in (task_id, "another-design-task")}
    try:
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = deepcopy(pinned.manifest)
            run.input = {**run.input, "sources": selected_sources}
            run.native_job_ref = deepcopy(pinned.native_job_ref)
            run.admission = {**run.admission, "generation_scope": pinned.admission["generation_scope"]}
            result = AssessmentResult(tenant_id=run.tenant_id, run_id=run.run_id,
                                      result_id=result_id, version=2, payload={"native_job_ref": collected["native_job_ref"]})
            detail = normalize_wa_result(run, result_id, 2, collected, NOW)
            comparisons = compare_wa_recommendations(run, collected, archived, _independent_verifier)
            detail.payload = {**detail.payload, "based_on_result_id": "native-result-example",
                              "comparison": comparisons,
                              "recommendations": [{**item, "comparison": verdict}
                                                  for item, verdict in zip(detail.payload["recommendations"],
                                                                           comparisons, strict=True)]}
            session.add_all([result, detail, AssessmentResult(
                tenant_id=run.tenant_id, run_id=run.run_id, result_id="native-result-example",
                version=1, payload=collected),
                normalize_wa_result(run, "native-result-example", 1, collected, NOW)])
            publications = create_wa_publications(run, result, detail, now=NOW)
            session.add_all(publications)
            await session.commit()

        def peer(request):
            path = request.url.path
            destination = "product-context" if "/context/" in path else "engineering-insights"
            if path.endswith("/invalidate"):
                assert request.method == "POST"
                publication_id = path.split("/")[-2]
                admitted, admission_receipt = publication_admissions[(destination, publication_id)]
                assert request.headers["X-L5-Delegation-ID"] == "delegation-example"
                assert request.headers["Idempotency-Key"] == (
                    f"{SCOPE['tenant_id']}/{result_id}/2/{destination}/1.0")
                assert json.loads(request.content) == {
                    "source_refs": [selected_sources[0]], "reason": "source_access_revoked",
                    "invalidation_watermark": 1}
                assert admitted["scope"] == SCOPE
                assert destination not in invalidations
                invalidations[destination] = {
                    "receipt_id": f"invalidated-{destination}", "applied_watermark": 1,
                    "record_refs": admission_receipt["record_refs"]}
                return httpx.Response(200, json=invalidations[destination])
            if "/example-contributions" in path:
                if request.method == "POST":
                    payload = json.loads(request.content)
                    key = (destination, payload["contribution_id"])
                    assert key not in publication_admissions
                    receipt = {"receipt_id": f"admission-{destination}",
                               "contribution_id": payload["contribution_id"],
                               "admitted_schema": "1.0", "consumer_revision": "1",
                               "record_refs": [payload["result_ref"]]}
                    publication_admissions[key] = (payload, receipt)
                    return httpx.Response(201, json=receipt)
                publication_id = path.split("/")[-2 if path.endswith("/projection") else -1]
                admitted = publication_admissions.get((destination, publication_id))
                if admitted is None:
                    return httpx.Response(404)
                payload, receipt = admitted
                if path.endswith("/projection"):
                    return httpx.Response(200, json={**payload, "contribution_hash": sha256(json.dumps(
                        payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
                return httpx.Response(200, json=receipt)
            if not state["visible"]:
                return httpx.Response(404)
            if request.method == "GET":
                receipt = reuse_admissions.get((destination, path))
                return httpx.Response(200, json=receipt) if receipt else httpx.Response(404)
            assert request.method == "POST"
            payload = json.loads(request.content)
            admitted = publication_admissions.get((destination, payload["contribution_id"]))
            task = tasks.get(payload.get("task_id"))
            if (admitted is None or task is None or payload.get("actor_id") != task["actor_id"]
                    or payload.get("scope") != task["scope"]
                    or admitted[0].get("source_refs") != task["source_refs"]
                    or payload.get("result_ref") != admitted[0]["result_ref"]
                    or (destination == "engineering-insights" and not any(
                        receipt["receipt_id"] == payload.get("retrieval_receipt_id")
                        for (peer_destination, _), receipt in reuse_admissions.items()
                        if peer_destination == "product-context"))):
                return httpx.Response(403)
            receipt = {**payload, "receipt_id": f"retrieved-{task_id}" if destination == "product-context"
                       else f"used-{task_id}"}
            if state["bad_receipt"] and destination == "engineering-insights":
                receipt["result_ref"] = {**receipt["result_ref"], "record_revision": "1"}
            reuse_admissions[(destination, path + "/" + payload["task_id"])] = receipt
            calls.append(destination)
            if destination == "engineering-insights" and state["lost_ack"]:
                state["lost_ack"] = False
                raise httpx.ConnectError("acknowledgement lost")
            return httpx.Response(201, json=receipt)

        async with httpx.AsyncClient(transport=httpx.MockTransport(peer),
                                     base_url="https://peer.example.invalid") as transport:
            clients = {}
            for destination, prefix in (("product-context", "context"),
                                        ("engineering-insights", "insights")):
                clients[destination] = WellArchitectedConsumerClient(
                    transport, "delegation-example", scope=SCOPE, destination=destination,
                    peer_admission={"kind": "well_architected_result", "schema_version": "1.0",
                                    "private_read_fencing": True, "task_fencing": True, "scope": SCOPE,
                                    "path": f"/internal/v1/{prefix}/example-contributions",
                                    ("query_path" if prefix == "context" else "usage_path"):
                                        f"/internal/v1/{prefix}/example-reuse"})
            async def forbidden_provider(*args):
                raise AssertionError("later-task publication must not restart the provider")

            service = SimpleNamespace(sessions=sessions, clock=lambda: NOW, publish=forbidden_provider,
                                      well_architected_context_client=clients["product-context"],
                                      well_architected_insights_client=clients["engineering-insights"])
            assert await repair_once(service, now=NOW) == 2
            async with sessions() as session:
                admitted_rows = (await session.scalars(select(Publication).where(
                    Publication.result_id == result_id))).all()
                assert {row.destination for row in admitted_rows} == {
                    "product-context", "engineering-insights"}
                assert all(row.state == "published" and row.receipt["record_refs"] == [
                    row.payload["result_ref"]] for row in admitted_rows)
            assert len(publication_admissions) == 2

            async def source_authority(run):
                return state["visible"] and state["actor"] == "design-owner"

            async def reuse_authority(run, result_id):
                return state["reuse_active"]

            async def task_authority(run, selected_task, actor):
                task = tasks.get(selected_task)
                return bool(task and task["actor_id"] == actor and task["scope"] == run.admission["scope"])

            class Backend:
                def __init__(self, session):
                    self.session = session

                async def result_store(self, product_id, project_id, service_id):
                    if (product_id, project_id, service_id) != (
                            SCOPE["product_id"], SCOPE["project_id"], "well_architected"):
                        raise HTTPException(404, detail={"code": "not_found"})
                    from l5_services.models.run import Run

                    run = await self.session.get(Run, (SCOPE["tenant_id"], "run-example"))
                    return ResultStore(self.session, [run], service_id=service_id,
                                       actor_id=state["actor"], clock=lambda: NOW,
                                       wa_source_authority=source_authority,
                                       task_authority=task_authority,
                                       wa_reuse_authority=reuse_authority,
                                       wa_context_client=clients["product-context"],
                                       wa_insights_client=clients["engineering-insights"])

            async def backend():
                async with sessions() as session:
                    yield Backend(session)

            app = FastAPI()
            app.include_router(create_results_router(backend), prefix=(
                "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"))
            path = ("/v1/products/product-example/projects/project-example/services/"
                    f"well_architected/results/{result_id}/design-usage")
            root = path.split("/results/", 1)[0]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://local.example.invalid") as client:
                async def request():
                    return await client.post(path, json={"task_id": task_id})

                assert (await client.get(path + "/" + task_id)).status_code == 404
                assert calls == []  # GET must never create peer usage receipts.
                first = await request()
                assert first.status_code == 503 and calls == ["product-context", "engineering-insights"]
                second = await request()
                assert second.status_code == 200 and calls == ["product-context", "engineering-insights"]
                receipt = second.json()
                assert receipt["result"]["revision"] == 2
                assert receipt["citation"] == f"well_architected_result:{result_id}@2"
                assert receipt["result"]["based_on_result_id"] == "native-result-example"
                assert receipt["result"]["review_mode"] == "source_only"
                assert {item["comparison"]["status"] for item in receipt["result"]["recommendations"]} == {
                    "supported", "contradicted", "not_verifiable"}
                assert receipt["retrieval_receipt"]["result_ref"]["record_id"] == result_id
                assert receipt["retrieval_receipt"]["result_ref"]["record_revision"] == "2"
                assert receipt["retrieval_receipt"]["task_id"] == task_id
                assert receipt["usage_receipt"]["retrieval_receipt_id"] == receipt["retrieval_receipt"]["receipt_id"]
                assert receipt["usage_receipt"]["citation"] == receipt["citation"]
                assert (await request()).json() == receipt
                readback = await client.get(path + "/" + task_id)
                assert readback.status_code == 200 and readback.json() == receipt
                assert readback.headers["cache-control"] == "no-store"
                # Cached receipts remain private after retained-result consent is revoked.
                state["reuse_active"] = False
                assert (await client.get(path + "/" + task_id)).status_code == 404
                assert (await request()).status_code == 404
                assert calls == ["product-context", "engineering-insights"]
                state["reuse_active"] = True
                original_task = tasks.pop(task_id)
                assert (await client.get(path + "/" + task_id)).status_code == 404
                assert (await request()).status_code == 404
                tasks[task_id] = original_task
                assert len(publication_admissions) == 2 and len(reuse_admissions) == 2
                state["bad_receipt"] = True
                assert (await client.post(path, json={"task_id": "another-design-task"})).status_code == 503
                state["bad_receipt"] = False
                state["actor"] = "foreign-actor"
                denied = [await request(),
                          await client.post(path, json={"task_id": "foreign-task"}),
                          await client.get(f"{root}/results/{result_id}"),
                          await client.get(f"{root}/results"),
                          await client.get(f"{root}/results", params={"cursor": result_id}),
                          await client.get(f"{root}/publications/{publications[0].publication_id}"),
                          await client.get(f"{root}/artifacts/readback-{result_id}"),
                          await client.get(f"{root}/artifacts/readback-{result_id}/content")]
                assert all(response.status_code == 404 and response.json() == {
                    "detail": {"code": "not_found"}} for response in denied)
                assert all("recommendations" not in response.text and "receipt" not in response.text
                           and "items" not in response.text for response in denied)
                state["actor"] = "design-owner"
                state["visible"] = False
                assert (await request()).status_code == 404
                assert (await transport.get(
                    f"/internal/v1/context/example-reuse/{task_id}")).status_code == 404
                state["visible"] = True
                async with sessions() as session:
                    from l5_services.models.run import Run

                    run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                    assert await fence_source(session, tenant_id=run.tenant_id,
                                              product_id=run.product_id, project_id=run.project_id,
                                              source_ref=run.input["sources"][0], watermark=1, now=NOW) == 2
                    await session.commit()
                denied = [await request(),
                          await client.get(f"{root}/results"),
                          await client.get(f"{root}/results", params={"cursor": result_id}),
                          await client.get(f"{root}/results/{result_id}"),
                          await client.get(f"{root}/publications/{publications[0].publication_id}"),
                          await client.get(f"{root}/artifacts/readback-{result_id}"),
                          await client.get(f"{root}/artifacts/readback-{result_id}/content"),
                          await client.post(path.replace("product-example", "other-product"),
                                            json={"task_id": task_id})]
                assert all(response.status_code == 404 and response.json() == {
                    "detail": {"code": "not_found"}} for response in denied)
                assert all("recommendations" not in response.text and "receipt" not in response.text
                           and "items" not in response.text for response in denied)
                async with sessions() as session:
                    for publication in await session.scalars(select(Publication)):
                        consumer = clients[publication.destination]
                        assert publication.state == "invalidation_pending"
                        assert not consumer.can_publish(publication)
                        with pytest.raises(ValueError):
                            await consumer.read_receipt(publication)
                        with pytest.raises(ValueError):
                            await consumer.reuse_receipt(publication, task_id=task_id,
                                                         actor_id="design-owner", citation="denied")
                        pending = deepcopy(publication)
                        pending.state = "pending"
                        with pytest.raises(ValueError):
                            await consumer.publish(pending)
                        for field in ("tenant_id", "product_id", "project_id"):
                            foreign = deepcopy(publication)
                            foreign.payload["scope"][field] = "foreign"
                            with pytest.raises(ValueError):
                                await consumer.invalidate(foreign)
                        for field, value in (("destination", "unadmitted-consumer"),
                                             ("schema_version", "unadmitted-schema")):
                            foreign = deepcopy(publication)
                            setattr(foreign, field, value)
                            with pytest.raises(ValueError):
                                await consumer.invalidate(foreign)
                        foreign = deepcopy(publication)
                        foreign.payload["kind"] = "finops_cost_result"
                        with pytest.raises(ValueError):
                            await consumer.invalidate(foreign)
                assert invalidations == {}
                assert await repair_once(service, now=NOW) == 2
                assert await repair_once(service, now=NOW) == 0
                assert set(invalidations) == {"product-context", "engineering-insights"}
                async with sessions() as session:
                    for publication in await session.scalars(select(Publication)):
                        assert publication.state == "invalidated"
                        assert publication.invalidation_watermark == publication.applied_watermark == 1
                        assert publication.invalidation_receipt == invalidations[publication.destination]
                        assert publication.last_error is None
                        assert not clients[publication.destination].can_publish(publication)
                assert (await request()).status_code == 404
                clients["product-context"] = WellArchitectedConsumerClient(
                    transport, "delegation-example", scope=SCOPE, destination="product-context",
                    peer_admission={"kind": "well_architected_result", "schema_version": "1.0",
                                    "private_read_fencing": True, "scope": SCOPE,
                                    "path": "/internal/v1/context/example-contributions"})
                assert (await client.post(path, json={"task_id": "unadmitted-task"})).status_code == 404
                assert (await client.post(path, json={"task_id": "run-example"})).status_code == 404
            async with sessions() as session:
                usages = (await session.scalars(select(WellArchitectedUsage))).all()
                assert len(usages) == 1 and usages[0].actor_id == "design-owner"
                assert usages[0].task_id == task_id and usages[0].result_id == result_id
                assert usages[0].retrieval_receipt == receipt["retrieval_receipt"]
                assert usages[0].usage_receipt == receipt["usage_receipt"]
    finally:
        await engine.dispose()


async def test_activation_skips_full_batch_of_native_debts(tmp_path):
    engine, sessions = await _database(tmp_path)
    pinned, collected, archived = _pinned_review()
    try:
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = deepcopy(pinned.manifest)
            run.input = {**run.input, "sources": [entry["source_ref"] for entry in
                                                pinned.manifest["architecture"]["sources"]]}
            run.native_job_ref = deepcopy(pinned.native_job_ref)
            run.admission = {**run.admission, "generation_scope": pinned.admission["generation_scope"]}
            for index in range(51):
                version = 1 if index < 50 else 2
                result_id = f"activation-result-{index}"
                publication_run = run
                if 0 < index < 50:
                    publication_run = Run(**{column.key: deepcopy(getattr(run, column.key))
                                             for column in Run.__table__.columns})
                    publication_run.run_id = f"activation-run-{index}"
                    session.add(publication_run)
                result = AssessmentResult(tenant_id=run.tenant_id, run_id=publication_run.run_id,
                                          result_id=result_id, version=version, payload=collected)
                detail = normalize_wa_result(publication_run, result_id, version, collected, NOW)
                if version == 2:
                    comparisons = compare_wa_recommendations(run, collected, archived, _independent_verifier)
                    detail.payload = {**detail.payload, "based_on_result_id": "activation-result-0",
                                      "comparison": comparisons,
                                      "recommendations": [{**item, "comparison": verdict} for item, verdict in
                                                          zip(detail.payload["recommendations"], comparisons,
                                                              strict=True)]}
                session.add_all([result, detail, *create_wa_publications(
                    publication_run, result, detail, now=NOW + timedelta(seconds=index))])
            await session.commit()

        def forbidden_peer(request):
            raise AssertionError("activation must not contact consumers or providers")

        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_peer),
                                    base_url="http://consumer.test") as transport:
            for destination, prefix in (("product-context", "context"), ("engineering-insights", "insights")):
                consumer = WellArchitectedConsumerClient(
                    transport, "delegation-example", scope=SCOPE, destination=destination,
                    peer_admission={"kind": "well_architected_result", "schema_version": "1.0",
                                    "private_read_fencing": True, "scope": SCOPE,
                                    "path": f"/internal/v1/{prefix}/example-contributions"})
                assert [await activate_wa_publications(sessions, consumer, now=NOW + timedelta(minutes=1))
                        for _ in range(3)] == [1, 0, 0]
        async with sessions() as session:
            rows = (await session.scalars(select(Publication))).all()
            native = [row for row in rows if row.result_version == 1]
            compared = [row for row in rows if row.result_version == 2]
            assert len(native) == 100 and len(compared) == 2
            assert all(row.state == "unavailable" and row.last_error == "consumer_schema_unadmitted"
                       and row.revision == 1 and row.attempts == 0 for row in native)
            assert all(row.state == "pending" and row.last_error is None
                       and row.revision == 2 and row.attempts == 0 for row in compared)
    finally:
        await engine.dispose()


async def test_ac_04(tmp_path):
    engine, sessions = await _database(tmp_path)
    pinned, collected, archived = _pinned_review()
    base_id, candidate_id = "first-compared-review", "later-compared-review"
    foreign_id = "foreign-compared-review"
    expected_sources = [
        {**SCOPE, "record_type": "document", "source_connection_id": "connection-example",
         "record_id": f"source-example-{index}", "record_revision": sha256(content).hexdigest()}
        for index, content in enumerate((b"encrypt = true\nfirewall = false\n", b"Protect stored data\n"))
    ]
    expected_generation_scope = {"pillars": ["SECURITY"], "goalIds": ["pinned-goal-example"]}
    try:
        async with sessions() as session:
            from l5_services.models.run import Run

            base_run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            base_run.manifest = deepcopy(pinned.manifest)
            base_run.admission = deepcopy(pinned.admission)
            base_run.input = {"capability": "architecture_review", "sources": [
                deepcopy(entry["source_ref"]) for entry in pinned.manifest["architecture"]["sources"]]}
            base_run.native_job_ref = deepcopy(pinned.native_job_ref)
            next_run = Run(**{column.key: deepcopy(getattr(base_run, column.key))
                              for column in Run.__table__.columns if column.key != "run_id"},
                           run_id="later-review")
            next_run.native_job_ref = {**next_run.native_job_ref, "generation_id": "later-generation"}
            session.add(next_run)
            assert base_run.input["sources"] == next_run.input["sources"] == expected_sources
            assert base_run.admission["generation_scope"] == next_run.admission[
                "generation_scope"] == expected_generation_scope
            baseline_detail = normalize_wa_result(base_run, base_id, 2, collected, NOW)
            baseline_verdicts = compare_wa_recommendations(base_run, collected, archived, _independent_verifier)
            baseline_detail.payload = {**baseline_detail.payload, "based_on_result_id": "first-native",
                                       "comparison": baseline_verdicts,
                                       "recommendations": [{**item, "comparison": verdict}
                                                           for item, verdict in zip(
                                                               baseline_detail.payload["recommendations"],
                                                               baseline_verdicts, strict=True)]}
            candidate_collected = {"native_job_ref": next_run.native_job_ref,
                                   "recommendations": [
                                       {**item, "generationId": "later-generation",
                                        "recommendationArn": item["recommendationArn"] + "-later"}
                                       for item in collected["recommendations"][:2]]}
            candidate_collected["recommendations"].append({
                **candidate_collected["recommendations"][-1],
                "recommendationArn": "native-new-later", "title": "Review network boundaries"})

            def later_verifier(recommendation, files, goals):
                if recommendation["title"] == "Encrypt stored data":
                    return {"status": "not_verifiable", "explanation": "No independently cited source",
                            "citations": []}
                return _independent_verifier(recommendation, files, goals)

            candidate_detail = normalize_wa_result(next_run, candidate_id, 2, candidate_collected, NOW)
            candidate_verdicts = compare_wa_recommendations(
                next_run, candidate_collected, archived, later_verifier)
            candidate_detail.payload = {**candidate_detail.payload, "based_on_result_id": "later-native",
                                        "comparison": candidate_verdicts,
                                        "recommendations": [{**item, "comparison": verdict}
                                                            for item, verdict in zip(
                                                                candidate_detail.payload["recommendations"],
                                                                candidate_verdicts, strict=True)]}
            foreign = Run(**{column.key: deepcopy(getattr(base_run, column.key))
                             for column in Run.__table__.columns if column.key != "run_id"},
                          run_id="foreign-review")
            foreign.product_id = "foreign-product"
            foreign.admission = {**foreign.admission, "scope": {**SCOPE, "product_id": "foreign-product"}}
            foreign.input = {**foreign.input, "sources": [
                {**source, "product_id": "foreign-product"} for source in expected_sources]}
            foreign_detail = ResultDetail(tenant_id=SCOPE["tenant_id"], result_id=foreign_id,
                                          payload={**baseline_detail.payload, "result_id": foreign_id,
                                                   "run_id": foreign.run_id, "scope": foreign.admission["scope"]})
            candidate_finding = wa_recommendation_findings(candidate_detail)[0]
            foreign_finding = wa_recommendation_findings(foreign_detail)[0]
            session.add_all([
                foreign,
                AssessmentResult(tenant_id=SCOPE["tenant_id"], result_id=base_id,
                                 run_id=base_run.run_id, version=2, payload={}),
                AssessmentResult(tenant_id=SCOPE["tenant_id"], result_id=candidate_id,
                                 run_id=next_run.run_id, version=2, payload={}),
                AssessmentResult(tenant_id=SCOPE["tenant_id"], result_id=foreign_id,
                                 run_id=foreign.run_id, version=2, payload={}),
                baseline_detail, candidate_detail, foreign_detail, candidate_finding, foreign_finding,
            ])
            await session.commit()

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != (
                        SCOPE["product_id"], SCOPE["project_id"], "well_architected"):
                    raise HTTPException(404, detail={"code": "not_found"})
                from l5_services.models.run import Run

                runs = (await self.session.scalars(select(Run).where(
                    Run.tenant_id == SCOPE["tenant_id"]))).all()
                selected = runs if state["leak"] else [run for run in runs if run.product_id == product_id]
                return ResultStore(self.session, selected, service_id=service_id,
                                   actor_id="design-owner", clock=lambda: NOW,
                                   wa_source_authority=current_source_allowed)

        async def backend():
            async with sessions() as session:
                yield Backend(session)

        app = FastAPI()
        app.include_router(create_results_router(backend), prefix=(
            "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"))
        path = "/v1/products/product-example/projects/project-example/services/well_architected/comparisons"
        root = path.rsplit("/comparisons", 1)[0]
        request = {"baseline_result_id": base_id, "candidate_result_id": candidate_id}
        state = {"leak": False}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://local.example.invalid") as client:
            async def compare():
                response = await client.post(path, json=request)
                assert response.status_code == 200
                return response.json()

            comparable = await compare()
            assert comparable["comparability"] == "comparable" and comparable["reasons"] == []
            assert (len(comparable["changed"]), len(comparable["added"]),
                    len(comparable["removed"]), comparable["unchanged_count"]) == (1, 1, 1, 1)
            changed = comparable["changed"][0]
            assert changed["baseline"]["comparison"]["status"] == "supported"
            citation = changed["baseline"]["comparison"]["citations"][0]
            assert citation["quote"] == "encrypt = true"
            assert citation["source_ref"] == expected_sources[0]
            assert citation["sha256"] == expected_sources[0]["record_revision"]
            assert changed["candidate"]["comparison"]["status"] == "not_verifiable"
            assert changed["baseline"]["native_id"] != changed["candidate"]["native_id"]

            page = await client.get(f"{root}/results", params={"limit": 1})
            assert page.status_code == 200 and len(page.json()["items"]) == 1
            next_page = await client.get(f"{root}/results", params={
                "limit": 1, "cursor": page.json()["next_cursor"]})
            assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
            assert "next_cursor" not in next_page.json()
            assert {item["result_id"] for response in (page, next_page)
                    for item in response.json()["items"]} == {base_id, candidate_id}
            triage = f"{root}/results/{candidate_id}/findings/{candidate_finding.finding_id}/triage"
            decision = {"decision": "open", "reason": "Verify cited encryption claim"}
            assert (await client.put(triage, json=decision, headers={"If-Match": '"0"'})).status_code == 200
            assert (await client.get(triage)).json()["items"][0]["decision"] == decision
            foreign_root = root.replace("product-example", "foreign-product")
            foreign_triage = (f"{foreign_root}/results/{foreign_id}/findings/"
                              f"{foreign_finding.finding_id}/triage")
            denied = [
                await client.get(f"{foreign_root}/results"),
                await client.get(f"{foreign_root}/results/{foreign_id}"),
                await client.get(foreign_triage),
                await client.put(foreign_triage, json=decision, headers={"If-Match": '"0"'}),
                await client.get(f"{root}/results/{foreign_id}"),
                await client.get(f"{root}/results", params={"cursor": foreign_id}),
                await client.get(f"{root}/results/{candidate_id}/findings", params={
                    "cursor": foreign_finding.finding_id}),
                await client.get(f"{root}/results/{foreign_id}/findings/{foreign_finding.finding_id}/triage"),
                await client.post(path, json={**request, "baseline_result_id": foreign_id}),
            ]
            assert all(response.status_code == 404 and response.json() == {
                "detail": {"code": "not_found"}} for response in denied)
            assert all("Verify cited encryption claim" not in response.text
                       and "items" not in response.text for response in denied)
            state["leak"] = True
            leaked = [await client.get(f"{root}/results"), await client.get(triage),
                      await client.post(path, json=request)]
            assert all(response.status_code == 404 and response.json() == {
                "detail": {"code": "not_found"}} for response in leaked)
            state["leak"] = False
            async with sessions() as session:
                rows = (await session.scalars(select(Triage))).all()
                assert len(rows) == 1 and rows[0].finding_id == candidate_finding.finding_id

            for update, reason in (
                    ({"input": {**next_run.input, "sources": [{**next_run.input["sources"][0],
                            "record_revision": "changed"}, *next_run.input["sources"][1:]]}},
                     "source_selection_changed"),
                    ({"admission": {**next_run.admission, "generation_scope": {
                        **next_run.admission["generation_scope"], "pillars": ["RELIABILITY"]}}},
                     "pillar_coverage_changed"),
                    ({"admission": {**next_run.admission, "generation_scope": {
                        **next_run.admission["generation_scope"], "goalIds": ["different-goal"]}}},
                     "goal_coverage_changed"),
                    ({"manifest": {**next_run.manifest, "architecture": {
                        **next_run.manifest["architecture"], "files": [
                            {**next_run.manifest["architecture"]["files"][0], "sha256": "f" * 64},
                            *next_run.manifest["architecture"]["files"][1:]]}}},
                     "manifest_coverage_changed")):
                async with sessions() as session:
                    from l5_services.models.run import Run

                    row = await session.get(Run, (SCOPE["tenant_id"], "later-review"))
                    original = deepcopy(getattr(row, next(iter(update))))
                    for field, value in update.items():
                        setattr(row, field, value)
                    await session.commit()
                denied = await compare()
                assert denied["comparability"] == "not_comparable"
                assert reason in denied["reasons"]
                assert not any(denied[field] for field in ("added", "removed", "changed"))
                async with sessions() as session:
                    row = await session.get(Run, (SCOPE["tenant_id"], "later-review"))
                    setattr(row, next(iter(update)), original)
                    await session.commit()
            assert (await compare())["comparability"] == "comparable"
            async with sessions() as session:
                row = await session.get(ResultDetail, (SCOPE["tenant_id"], candidate_id))
                original_detail = deepcopy(row.payload)
                row.payload = {**row.payload, "comparison": [{**row.payload["comparison"][0],
                    "status": "not_checked"}, *row.payload["comparison"][1:]]}
                await session.commit()
            unverifiable = await compare()
            assert unverifiable["comparability"] == "not_comparable"
            assert "independent_comparison_unavailable" in unverifiable["reasons"]
            async with sessions() as session:
                row = await session.get(ResultDetail, (SCOPE["tenant_id"], candidate_id))
                row.payload = original_detail
                await session.commit()
            assert (await compare())["comparability"] == "comparable"
    finally:
        await engine.dispose()


async def test_wa_api_scopes_pagination_comparison_and_triage_to_current_product(tmp_path):
    engine, sessions = await _database(tmp_path)
    provider = MixedGenerationProvider([NOW])
    provider.items[0]["title"] = "Encrypt stored data"
    published = []
    worker, _ = worker_for(sessions, [NOW], provider, published)
    pinned, _, archived = _pinned_review()
    state = {"visible": True, "leak": False}
    foreign_result_id = "foreign-result-example"
    try:
        await seed_collection(sessions, provider.native)
        async with sessions() as session:
            from l5_services.models.run import Run

            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = deepcopy(pinned.manifest)
            run.input = {**run.input, "sources": [source["source_ref"] for source in
                                                  pinned.manifest["architecture"]["sources"]]}
            run.admission = {**run.admission, "generation_scope": pinned.admission["generation_scope"]}
            await session.commit()
        for _ in range(6):
            envelope = ({"schema_version": "1.0", "work_id": "work-example", "generation": 1}
                        if not published else published.pop(0))
            await worker.execute(envelope)
            if not published:
                break

        class ArchiveStore:
            async def get_archive(self, run_id, reference):
                assert run_id == "run-example" and reference == pinned.manifest["archive"]
                return archived

        compared_id = await persist_wa_comparison(
            sessions, SCOPE["tenant_id"], "run-example", ArchiveStore(), _independent_verifier, now=NOW)
        async with sessions() as session:
            from l5_services.models.run import Run

            original = (await session.scalars(select(AssessmentResult).where(
                AssessmentResult.version == 1))).one()
            native_finding = (await session.scalars(select(Finding).where(
                Finding.result_id == original.result_id))).one()
            compared_finding = (await session.scalars(select(Finding).where(
                Finding.result_id == compared_id))).one()
            assert native_finding.kind == compared_finding.kind == "wa_recommendation"
            assert native_finding.payload["comparison"]["status"] == "not_checked"
            assert compared_finding.payload["comparison"]["status"] == "supported"
            assert native_finding.native_id == compared_finding.native_id
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            foreign = Run(**{column.key: deepcopy(getattr(run, column.key))
                             for column in Run.__table__.columns if column.key != "run_id"},
                          run_id="foreign-review")
            foreign.product_id = "foreign-product"
            foreign.admission = {**foreign.admission, "scope": {**SCOPE, "product_id": "foreign-product"}}
            foreign.input = {**foreign.input, "sources": [
                {**source, "product_id": "foreign-product"} for source in foreign.input["sources"]]}
            session.add(foreign)
            detail = await session.get(ResultDetail, (SCOPE["tenant_id"], original.result_id))
            foreign_detail = ResultDetail(tenant_id=SCOPE["tenant_id"], result_id=foreign_result_id,
                                          payload={**detail.payload, "result_id": foreign_result_id,
                                                   "run_id": foreign.run_id,
                                                   "scope": foreign.admission["scope"]})
            session.add_all([
                AssessmentResult(tenant_id=SCOPE["tenant_id"], result_id=foreign_result_id,
                                 run_id=foreign.run_id, version=1, payload={}),
                foreign_detail,
            ])
            foreign_findings = wa_recommendation_findings(foreign_detail)
            session.add_all(foreign_findings)
            await session.commit()

        async def source_authority(run):
            return state["visible"] and run.product_id == SCOPE["product_id"]

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                from l5_services.models.run import Run

                runs = (await self.session.scalars(select(Run).where(
                    Run.tenant_id == SCOPE["tenant_id"]))).all()
                if state["leak"]:
                    selected = runs
                else:
                    selected = [run for run in runs if run.product_id == product_id]
                return ResultStore(self.session, selected, service_id=service_id,
                                   actor_id="design-owner", clock=lambda: NOW,
                                   wa_source_authority=source_authority)

        async def backend():
            async with sessions() as session:
                yield Backend(session)

        app = FastAPI()
        app.include_router(create_results_router(backend), prefix=(
            "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"))
        root = "/v1/products/product-example/projects/project-example/services/well_architected"
        foreign_root = root.replace("product-example", "foreign-product")
        comparison = {"baseline_result_id": original.result_id, "candidate_result_id": compared_id}
        triage = f"{root}/results/{compared_id}/findings/{compared_finding.finding_id}/triage"
        decision = {"decision": "open", "reason": "Investigate the cited source"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://local.example.invalid") as client:
            page = await client.get(f"{root}/results", params={"limit": 1})
            assert page.status_code == 200 and len(page.json()["items"]) == 1
            cursor = page.json()["next_cursor"]
            next_page = await client.get(f"{root}/results", params={"limit": 1, "cursor": cursor})
            assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
            assert "next_cursor" not in next_page.json()
            assert (await client.get(f"{root}/results/{compared_id}/findings")).json()["items"][0][
                "finding_id"] == compared_finding.finding_id
            assert (await client.post(f"{root}/comparisons", json=comparison)).status_code == 200
            assert (await client.put(triage, json=decision, headers={"If-Match": '"0"'})).status_code == 200
            assert (await client.get(triage)).json()["items"][0]["decision"] == decision

            foreign_triage = (f"{foreign_root}/results/{foreign_result_id}/findings/"
                              f"{foreign_findings[0].finding_id}/triage")
            denied = [
                await client.get(f"{foreign_root}/results"),
                await client.get(f"{foreign_root}/results/{foreign_result_id}"),
                await client.get(foreign_triage),
                await client.put(foreign_triage, json=decision, headers={"If-Match": '"0"'}),
                await client.get(f"{root}/results", params={"cursor": foreign_result_id}),
                await client.get(f"{root}/results/{compared_id}/findings", params={
                    "cursor": foreign_findings[0].finding_id}),
                await client.get(f"{root}/results/{foreign_result_id}"),
                await client.get(f"{root}/results/{foreign_result_id}/findings/{foreign_findings[0].finding_id}/triage"),
                await client.post(f"{root}/comparisons", json={
                    **comparison, "baseline_result_id": foreign_result_id}),
            ]
            state["leak"] = True
            denied.extend((await client.get(f"{root}/results"),
                           await client.get(triage),
                           await client.post(f"{root}/comparisons", json=comparison)))
            state["leak"] = False
            state["visible"] = False
            denied.extend((await client.get(f"{root}/results"),
                           await client.get(f"{root}/results", params={"cursor": cursor}),
                           await client.get(f"{root}/results/{compared_id}/findings"),
                           await client.get(triage),
                           await client.put(triage, json=decision, headers={"If-Match": '"1"'}),
                           await client.post(f"{root}/comparisons", json=comparison)))
            assert all(response.status_code == 404 and response.json() == {
                "detail": {"code": "not_found"}} for response in denied)
            assert all("Investigate the cited source" not in response.text
                       and "native" not in response.text and "items" not in response.text
                       for response in denied)
        async with sessions() as session:
            rows = (await session.scalars(select(Triage))).all()
            assert len(rows) == 1 and rows[0].finding_id == compared_finding.finding_id
        assert not provider.starts
    finally:
        await engine.dispose()
