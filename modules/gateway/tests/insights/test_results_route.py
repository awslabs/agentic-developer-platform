"""Gateway deployment path authenticates before reading an Insights snapshot."""

import asyncio
import copy
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from contracts.insights import results
from src.insights_results import routes
from src.products.connector_policy import derive_current_envelope, descriptor_digest
from src.products.models import ConnectorCell, ConnectorEvidenceDependency, Product, ProductChange, ProductProjectBinding, Project
from tests.products import test_connector_policy, test_i07_api

member = test_i07_api.member


TIME = "2026-10-19T12:00:00Z"
PROJECT = "example-project"


def reference(tenant, kind, identifier):
    return {
        "tenant_id": tenant,
        "project_id": PROJECT,
        "source_connection_id": "example-source",
        "record_type": kind,
        "record_id": identifier,
        "record_revision": "example-revision-1",
    }


def snapshot(tenant):
    work = reference(tenant, "work", "example-work")
    cost = reference(tenant, "cost", "example-cost")
    product = reference(tenant, "product", "example-product")
    portfolio = reference(tenant, "portfolio", "example-portfolio")
    links = []
    for fact in (work, cost):
        links.append(
            {
                "relationship_type": "work_product",
                "relationship_id": fact["record_id"] + "-link",
                "relationship_revision": "example-revision-1",
                "source_connection_id": "example-source",
                "from_ref": fact,
                "to_ref": product,
                "valid_from": "2026-10-18T00:00:00Z",
                "recorded_at": TIME,
                "mapping_version": "example-map-1",
            }
        )
    links.append(
        {
            "relationship_type": "portfolio_product",
            "relationship_id": "example-membership",
            "relationship_revision": "example-revision-1",
            "source_connection_id": "example-source",
            "from_ref": portfolio,
            "to_ref": product,
            "valid_from": "2026-10-18T00:00:00Z",
            "recorded_at": TIME,
            "mapping_version": "example-map-1",
        }
    )
    return {
        "status": "complete",
        "dataset_version": "example-dataset-1",
        "mapping_versions": ["example-map-1"],
        "facts": [
            {"subject_ref": work, "kind": "work", "effective_at": TIME, "recorded_at": TIME},
            {"subject_ref": cost, "kind": "cost", "amount": "10.00", "currency": "USD", "effective_at": TIME, "recorded_at": TIME},
        ],
        "relationships": links,
        "allocations": [],
    }


def shared_policy(snapshot, tenant):
    references = {key: dict(zip(results.projection.REFERENCE_FIELDS, key)) for key in results.snapshot_references(snapshot)}
    dependencies = list(references.values())
    return {
        "schema_version": "results-policy/1.0",
        "tenant_id": tenant,
        "project_id": PROJECT,
        "dataset_version": snapshot["dataset_version"],
        "snapshot_sha256": results.snapshot_digest(snapshot),
        "policy_epoch": 1,
        "envelopes": [
            {
                "envelope_version": 1,
                "identity": dependencies[0],
                "realm_id": "example-realm",
                "realm_revision": 1,
                "descriptor_digest": "a" * 64,
                "audience": "shared",
                "dependency_identities": dependencies,
                "dependency_versions": [{"identity": identity, "revision": 1, "metadata_digest": "b" * 64} for identity in dependencies],
                "required_policies": [
                    {"ref": name, "revision": 1, "visibility": "shared", "audience_expression": "current:project-readers"}
                    for name in ("source-policy", "evidence-policy")
                ],
            }
        ],
    }


