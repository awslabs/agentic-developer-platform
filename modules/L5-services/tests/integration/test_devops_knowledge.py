"""Offline Product-source and native-asset fixtures; no provider calls."""

from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.devops.assets import NativeKnowledgeAssets
from l5_services.devops.knowledge import KnowledgeAccessDenied, resolve_knowledge, synchronize_selected_knowledge
from l5_services.devops.knowledge_sync import KnowledgeSynchronizer
from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.models import DevopsSpaceIntent
from l5_services.models.base import Base


def ref(project, name, revision="v1"):
    return {"tenant_id": "example-tenant", "project_id": project,
            "source_connection_id": "docs", "record_type": "document",
            "record_id": name, "record_revision": revision}


class Product:
    def __init__(self, references):
        self.references = references
        self.pins = []
        self.contents = {}
        self.scopes = []
        self.required_claim = None
        self.read_requests = []
        self.stream_requests = []

    async def list_sources(self, product_id, project_id):
        self.scopes.append((product_id, project_id))
        return {"coverage": "complete", "next_cursor": None, "items": [
            {"connection_ref": {"id": "docs"}, "resources": deepcopy(self.references)}]}

    async def pin_source(self, product_id, project_id, request, *, idempotency_key):
        self.pins.append(deepcopy(request))
        content = self.contents.get(request["source_ref"]["record_revision"], request["source_ref"]["record_id"]).encode()
        return {"snapshot_id": idempotency_key, "source_ref": request["source_ref"],
                "content": {"bytes": len(content), "sha256": sha256(content).hexdigest()}}

    async def read_source(self, product_id, project_id, snapshot_id, request):
        self.read_requests.append(deepcopy(request))
        if self.required_claim is not None and request != self.required_claim:
            raise KnowledgeAccessDenied("stale work claim")
        pin = self.pins[-1]
        content = self.contents.get(pin["source_ref"]["record_revision"], pin["source_ref"]["record_id"]).encode()
        return {"snapshot_id": snapshot_id, "source_ref": pin["source_ref"],
                "content": {"bytes": len(content), "sha256": sha256(content).hexdigest()}}

    async def stream_source(self, product_id, project_id, snapshot_id, request):
        self.stream_requests.append(deepcopy(request))
        if self.required_claim is not None and request != self.required_claim:
            raise KnowledgeAccessDenied("stale work claim")
        reference = self.pins[-1]["source_ref"]
        yield self.contents.get(reference["record_revision"], reference["record_id"]).encode()


class Native:
    def __init__(self):
        self.assets = {}
        self.creates = 0
        self.updates = 0
        self.delete_fails = False

    def list_assets(self, **request):
        return {"items": [self.get_asset(agentSpaceId=request["agentSpaceId"], assetId=identity)["asset"]
                          for identity in self.assets]}

    def create_asset(self, **request):
        self.creates += 1
        identity = str(self.creates)
        self.assets[identity] = {"assetId": identity, "version": 1,
                                 "assetType": request["assetType"], "metadata": request["metadata"],
                                 "content": request["content"]["file"]}
        return {"asset": deepcopy(self.assets[identity])}

    def update_asset(self, **request):
        self.updates += 1
        asset = self.assets[request["assetId"]]
        asset["version"] += 1
        asset["metadata"].update(request["metadata"])
        asset["content"] = request["content"]["file"]
        return {"asset": deepcopy(asset)}

    def delete_asset(self, **request):
        if self.delete_fails:
            raise ConnectionError("native cleanup unavailable")
        del self.assets[request["assetId"]]

    def get_asset(self, **request):
        asset = deepcopy(self.assets[request["assetId"]])
        del asset["content"]
        return {"asset": asset}

    def get_asset_content(self, **request):
        content = self.assets[request["assetId"]]["content"]
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(content["path"], content["body"]["text"])
        return {"content": {"zipFile": buffer.getvalue()},
                "version": self.assets[request["assetId"]]["version"]}


