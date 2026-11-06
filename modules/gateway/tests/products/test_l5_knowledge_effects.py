"""Crash/recovery and stale-claim tests for retained native knowledge effects."""

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from l5_services.devops.knowledge import KnowledgeAccessDenied
from l5_services.devops.knowledge_native import KnowledgeNativeDriver
from l5_services.devops.knowledge_worker import KnowledgeSetupWorker
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.workers.execution import AuthorityRevoked
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["claimed_knowledge", "knowledge_fixture", "consent_fixture", "l5_owner"]


class Native:
    def __init__(self):
        self.assets, self.calls = {}, []
        self.lose_ack = False

    def list_assets(self, **request):
        self.calls.append("list")
        return {"items": [self.get_asset(assetId=key)["asset"] for key in self.assets]}

    def create_asset(self, **request):
        self.calls.append("create")
        key = str(len(self.assets) + 1)
        self.assets[key] = {
            "assetId": key,
            "version": 1,
            "assetType": request["assetType"],
            "metadata": deepcopy(request["metadata"]),
            "content": request["content"]["file"],
        }
        if self.lose_ack:
            raise TimeoutError("acknowledgement lost")
        return {"asset": deepcopy(self.assets[key])}

    def update_asset(self, **request):
        self.calls.append("update")
        value = self.assets[request["assetId"]]
        value.update(version=value["version"] + 1, metadata=deepcopy(request["metadata"]), content=request["content"]["file"])
        if self.lose_ack:
            raise TimeoutError("acknowledgement lost")
        return {"asset": deepcopy(value)}

    def delete_asset(self, **request):
        raise AssertionError("publication cannot delete assets")

    def get_asset(self, **request):
        self.calls.append("get")
        return {"asset": {key: deepcopy(value) for key, value in self.assets[request["assetId"]].items() if key != "content"}}

    def get_asset_content(self, **request):
        self.calls.append("content")
        value = self.assets[request["assetId"]]
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(value["content"]["path"], value["content"]["body"]["text"])
        return {"content": {"zipFile": buffer.getvalue()}, "version": value["version"]}


@pytest.fixture
async def knowledge_worker(test_engine, claimed_knowledge):
    row = claimed_knowledge
    snapshot = row.intent.authorization["knowledge_admission"]["source_snapshots"][0]

    async def stream(*args):
        yield b"original"

    native = Native()
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    worker = KnowledgeSetupWorker(
        sessions, SimpleNamespace(read_source=AsyncMock(return_value=snapshot), stream_source=stream), KnowledgeNativeDriver(native), AsyncMock()
    )
    return row, worker, native