def test_fixed_key_storage_reads_are_bounded_and_fail_closed(monkeypatch):
    monkeypatch.setenv("BG_INSIGHTS_RESULTS_BUCKET", "example-insights-bucket")
    client = Mock()
    monkeypatch.setattr(routes.boto3, "client", lambda service: client)
    with pytest.raises(results.ReadError) as invalid:
        routes.read_snapshot("..", PROJECT)
    assert invalid.value.status == 409
    assert routes.read_policy("example-tenant", "../foreign") is None
    client.get_object.assert_not_called()

    for reader, limit, suffix in (
        (routes.read_snapshot, routes.SNAPSHOT_LIMIT, "published.json"),
        (routes.read_policy, routes.SNAPSHOT_LIMIT, "published.json"),
    ):
        client.get_object.return_value = {"Body": io.BytesIO(b"x" * (limit + 1))}
        client.get_object.side_effect = None
        with pytest.raises(results.ReadError) as oversized:
            reader("example-tenant", PROJECT)
        assert (oversized.value.status, oversized.value.code) == (409, "projection_incomplete")
        client.get_object.assert_called_with(Bucket="example-insights-bucket", Key=f"results/v1/example-tenant/{PROJECT}/{suffix}")
        client.get_object.side_effect = OSError("storage read unavailable")
        with pytest.raises(results.ReadError) as outage:
            reader("example-tenant", PROJECT)
        assert outage.value.status == 409

    client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    assert routes.read_policy("example-tenant", PROJECT) is None