def selection(reference, *, kind="skill"):
    return {"source_ref": reference, "kind": kind, "scope": "agent", "active": True,
            "native_metadata": ({"agent_type": "CHAT"} if kind == "instruction" else
                                {"name": reference["record_id"], "description": "Selected operational steps",
                                 "agent_types": ["CHAT"]})}


async def resolved(product, policy, publications, project="one"):
    return await resolve_knowledge(product, "example-tenant", "example-product", project,
                                   policy, publications, delegation_id="delegation",
                                   delegation_revision=1, operation_id="operation",
                                   attempt_id="attempt", lease_owner="worker-one", lease_fence=1,
                                   retain_until="2026-11-01T00:00:00Z")


@pytest.mark.asyncio
async def test_knowledge_publish(tmp_path):
    first, second, restricted = (ref("one", name) for name in ("runbook-a", "runbook-b", "secret"))
    product = Product([first, second, restricted])
    policy = {"source_refs": [first, second], "visibility": "product_team"}
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    try:
        receipts = await synchronize_selected_knowledge(
            product, synchronizer, scope, "binding-operation", policy,
            [selection(first), selection(second, kind="instruction")],
            delegation_id="delegation", delegation_revision=1, operation_id="publication",
            attempt_id="attempt", lease_owner="worker-one", lease_fence=1, retain_until="2026-11-01T00:00:00Z")
        assert [receipt["source_ref"] for receipt in receipts] == [first, second]
        assert product.scopes == [("example-product", "one")] * 3
        assert len(native.assets) == len(product.pins) == 2
        assert all(receipt["source_hash"] == sha256(native.assets[receipt["asset_id"]]["content"]
                                                    ["body"]["text"].encode()).hexdigest()
                   and receipt["scope"] == "agent" and receipt["active"] is True
                   and receipt["visibility"] == "product_team" and receipt["mapping_version"] == 2
                   for receipt in receipts)
        assert {item["asset_id"] for item in await synchronizer.accessible(
            scope, "binding-operation", product, policy)} == {"1", "2"}
        assert all(native.get_asset(agentSpaceId="space-one", assetId=item["asset_id"])
                   ["asset"]["version"] == item["version"] for item in receipts)
        assert [native.assets[item["asset_id"]]["content"]["body"]["text"]
                for item in receipts] == ["runbook-a", "runbook-b"]
        assert restricted not in [pin["source_ref"] for pin in product.pins]
        assert native.assets["1"]["assetType"] == "skill"
        assert native.assets["1"]["metadata"]["agent_types"] == ["CHAT"]
        assert native.assets["1"]["metadata"]["status"] == "ACTIVE"
        assert native.assets["2"]["assetType"] == "agents_md"
        assert native.assets["2"]["metadata"]["agent_type"] == "CHAT"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility,change", [
    ("only_me", {}),
    ("product_team", {"kind": "context"}),
    ("product_team", {"native_metadata": {}}),
    ("product_team", {"scope": "global"}),
    ("product_team", {"native_metadata": {"name": "valid", "description": "Valid", "agent_types": ["GENERIC", "CHAT"]}}),
    ("product_team", {"native_metadata": {"name": "valid", "description": "Valid", "agent_types": ["UNKNOWN"]}}),
    ("product_team", {"native_metadata": {"name": "valid", "description": "Valid", "agent_types": ["CHAT"], "enable_tools": ["any"]}}),
    ("product_team", {"native_metadata": {"name": "valid", "description": "Valid", "agent_types": ["CHAT"], "skill_type": "LEARNED"}}),
    ("product_team", {"kind": "instruction", "active": False, "native_metadata": {"agent_type": "CHAT"}}),
])
async def test_knowledge_unsupported_native_policy_stops_before_content(visibility, change):
    source = ref("one", "runbook")
    product = Product([source])
    with pytest.raises(KnowledgeAccessDenied):
        await resolved(product, {"source_refs": [source], "visibility": visibility},
                       [{**selection(source), **change}])
    assert product.pins == []