async def test_content_readback_is_distinct_from_native_use_and_has_immutable_effects(db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    receipts = await worker.execute(row.intent, row.claim)
    assert len(receipts) == 1 and receipts[0]["content_written"] is True and receipts[0]["native_use_qualified"] is False
    assert native.calls.count("create") == 1 and "update" not in native.calls
    events = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert [event.phase for event in events] == ["planned", "started", "ready"]
    assert all(event.lease_owner == row.claim.owner and event.fence == row.claim.fence for event in events)
    assert events[1].previous_hash == events[0].event_hash and events[2].previous_hash == events[1].event_hash
    await db_session.refresh(row.operation)
    await db_session.refresh(row.work)
    assert row.operation.state == "waiting" and row.operation.gaps == ["knowledge_native_use_unqualified"]
    assert row.work.completed_at is not None


@pytest.mark.parametrize("recovery", ["present", "absent", "changed"])
async def test_lost_create_ack_never_creates_or_updates_on_recovery(db_session, knowledge_worker, recovery):
    row, worker, native = knowledge_worker
    native.lose_ack = True
    with pytest.raises(TimeoutError):
        await worker.execute(row.intent, row.claim)
    first = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    original_hashes = [event.event_hash for event in first]
    assert [event.phase for event in first] == ["planned", "started", "outcome_unknown"]
    native.lose_ack = False
    if recovery == "absent":
        native.assets.clear()
    elif recovery == "changed":
        native.assets["1"]["metadata"]["status"] = "INACTIVE"
    if recovery == "present":
        await worker.execute(row.intent, row.claim)
    else:
        with pytest.raises(KnowledgeAccessDenied, match="knowledge_native_outcome_unknown"):
            await worker.execute(row.intent, row.claim)
    assert native.calls.count("create") == 1 and "update" not in native.calls
    current = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert [event.event_hash for event in current[:3]] == original_hashes


async def test_stale_worker_cannot_commit_readback_after_native_effect(db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    count = 0

    async def verify(*args):
        nonlocal count
        count += 1
        if native.assets:
            await db_session.refresh(row.work)
            row.work.fence += 1
            await db_session.commit()

    worker.verify = verify
    with pytest.raises(AuthorityRevoked):
        await worker.execute(row.intent, row.claim)
    events = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert [event.phase for event in events] == ["planned", "started"]
    assert native.calls.count("create") == 1
    owned = await db_session.scalar(select(KnowledgeOwnership))
    assert owned.state == "started" and owned.receipt is None


async def test_replaced_worker_reconciles_original_started_effect(db_session, knowledge_worker):
    from dataclasses import replace

    row, worker, native = knowledge_worker
    native.lose_ack = True
    with pytest.raises(TimeoutError):
        await worker.execute(row.intent, row.claim)
    await db_session.refresh(row.work)
    row.work.fence += 1
    row.work.lease_owner = "replacement-owner"
    await db_session.commit()
    new_claim = replace(row.claim, fence=row.work.fence, owner=row.work.lease_owner)
    native.lose_ack = False
    await worker.execute(row.intent, new_claim)
    assert native.calls.count("create") == 1 and "update" not in native.calls
    events = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert events[-1].phase == "ready" and events[-1].lease_owner == new_claim.owner
    assert events[1].lease_owner == row.claim.owner


async def test_tampered_history_blocks_before_native_calls(db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    native.lose_ack = True
    with pytest.raises(TimeoutError):
        await worker.execute(row.intent, row.claim)
    event = await db_session.scalar(select(KnowledgeEffect).where(KnowledgeEffect.sequence == 1))
    event.payload = {**event.payload, "space_id": "another-space"}
    await db_session.commit()
    calls = len(native.calls)
    with pytest.raises(AuthorityRevoked, match="knowledge_effect_history_changed"):
        await worker.execute(row.intent, row.claim)
    assert len(native.calls) == calls


@pytest.mark.parametrize("readback", ["written", "absent", "changed"])
async def test_ambiguous_update_recovery_is_strictly_read_only(db_session, knowledge_worker, readback):
    from l5_services.devops.knowledge_work import current_context, resolve_admitted

    row, worker, native = knowledge_worker
    async with worker.sessions() as session:
        context = await current_context(session, row.intent, row.claim)
    assets = await resolve_admitted(context, row.claim, worker.sources, AsyncMock())
    asset = assets[0]
    driver = worker.driver
    initial = driver.prepare("native-space", asset, "previous-operation", None)
    ownership = driver.mutate(initial, asset)
    db_session.add(
        KnowledgeOwnership(
            tenant_id=row.intent.tenant_id,
            space_id="native-space",
            source_key=initial["source_key"],
            binding_id=row.intent.request["binding_id"],
            operation_id="previous-operation",
            state="ready",
            receipt=ownership,
        )
    )
    await db_session.commit()
    native.lose_ack = True
    with pytest.raises(TimeoutError):
        await worker.execute(row.intent, row.claim)
    events = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert [event.phase for event in events] == ["planned", "started", "outcome_unknown"]
    assert events[0].payload["action"] == "update" and events[0].payload["asset_id"] == ownership["asset_id"]
    if readback == "absent":
        native.assets.clear()
    elif readback == "changed":
        native.assets["1"]["metadata"]["adp_source_hash"] = "f" * 64
    if readback == "written":
        receipts = await worker.execute(row.intent, row.claim)
        assert receipts[0]["version"] == 2
    else:
        with pytest.raises(KnowledgeAccessDenied, match="knowledge_native_outcome_unknown"):
            await worker.execute(row.intent, row.claim)
    assert native.calls.count("create") == 1 and native.calls.count("update") == 1


async def test_unrelated_native_instruction_is_preserved(db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    native.assets["unrelated"] = {
        "assetId": "unrelated",
        "assetType": "agents_md",
        "version": 7,
        "metadata": {"agent_type": "CHAT"},
        "content": {"path": "AGENTS.md", "body": {"text": "unrelated"}},
    }
    original = deepcopy(native.assets)
    with pytest.raises(KnowledgeAccessDenied, match="knowledge_instruction_target_occupied"):
        await worker.execute(row.intent, row.claim)
    assert native.assets == original and "create" not in native.calls and "update" not in native.calls
    assert await db_session.scalar(select(KnowledgeEffect)) is None