@pytest.mark.asyncio
async def test_deployable_route_uses_current_project_scope_and_read_only_provider(test_client, db_session, member, monkeypatch):
    db_session.add(Project(id=PROJECT, tenant_id=member.org_id, name="Example", owner_user_id=member.user_id))
    await db_session.commit()
    monkeypatch.setenv("BG_INSIGHTS_RESULTS_BUCKET", "example-insights-bucket")
    client = Mock()
    data = snapshot(member.org_id)
    policy = shared_policy(data, member.org_id)
    calls = []

    def get_object(**params):
        calls.append(params["Key"])
        document = policy if params["Key"].endswith("policy.json") else data
        return {"Body": io.BytesIO(json.dumps(document).encode())}

    client.get_object.side_effect = get_object
    monkeypatch.setattr(routes.boto3, "client", lambda service: client)
    authority = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "current_policy_access", authority)
    base = f"/engineering-insights/v1/projects/{PROJECT}/results"
    params = {"view": "business_context", "product_id": "example-product", "as_of": TIME}
    response = await test_client.get(base, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["work_count"] == 1
    assert body["cost_by_currency"] == {"USD": "10.00"}
    assert body["dataset_version"] == "example-dataset-1"
    assert {row["subject_ref"]["record_id"] for row in body["associations"]} == {"example-work", "example-cost"}
    assert {row["portfolio_ids"][0] for row in body["contexts"]} == {"example-portfolio"}
    assert calls == [
        f"results/v1/{member.org_id}/{PROJECT}/{suffix}"
        for suffix in ("published.json", "policy.json", "published.json", "published.json", "policy.json")
    ]
    denied = await test_client.get(base.replace(PROJECT, "foreign-project"), params=params)
    assert denied.status_code == 404
    assert denied.json() == {"error": {"code": "not_found", "message": "Result unavailable"}}
    assert client.get_object.call_count == 5
    authority.return_value = False
    denied_source = await test_client.get(base, params=params)
    assert denied_source.status_code == 404
    assert denied_source.json() == denied.json()
    assert client.get_object.call_count == 7
    authority.return_value = True
    project = await db_session.get(Project, (member.org_id, PROJECT))
    project.owner_user_id = "revoked-owner"
    await db_session.commit()
    revoked = await test_client.get(base, params=params)
    assert revoked.status_code == 404
    assert revoked.json() == denied.json()
    assert client.get_object.call_count == 7


@pytest.mark.asyncio
async def test_reader_denies_missing_stale_and_foreign_policy_without_snapshot(test_client, db_session, member, monkeypatch):
    db_session.add(Project(id=PROJECT, tenant_id=member.org_id, name="Example", owner_user_id=member.user_id))
    await db_session.commit()
    monkeypatch.setenv("BG_INSIGHTS_RESULTS_BUCKET", "example-insights-bucket")
    data = snapshot(member.org_id)
    policy = shared_policy(data, member.org_id)
    requested = []
    missing = False
    client = Mock()

    def get_object(**params):
        requested.append(params["Key"])
        if params["Key"].endswith("policy.json"):
            if missing:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            document = policy
        else:
            document = data
        return {"Body": io.BytesIO(json.dumps(document).encode())}

    client.get_object.side_effect = get_object
    monkeypatch.setattr(routes.boto3, "client", lambda service: client)
    authority = AsyncMock(side_effect=lambda db, actor, project, descriptor: descriptor["policy_epoch"] == 1)
    monkeypatch.setattr(routes, "current_policy_access", authority)
    base = f"/engineering-insights/v1/projects/{PROJECT}/results"
    params = {"view": "business_context", "product_id": "example-product", "as_of": TIME}
    foreign = await test_client.get(base.replace(PROJECT, "other-project"), params=params)
    assert foreign.status_code == 404
    assert requested == []
    injected = await test_client.get(base, params={**params, "path": "results/v1/foreign/published.json"})
    assert injected.status_code == 400
    assert not any("foreign" in key for key in requested)

    missing = True
    requested.clear()
    absent = await test_client.get(base, params=params)
    assert (absent.status_code, absent.json()["error"]["code"]) == (409, "projection_incomplete")
    assert requested and all(key.endswith(("policy.json", "published.json")) for key in requested)
    missing = False
    policy["policy_epoch"] += 1
    requested.clear()
    stale = await test_client.get(base, params=params)
    assert stale.status_code == 404
    assert requested and all(key.endswith(("policy.json", "published.json")) for key in requested)
    policy["policy_epoch"] -= 1
    data["facts"][1]["amount"] = "999.00"
    tampered = await test_client.get(base, params=params)
    assert tampered.status_code == 409
    assert "999.00" not in tampered.text
    assert tampered.headers["Cache-Control"] == "no-store"
    assert not any("foreign" in key for key in requested)
    assert all(f"/{member.org_id}/{PROJECT}/" in key for key in requested)
    authority.side_effect = OSError("current authority unavailable")
    requested.clear()
    outage = await test_client.get(base, params=params)
    assert (outage.status_code, outage.json()["error"]["code"]) == (409, "projection_incomplete")
    assert requested and all(key.endswith(("policy.json", "published.json")) for key in requested)
    assert "example-work" not in outage.text


@pytest.mark.parametrize("barrier", ["snapshot", "final_policy"])
@pytest.mark.parametrize("revoked_scope", ["source", "project", "policy"])
@pytest.mark.asyncio
async def test_revocation_while_snapshot_is_loading_releases_no_result(test_client, db_session, member, monkeypatch, revoked_scope, barrier):
    db_session.add(Project(id=PROJECT, tenant_id=member.org_id, name="Example", owner_user_id=member.user_id))
    await db_session.commit()
    monkeypatch.setenv("BG_INSIGHTS_RESULTS_BUCKET", "example-insights-bucket")
    data = snapshot(member.org_id)
    policy = shared_policy(data, member.org_id)
    loaded = threading.Event()
    release = threading.Event()
    block_load = True
    policy_reads = 0
    pointer_reads = 0
    snapshot_reads = []
    client = Mock()

    def get_object(**params):
        nonlocal block_load, policy_reads, pointer_reads
        if params["Key"].endswith("published.json"):
            pointer_reads += 1
        if params["Key"].endswith("policy.json"):
            policy_reads += 1
            document = policy
        else:
            snapshot_reads.append(params["Key"])
            document = data
        if block_load and ((barrier == "snapshot" and pointer_reads == 2) or (barrier == "final_policy" and policy_reads == 2)):
            loaded.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test read barrier timed out")
            block_load = False
        return {"Body": io.BytesIO(json.dumps(document).encode())}

    client.get_object.side_effect = get_object
    monkeypatch.setattr(routes.boto3, "client", lambda service: client)
    grants = {"source": True}
    authority = AsyncMock(side_effect=lambda *args: grants["source"])
    monkeypatch.setattr(routes, "current_policy_access", authority)
    base = f"/engineering-insights/v1/projects/{PROJECT}/results"
    params = {"view": "business_context", "product_id": "example-product", "as_of": TIME, "limit": 1}
    pending = asyncio.create_task(test_client.get(base, params=params))
    try:
        assert await asyncio.to_thread(loaded.wait, 5)
        if revoked_scope == "project":
            project = await db_session.get(Project, (member.org_id, PROJECT))
            project.owner_user_id = "revoked-owner"
            await db_session.commit()
        elif revoked_scope == "policy":
            policy["policy_epoch"] += 1
        else:
            grants["source"] = False
    finally:
        release.set()
    revoked = await asyncio.wait_for(pending, timeout=10)
    assert revoked.status_code == 404
    assert revoked.json() == {"error": {"code": "not_found", "message": "Result unavailable"}}
    assert "example-work" not in revoked.text
    assert "example-cost" not in revoked.text
    assert "work_count" not in revoked.text
    assert revoked.headers["Cache-Control"] == "no-store"
    assert len(snapshot_reads) == 3
    if revoked_scope == "project":
        project.owner_user_id = member.user_id
        await db_session.commit()
    elif revoked_scope == "policy":
        policy["policy_epoch"] -= 1
    else:
        assert authority.await_count == 2
    grants["source"] = True
    first = await test_client.get(base, params=params)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor
    grants["source"] = False
    denied_page = await test_client.get(base, params={**params, "cursor": cursor})
    assert denied_page.status_code == 404
    assert denied_page.json() == revoked.json()
    assert len(snapshot_reads) == 7
    grants["source"] = True
    second = await test_client.get(base, params={**params, "cursor": cursor})
    assert second.status_code == 200
    assert len(second.json()["contexts"]) == 1


@pytest.mark.asyncio
async def test_source_and_evidence_policy_require_current_dependency_and_connection(monkeypatch):
    tenant = "example-tenant"
    realm = test_connector_policy.realm()
    realm.tenant_id = tenant
    realm.project_id = PROJECT
    realm.descriptor["audience_expression"] = "current:project-readers"
    for requirement in realm.descriptor["policies"]:
        if requirement["visibility"] == "shared":
            requirement["audience_expression"] = "current:project-readers"
    realm.descriptor_digest = descriptor_digest(realm.descriptor)
    data = snapshot(tenant)
    identities = [dict(zip(results.projection.REFERENCE_FIELDS, key)) for key in results.snapshot_references(data)]
    unique = {descriptor_digest(identity): identity for identity in identities}
    rows = [
        SimpleNamespace(identity=identity, identity_hash=digest, revision=1, state="active", policy_refs=[], depends_on=[])
        for digest, identity in unique.items()
    ]
    latest = {row.identity_hash: row for row in rows}
    envelope = derive_current_envelope(realm, identities[0], latest, "shared")
    policy = shared_policy(data, tenant)
    policy["envelopes"] = [envelope]
    cell = SimpleNamespace(
        id="example-cell",
        realm_id=realm.id,
        realm_revision=realm.revision,
        routing_revision=policy["policy_epoch"],
    )
    binding = SimpleNamespace(
        id="example-binding",
        project_id=PROJECT,
        generation=1,
        revision=1,
        kind="repository",
        native_id="example-repository",
        status="healthy",
        installation_id="example-source",
        credential_id=None,
        unlinked_at=None,
        desired={},
        observed={"observed_at": "2020-01-01T00:00:00Z", "stale_after": "2099-01-01T00:00:00Z"},
    )
    bindings = [binding]
    db = Mock()
    db.get = AsyncMock(return_value=realm)
    db.scalars = AsyncMock(
        side_effect=lambda query: SimpleNamespace(
            all=lambda: {
                "ConnectorCell": [cell],
                "ConnectorEvidenceDependency": rows,
                "ProductSourceBinding": bindings,
            }[query.column_descriptions[0]["entity"].__name__]
        )
    )
    source_access = AsyncMock(return_value=binding)
    monkeypatch.setattr(routes, "connection_access", source_access)
    actor = SimpleNamespace(org_id=tenant, account_type="human")
    assert await routes.current_policy_access(db, actor, PROJECT, policy)
    assert source_access.await_count == 1
    second_binding = copy.deepcopy(binding)
    second_binding.id = "example-second-binding"
    second_binding.native_id = "example-second-repository"
    bindings.append(second_binding)
    assert await routes.current_policy_access(db, actor, PROJECT, policy)
    for required_binding in bindings:
        required_binding.status = "revoked"
        assert not await routes.current_policy_access(db, actor, PROJECT, policy)
        required_binding.status = "healthy"
        source_access.side_effect = lambda db, actor, candidate: None if candidate is required_binding else candidate
        assert not await routes.current_policy_access(db, actor, PROJECT, policy)
    source_access.side_effect = None
    selections = [reference(tenant, item.kind, item.native_id) for item in bindings]
    rows[0].depends_on = selections
    for identity in selections:
        row = SimpleNamespace(identity=identity, identity_hash=descriptor_digest(identity), revision=1, state="active", policy_refs=[], depends_on=[])
        rows.append(row)
        latest[row.identity_hash] = row
    policy["envelopes"] = [derive_current_envelope(realm, identities[0], latest, "shared")]
    unrelated_binding = copy.deepcopy(binding)
    unrelated_binding.native_id = "unrelated-repository"
    unrelated_binding.status = "revoked"
    bindings.append(unrelated_binding)
    assert await routes.current_policy_access(db, actor, PROJECT, policy)
    second_binding.status = "revoked"
    assert not await routes.current_policy_access(db, actor, PROJECT, policy)
    second_binding.status = "healthy"
    bindings.remove(second_binding)
    assert not await routes.current_policy_access(db, actor, PROJECT, policy)
    bindings.append(second_binding)
    realm.descriptor["audience_expression"] = "current:team-readers"
    realm.descriptor_digest = descriptor_digest(realm.descriptor)
    assert not await routes.current_policy_access(db, actor, PROJECT, policy)
    realm.descriptor["audience_expression"] = "current:project-readers"
    realm.descriptor_digest = descriptor_digest(realm.descriptor)

    source_access.return_value = None
    assert not await routes.current_policy_access(db, actor, PROJECT, policy)
    source_access.return_value = binding
    rows[0].state = "revoked"
    assert not await routes.current_policy_access(db, actor, PROJECT, policy)
    rows[0].state = "active"
    realm.descriptor["policies"][1]["visibility"] = "private"
    assert not await routes.current_policy_access(db, actor, PROJECT, policy)


@pytest.mark.asyncio
async def test_native_profile_policy_uses_current_owning_product_scope(test_client, db_session, member, monkeypatch):
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    created = await test_client.post("/v1/products", json=test_i07_api.REQUEST, headers=test_i07_api.HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.project_id == project_id))
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id))
    realm = test_connector_policy.realm()
    realm.tenant_id, realm.project_id = member.org_id, project_id
    realm.descriptor["audience_expression"] = "current:project-readers"
    for requirement in realm.descriptor["policies"]:
        if requirement["visibility"] == "shared":
            requirement["audience_expression"] = "current:project-readers"
    realm.descriptor_digest = descriptor_digest(realm.descriptor)
    db_session.add(realm)
    cell = ConnectorCell(
        tenant_id=member.org_id,
        project_id=project_id,
        id="example-cell",
        realm_id=realm.id,
        realm_revision=realm.revision,
        database_ref="example-db",
        role_ref="example-role",
        object_prefix="example/",
        workload_id="example-reader",
        audience="shared",
        routing_revision=1,
    )
    db_session.add(cell)
    rows = []
    for kind, identifier, revision in (
        ("product", product_id, change.id),
        ("project", project_id, change.id),
        ("relationship", binding.id, str(binding.relationship_revision)),
        ("change", change.id, str(change.revision)),
    ):
        identity = {
            **reference(member.org_id, kind, identifier),
            "project_id": project_id,
            "source_connection_id": "adp.profile",
            "record_revision": revision,
        }
        row = ConnectorEvidenceDependency(
            tenant_id=member.org_id,
            project_id=project_id,
            cell_id=cell.id,
            identity_hash=descriptor_digest(identity),
            identity=identity,
            revision=1,
            policy_refs=[],
            depends_on=[],
            state="active",
        )
        rows.append(row)
        db_session.add(row)
    await db_session.commit()
    latest = {row.identity_hash: row for row in rows}
    policy = shared_policy(snapshot(member.org_id), member.org_id)
    policy["project_id"] = project_id
    policy["envelopes"] = [derive_current_envelope(realm, row.identity, latest, "shared") for row in rows]
    source_access = AsyncMock()
    monkeypatch.setattr(routes, "connection_access", source_access)
    assert await routes.current_policy_access(db_session, member, project_id, policy)
    source_access.assert_not_awaited()

    product = await db_session.get(Product, (member.org_id, product_id))
    product.team_id = "another-team"
    await db_session.commit()
    assert not await routes.current_policy_access(db_session, member, project_id, policy)
    product.team_id = member.team_id
    await db_session.commit()
    assert await routes.current_policy_access(db_session, member, project_id, policy)
    rows[0].state = "revoked"
    await db_session.commit()
    assert not await routes.current_policy_access(db_session, member, project_id, policy)
    rows[0].state = "active"
    await db_session.commit()
    for kind, identifier in (("product", "foreign-product"), ("project", "foreign-project"), ("unknown", "example-record")):
        foreign_identity = {**rows[0].identity, "record_type": kind, "record_id": identifier}
        rows[0].identity = foreign_identity
        rows[0].identity_hash = descriptor_digest(foreign_identity)
        await db_session.commit()
        latest = {row.identity_hash: row for row in rows}
        policy["envelopes"] = [derive_current_envelope(realm, row.identity, latest, "shared") for row in rows]
        assert not await routes.current_policy_access(db_session, member, project_id, policy)