@pytest.mark.asyncio
async def test_skill_activation_uses_provider_status_and_reuses_asset(tmp_path):
    source = ref("one", "runbook")
    product = Product([source])
    policy = {"source_refs": [source], "visibility": "product_team"}
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    try:
        for active in (True, False, True):
            assets = await resolved(product, policy, [{**selection(source), "active": active}])
            await synchronizer.sync(scope, "binding-operation", assets,
                                    operation_id=f"activation-{active}", selected_refs=[source])
            assert native.assets["1"]["metadata"]["status"] == ("ACTIVE" if active else "INACTIVE")
        assert native.creates == 1 and native.updates == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_instruction_publication_preserves_existing_target():
    source = ref("one", "runbook")
    assets = await resolved(Product([source]), {"source_refs": [source], "visibility": "product_team"},
                            [selection(source, kind="instruction")])
    native = Native()
    native.assets["existing"] = {"assetId": "existing", "assetType": "agents_md", "version": 1,
                                  "metadata": {"agent_type": "CHAT"},
                                  "content": {"path": "AGENTS.md", "body": {"text": "retain"}}}
    before = deepcopy(native.assets)
    with pytest.raises(KnowledgeAccessDenied, match="already belongs"):
        NativeKnowledgeAssets(native).publish("space-one", assets, operation_id="publication")
    assert native.assets == before and native.creates == native.updates == 0


@pytest.mark.asyncio
async def test_instruction_source_content_obeys_provider_size_limit():
    source = ref("one", "runbook")
    product = Product([source])
    product.contents["v1"] = "x" * 25_001
    with pytest.raises(KnowledgeAccessDenied, match="too large"):
        await resolved(product, {"source_refs": [source], "visibility": "product_team"},
                       [selection(source, kind="instruction")])


@pytest.mark.asyncio
async def test_native_publication_rechecks_private_visibility_before_provider_calls():
    source = ref("one", "runbook")
    assets = await resolved(Product([source]), {"source_refs": [source], "visibility": "product_team"},
                            [selection(source)])
    class NoProviderCalls:
        def list_assets(self, **kwargs):
            pytest.fail("private knowledge must not reach the native provider")
    with pytest.raises(KnowledgeAccessDenied, match="private_visibility"):
        NativeKnowledgeAssets(NoProviderCalls()).publish(
            "space-one", [replace(assets[0], visibility="only_me")], operation_id="publication")


@pytest.mark.asyncio
async def test_native_metadata_patch_cannot_retain_unselected_tools():
    source = ref("one", "runbook")
    assets = await resolved(Product([source]), {"source_refs": [source], "visibility": "product_team"},
                            [selection(source)])
    native = Native()
    adapter = NativeKnowledgeAssets(native)
    adapter.publish("space-one", assets, operation_id="publication")
    native.assets["1"]["metadata"]["enable_tools"] = ["unselected-tool"]
    with pytest.raises(KnowledgeAccessDenied, match="metadata changed"):
        adapter.publish("space-one", assets, operation_id="refresh")
    assert native.creates == 1 and native.updates == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["restricted", "foreign", "undiscovered"])
async def test_knowledge_rejects_unauthorized_sources(failure):
    selected, restricted = ref("one", "runbook"), ref("one", "secret")
    product = Product([selected, restricted])
    policy = {"source_refs": [selected], "visibility": "product_team"}
    candidate = {"restricted": restricted, "foreign": ref("two", "runbook"),
                 "undiscovered": ref("one", "not-listed")}[failure]
    if failure == "undiscovered":
        policy["source_refs"].append(candidate)
    with pytest.raises(KnowledgeAccessDenied):
        await resolved(product, policy, [selection(candidate)])
    assert not product.pins


@pytest.mark.asyncio
async def test_knowledge_refresh(tmp_path):
    first, sibling, revised = ref("one", "runbook"), ref("one", "other"), ref("one", "runbook", "v2")
    product = Product([first, sibling])
    policy = {"source_refs": [first, sibling], "visibility": "product_team"}
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))

    async def publish(selected, operation):
        return await synchronize_selected_knowledge(
            product, synchronizer, scope, "binding-operation", policy,
            [selection(reference) for reference in selected],
            delegation_id="delegation", delegation_revision=1, operation_id=operation,
            attempt_id="attempt", lease_owner="worker-one", lease_fence=1, retain_until="2026-11-01T00:00:00Z")

    try:
        before = await publish([first, sibling], "first")
        learned = {"assetId": "learned", "version": 4, "assetType": "skill",
                   "metadata": {"provider": "learned"},
                   "content": {"path": "SKILL.md", "body": {"text": "provider memory"}}}
        native.assets["learned"] = deepcopy(learned)
        product.references = [revised, sibling]
        product.contents["v2"] = "revised operational steps"
        policy["source_refs"] = [revised, sibling]
        changed = await publish([revised, sibling], "refresh")
        repeated = await publish([revised, sibling], "refresh-repeat")
        assert [item["asset_id"] for item in before] == [item["asset_id"] for item in changed] == [
            item["asset_id"] for item in repeated]
        assert [item["version"] for item in changed] == [2, 1]
        assert [item["version"] for item in repeated] == [2, 1]
        assert changed[0]["source_ref"] == revised
        assert changed[0]["source_hash"] == sha256(b"revised operational steps").hexdigest()
        assert native.creates == 2 and native.updates == 1
        assert native.assets["learned"] == learned
        assert native.assets[changed[0]["asset_id"]]["content"]["body"]["text"] == "revised operational steps"
        accessible = await synchronizer.accessible(scope, "binding-operation", product, policy)
        assert {item["source_ref"]["record_revision"] for item in accessible} == {"v1", "v2"}
        assert len(product.scopes) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_rejects_revision_rotated_before_refresh(tmp_path):
    source, revised = ref("one", "runbook"), ref("one", "runbook", "v2")

    class ChangingProduct(Product):
        async def list_sources(self, product_id, project_id):
            current = await super().list_sources(product_id, project_id)
            if len(self.scopes) == 3:
                current["items"][0]["resources"] = [revised]
            return current

    product = ChangingProduct([source])
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    try:
        with pytest.raises(KnowledgeAccessDenied, match="changed"):
            await synchronize_selected_knowledge(
                product, KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native)),
                scope, "binding-operation", {"source_refs": [source], "visibility": "product_team"},
                [selection(source)], delegation_id="delegation", delegation_revision=1,
                operation_id="publication", attempt_id="attempt", lease_owner="worker-one", lease_fence=1,
                retain_until="2026-11-01T00:00:00Z")
        assert native.creates == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_refuses_ambiguous_ownership():
    source = ref("one", "runbook")
    product = Product([source])
    assets = await resolved(product, {"source_refs": [source], "visibility": "product_team"}, [selection(source)])
    native = Native()
    adapter = NativeKnowledgeAssets(native)
    adapter.publish("space-one", assets, operation_id="first")
    native.assets["duplicate"] = deepcopy(native.assets["1"])
    native.assets["duplicate"]["assetId"] = "duplicate"
    with pytest.raises(KnowledgeAccessDenied, match="duplicate"):
        adapter.publish("space-one", assets, operation_id="second")
    assert native.creates == 1 and native.updates == 0