def test_gateway_image_stages_canonical_insights_read_code():
    gateway = Path(__file__).resolve().parents[2]
    owned = gateway.parent / "insights"
    for source in ("results.py", "projection.py"):
        assert (gateway / "contracts" / "insights" / source).read_bytes() == (owned / source).read_bytes()
    assert "from contracts.insights import results" in (gateway / "Dockerfile").read_text()
    policy = (gateway / "infra" / "gateway-role-policies.tf").read_text()
    assert "InsightsResultsRead" in policy and "InsightsResultsDecrypt" in policy


@pytest.mark.parametrize("selection_as_of", [None, TIME])
@pytest.mark.asyncio
async def test_versioned_pair_reaches_authenticated_results_route(test_client, db_session, member, monkeypatch, selection_as_of):
    import hashlib

    db_session.add(Project(id=PROJECT, tenant_id=member.org_id, name="Example", owner_user_id=member.user_id))
    await db_session.commit()
    monkeypatch.setenv("BG_INSIGHTS_RESULTS_BUCKET", "example-insights-bucket")
    data = snapshot(member.org_id)
    if selection_as_of is not None:
        data["selection_as_of"] = selection_as_of
    policy = shared_policy(data, member.org_id)

    def body(document):
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    snapshot_body, policy_body = body(data), body(policy)
    snapshot_hash, policy_hash = (hashlib.sha256(document).hexdigest() for document in (snapshot_body, policy_body))
    prefix = f"results/v1/{member.org_id}/{PROJECT}/"
    pointer = {
        "schema_version": "results-publication/1.0",
        "dataset_version": "example-dataset-1",
        "snapshot_sha256": snapshot_hash,
        "policy_sha256": policy_hash,
        "quality_sha256": "a" * 64,
    }
    objects = {
        prefix + "published.json": body(pointer),
        prefix + f"datasets/example-dataset-1/snapshots/{snapshot_hash}.json": snapshot_body,
        prefix + f"datasets/example-dataset-1/policies/{policy_hash}.json": policy_body,
    }
    client = Mock()

    def get_object(**params):
        if params["Key"] not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(objects[params["Key"]])}

    client.get_object.side_effect = get_object
    monkeypatch.setattr(routes.boto3, "client", lambda service: client)
    monkeypatch.setattr(routes, "current_policy_access", AsyncMock(return_value=True))
    endpoint = f"/engineering-insights/v1/projects/{PROJECT}/results"
    params = {"view": "business_context", "product_id": "example-product", "as_of": TIME}
    response = await test_client.get(endpoint, params=params)
    assert response.status_code == 200, response.text
    assert (response.json()["dataset_version"], response.json()["cost_by_currency"]) == ("example-dataset-1", {"USD": "10.00"})
    if selection_as_of is not None:
        for cutoff in ("2026-10-18T12:00:00Z", "2026-10-20T12:00:00Z"):
            unavailable = await test_client.get(endpoint, params={**params, "as_of": cutoff})
            assert unavailable.status_code == 409
            assert unavailable.json() == {"error": {"code": "projection_incomplete", "message": "Result unavailable"}}
    objects[prefix + f"datasets/example-dataset-1/snapshots/{snapshot_hash}.json"] = body({**data, "status": "pending"})
    denied = await test_client.get(endpoint, params=params)
    assert denied.status_code == 409
    assert "example-cost" not in denied.text