async def sync_fixture(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    selection = SpaceSelection("example-tenant", "example-product", "one", "binding-one",
                               "123456789012", "region-one")
    async with sessions() as session:
        session.add(DevopsSpaceIntent(tenant_id=selection.tenant_id, operation_id="binding-operation",
                                     selection=asdict(selection), configuration_revision=1,
                                     intent_hash="a" * 64, state="ready", native_space_id="space-one",
                                     ownership="adp_created", owned_trigger_ids=[], resume_trigger_ids=[],
                                     active_executions=[]))
        await session.commit()
    native = Native()
    return engine, sessions, selection, native


@pytest.mark.asyncio
async def test_durable_knowledge_reconcile_after_lost_publication(tmp_path):
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    source = ref("one", "runbook")
    product = Product([source])
    policy = {"source_refs": [source], "visibility": "product_team"}
    assets = await resolved(product, policy, [selection(source)])
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    async def lost_receipt(*args, **kwargs):
        raise ConnectionError("receipt persistence unavailable")
    synchronizer._record = lost_receipt
    try:
        with pytest.raises(ConnectionError, match="receipt persistence"):
            await synchronizer.sync(scope, "binding-operation", assets,
                                    operation_id="publication", selected_refs=[source])
        assert native.creates == 1
        assert await synchronizer.accessible(scope, "binding-operation", product, policy) == []
        assert (await synchronizer.observations(scope, "binding-operation"))[0]["state"] == "effect_started"
        restarted = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
        receipt = await restarted.sync(scope, "binding-operation", assets,
                                       operation_id="publication", selected_refs=[source])
        assert len(receipt) == 1 and native.creates == 1 and native.updates == 0
        assert (await restarted.accessible(scope, "binding-operation", product, policy))[0]["asset_id"] == "1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_revocation_and_failed_cleanup(tmp_path):
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    source = ref("one", "runbook")
    product = Product([source])
    policy = {"source_refs": [source], "visibility": "product_team"}
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    try:
        assets = await resolved(product, policy, [selection(source)])
        await synchronizer.sync(scope, "binding-operation", assets,
                                operation_id="publication", selected_refs=[source])
        assert len(await synchronizer.accessible(scope, "binding-operation", product, policy)) == 1
        product.references = []
        assert await synchronizer.accessible(scope, "binding-operation", product, policy) == []
        policy["source_refs"] = []
        native.delete_fails = True
        await synchronizer.sync(scope, "binding-operation", [],
                                operation_id="revocation", selected_refs=[])
        assert await synchronizer.accessible(scope, "binding-operation", product, policy) == []
        assert (await synchronizer.observations(scope, "binding-operation")) == [
            {"source_ref": source, "state": "cleanup_failed", "asset_id": "1",
             "asset_version": 1, "error": "native_cleanup_unverified"}]
        assert "1" in native.assets
        native.delete_fails = False
        restarted = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
        await restarted.sync(scope, "binding-operation", [],
                             operation_id="retry", selected_refs=[])
        assert (await restarted.observations(scope, "binding-operation"))[0]["state"] == "revoked"
        assert native.assets == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_revocation_recovers_unrecorded_asset_id(tmp_path):
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    source = ref("one", "runbook")
    product = Product([source])
    assets = await resolved(product, {"source_refs": [source], "visibility": "product_team"}, [selection(source)])
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    async def lost_receipt(*args, **kwargs):
        raise ConnectionError("receipt persistence unavailable")
    synchronizer._record = lost_receipt
    try:
        with pytest.raises(ConnectionError):
            await synchronizer.sync(scope, "binding-operation", assets,
                                    operation_id="publication", selected_refs=[source])
        product.references = []
        restarted = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
        await restarted.sync(scope, "binding-operation", [],
                             operation_id="revocation", selected_refs=[])
        assert native.assets == {}
        assert (await restarted.observations(scope, "binding-operation"))[0]["state"] == "revoked"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_uncertain_write_never_creates_new_asset_on_missing_readback(tmp_path):
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    source = ref("one", "runbook")
    product = Product([source])
    assets = await resolved(product, {"source_refs": [source], "visibility": "product_team"}, [selection(source)])
    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    async def lost_receipt(*args, **kwargs):
        raise ConnectionError("receipt persistence unavailable")
    synchronizer._record = lost_receipt
    try:
        with pytest.raises(ConnectionError):
            await synchronizer.sync(scope, "binding-operation", assets,
                                    operation_id="publication", selected_refs=[source])
        native.assets.clear()
        restarted = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
        with pytest.raises(KnowledgeAccessDenied, match="not reconciled"):
            await restarted.sync(scope, "binding-operation", assets,
                                 operation_id="publication", selected_refs=[source])
        assert native.creates == 1 and native.updates == 0
        assert (await restarted.observations(scope, "binding-operation"))[0]["error"] == "native_outcome_unknown"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_recovery(tmp_path):
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    source, restricted = ref("one", "runbook"), ref("one", "secret")
    product = Product([source, restricted])
    policy = {"source_refs": [source], "visibility": "product_team"}

    async def sync(synchronizer, operation, *, attempt="attempt", owner="worker-one", fence=1):
        product.required_claim = {
            "delegation_id": "delegation", "delegation_revision": 1,
            "operation_id": operation, "attempt_id": attempt,
            "lease_owner": owner, "lease_fence": fence,
        }
        return await synchronize_selected_knowledge(
            product, synchronizer, scope, "binding-operation", policy,
            [selection(source)], delegation_id="delegation", delegation_revision=1,
            operation_id=operation, attempt_id=attempt, lease_owner=owner, lease_fence=fence,
            retain_until="2026-11-01T00:00:00Z")

    synchronizer = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
    async def lost_receipt(*args, **kwargs):
        raise ConnectionError("receipt persistence unavailable")
    synchronizer._record = lost_receipt
    try:
        with pytest.raises(ConnectionError, match="receipt persistence"):
            await sync(synchronizer, "publication")
        assert native.creates == 1
        assert await synchronizer.accessible(scope, "binding-operation", product, policy) == []
        restarted = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
        receipt = await sync(restarted, "publication", attempt="recovery-attempt", owner="worker-two", fence=2)
        assert len(receipt) == 1 and receipt[0]["asset_id"] == "1"
        assert native.creates == 1 and native.updates == 0
        assert product.read_requests == product.stream_requests
        assert [request["lease_owner"] for request in product.read_requests] == ["worker-one", "worker-two"]
        assert [request["attempt_id"] for request in product.read_requests] == ["attempt", "recovery-attempt"]
        assert [request["lease_fence"] for request in product.read_requests] == [1, 2]
        assert {request["operation_id"] for request in product.read_requests} == {"publication"}
        product.references = [restricted]
        native.delete_fails = True
        assert await sync(restarted, "revocation") == []
        assert await restarted.accessible(scope, "binding-operation", product, policy) == []
        assert (await restarted.observations(scope, "binding-operation"))[0]["error"] == "native_cleanup_unverified"
        assert "1" in native.assets
        native.delete_fails = False
        recovered = KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native))
        assert await sync(recovered, "retry") == []
        assert native.assets == {}
        assert (await recovered.observations(scope, "binding-operation"))[0]["state"] == "revoked"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_owner", [None, "", 42, "x" * 256])
@pytest.mark.parametrize("entry_point", ["resolve", "synchronize"])
async def test_knowledge_requires_current_lease_owner_before_source_access(tmp_path, lease_owner, entry_point):
    source = ref("one", "runbook")
    product = Product([source])
    policy = {"source_refs": [source], "visibility": "product_team"}
    claim = {"delegation_id": "delegation", "delegation_revision": 1,
             "operation_id": "publication", "attempt_id": "attempt",
             "lease_owner": lease_owner, "lease_fence": 1, "retain_until": "2026-11-01T00:00:00Z"}
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    try:
        with pytest.raises(KnowledgeAccessDenied):
            if entry_point == "resolve":
                await resolve_knowledge(product, "example-tenant", "example-product", "one",
                                        policy, [selection(source)], **claim)
            else:
                await synchronize_selected_knowledge(
                    product, KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native)),
                    scope, "binding-operation", policy, [selection(source)], **claim)
        assert product.scopes == product.pins == product.read_requests == product.stream_requests == []
        assert native.creates == native.updates == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_rejects_never_authorized_entry_point_source(tmp_path):
    source = ref("one", "secret")
    product = Product([])
    engine, sessions, scope, native = await sync_fixture(tmp_path)
    try:
        with pytest.raises(KnowledgeAccessDenied, match="never published"):
            await synchronize_selected_knowledge(
                product, KnowledgeSynchronizer(sessions, NativeKnowledgeAssets(native)),
                scope, "binding-operation", {"source_refs": [source], "visibility": "product_team"},
                [selection(source)], delegation_id="delegation", delegation_revision=1,
                operation_id="publication", attempt_id="attempt", lease_owner="worker-one", lease_fence=1,
                retain_until="2026-11-01T00:00:00Z")
        assert native.creates == 0 and not product.pins
    finally:
        await engine.dispose()
