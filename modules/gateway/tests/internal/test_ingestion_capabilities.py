"""Adversarial checks for asset-bound cloud capabilities and scheduler identity."""

import hashlib
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from src.internal import ingestion_control_routes as control
from src.internal import ingestion_refresh_routes as refresh
from src.knowledge import ingestion_backends as backends
from src.knowledge import ingestion_storage as storage
from src.knowledge.ingestion_authority import IngestionAuthority


def authority(**kwargs):
    return IngestionAuthority(
        **{
            "asset_id": str(uuid.uuid4()),
            "attempt_id": str(uuid.uuid4()),
            "asset_type": "repo",
            "source_ref": "https://github.com/fixture/service",
            "tenant_id": "tenant-a",
            "owner_sub": None,
            "project_id": None,
            "product_id": None,
            "installation_id": None,
            **kwargs,
        }
    )


@pytest.fixture
def s3(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(storage, "client", lambda _: client)
    monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", "fixture-content")
    monkeypatch.setenv("AGENT_CONTEXT_VECTORS_BUCKET", "fixture-vectors")
    return client


@pytest.mark.parametrize(
    "key",
    [
        "tenants/tenant-b/content/wikis/fixture-service-wiki.md",
        "tenants/tenant-a/content/wikis/fixture-other-wiki.md",
        "tenants/tenant-a/content/repos/fixture/service-other/file",
        "tenants/tenant-a/content/repos/fixture/service/../other/file",
        "tenants/tenant-a/content/repos/fixture/service//file",
        "tenants/tenant-a/content/repos/fixture/service/\\other",
        "content/wikis/fixture-service-wiki.md",
        "secrets/key",
        None,
    ],
)
def test_cross_asset_objects_denied_before_any_cloud_call(s3, key):
    with pytest.raises(HTTPException) as exc:
        storage.object_command(authority(), "get", {"Bucket": "fixture-content", "Key": key})
    assert exc.value.status_code == 403
    assert not s3.mock_calls


def test_exact_object_post_enforces_key_size_type_and_expiry(s3):
    a = authority()
    key = "tenants/tenant-a/wikis/fixture-service-wiki.md"
    storage.object_command(a, "put", {"Bucket": "fixture-content", "Key": key, "ContentLength": 21, "ContentType": "text/markdown"})
    s3.generate_presigned_post.assert_called_once_with(
        Bucket="fixture-content",
        Key=key,
        Fields={"Content-Type": "text/markdown"},
        Conditions=[{"Content-Type": "text/markdown"}, ["content-length-range", 21, 21]],
        ExpiresIn=300,
    )
    for length in [-1, True, storage.MAX_OBJECT_BYTES + 1, "12"]:
        with pytest.raises(HTTPException):
            storage.object_command(a, "put", {"Bucket": "fixture-content", "Key": key, "ContentLength": length})
    with pytest.raises(HTTPException):
        storage.object_command(a, "get", {"Bucket": "other-bucket", "Key": key})


def test_document_source_list_is_read_only_and_output_is_current_attempt(s3):
    a = authority(asset_type="doc", source_ref="s3://fixture-content/tenants/tenant-a/inbox/")
    storage.object_command(a, "get", {"Bucket": "fixture-content", "Key": "tenants/tenant-a/inbox/one.pdf"})
    s3.list_objects_v2.return_value = {"Contents": []}
    storage.object_command(a, "list", {"Bucket": "fixture-content", "Prefix": "tenants/tenant-a/inbox/"})
    for operation, key in [("put", "tenants/tenant-a/inbox/one.pdf"), ("get", "tenants/tenant-a/inbox-other/one.pdf")]:
        with pytest.raises(HTTPException):
            storage.object_command(a, operation, {"Bucket": "fixture-content", "Key": key, "ContentLength": 1})
    own = f"tenants/tenant-a/content/assets/{a.asset_id}/attempts/{a.attempt_id}/doc.md"
    assert a.permits_object(own)
    assert not a.permits_object(own.replace(a.attempt_id, str(uuid.uuid4())))
    assert not a.permits_object(own.replace(a.asset_id, str(uuid.uuid4())))


def test_vector_writes_override_worker_ownership_and_readback_cannot_return_content(s3):
    a = authority()
    key = f"{a.asset_id}:{a.attempt_id}:chunk"
    storage.vector_command(
        a,
        "put",
        {
            "index_name": "tenant-tenant-a",
            "vectors": [
                {
                    "key": key,
                    "embedding": [0.1] * 1024,
                    "metadata": {
                        "tenant_id": "tenant-b",
                        "repo": "victim/repo",
                        "visibility": "shared",
                        "section_heading": "Title",
                        "chunk_text": "hello",
                    },
                }
            ],
        },
    )
    metadata = s3.put_vectors.call_args.kwargs["vectors"][0]["metadata"]
    assert metadata["tenant_id"] == "tenant-a" and metadata["repo"] == "fixture/service"
    assert metadata["asset_id"] == a.asset_id and metadata["visibility"] == "tenant"
    assert metadata["section_heading"] == "Title"
    s3.query_vectors.return_value = {"vectors": [{"key": key, "metadata": {"secret": "never return"}}]}
    assert storage.vector_command(a, "count", {"index_name": "tenant-tenant-a", "query_vector": [0.1] * 1024}) == 1
    query = s3.query_vectors.call_args.kwargs
    assert not query["returnMetadata"] and not query["returnDistance"]
    assert {"asset_id": {"$eq": a.asset_id}} in query["filter"]["$and"]


@pytest.mark.parametrize("mutation", ["index", "key", "dimensions", "nan", "wiki_key"])
def test_vector_namespace_and_payload_boundaries(s3, mutation):
    a = authority()
    v = {"key": f"{a.asset_id}:{a.attempt_id}:chunk", "embedding": [0.1] * 1024, "metadata": {}}
    index = "tenant-tenant-a"
    if mutation == "index":
        index = "tenant-tenant-b"
    if mutation == "key":
        v["key"] = f"{uuid.uuid4()}:{a.attempt_id}:chunk"
    if mutation == "dimensions":
        v["embedding"] = [1]
    if mutation == "nan":
        v["embedding"][0] = float("nan")
    if mutation == "wiki_key":
        v["metadata"]["wiki_s3_key"] = "tenants/tenant-b/secret"
    with pytest.raises(HTTPException):
        storage.vector_command(a, "put", {"index_name": index, "vectors": [v]})
    s3.put_vectors.assert_not_called()


def test_vector_clear_can_only_remove_own_attempt_and_missing_index_is_empty(s3):
    a = authority()
    own = f"{a.asset_id}:{a.attempt_id}:chunk"
    s3.list_vectors.side_effect = [
        {"vectors": [{"key": own}, {"key": f"{a.asset_id}:{uuid.uuid4()}:old"}], "nextToken": "page-2"},
        {"vectors": [{"key": "another-asset:chunk"}]},
    ]
    storage.vector_command(a, "clear", {"index_name": "tenant-tenant-a"})
    assert s3.delete_vectors.call_args.kwargs["keys"] == [own]
    s3.list_vectors.side_effect = ClientError({"Error": {"Code": "NotFoundException"}}, "ListVectors")
    storage.vector_command(a, "clear", {"index_name": "tenant-tenant-a"})
    assert s3.delete_vectors.call_count == 1


def test_state_keys_ignore_worker_selected_source_and_owner(monkeypatch):
    a = authority()
    cloud = MagicMock()
    monkeypatch.setattr(backends.boto3, "resource", lambda *a, **kw: cloud)
    monkeypatch.setenv("INGESTION_STATE_TABLE", "fixture-state")
    backends.state_command(a, "state", {"status": "complete", "source": "victim/repo", "tenant_id": "victim"})
    item = cloud.Table.return_value.put_item.call_args.kwargs["Item"]
    assert item["source"] == f"repo#{a.asset_id}"
    assert "victim" not in str(item)


def test_graph_cannot_select_foreign_vertices_or_inject_query(monkeypatch):
    a = authority()
    client = MagicMock()
    monkeypatch.setattr(backends.boto3, "client", lambda *a, **kw: client)
    monkeypatch.setenv("INGESTION_NEPTUNE_ENDPOINT", "fixture.invalid")
    malicious = "x');g.V().drop();//"
    backends.graph_command(a, "put", {"entities": [{"entity_id": "victim:id", "name": malicious, "repo": "victim", "line": 4}], "relationships": []})
    request = client.execute_open_cypher_query.call_args.kwargs
    assert malicious not in request["openCypherQuery"]
    node = json.loads(request["parameters"])["nodes"][0]
    assert node["id"] == a.asset_id + ":" + hashlib.sha256(b"victim:id").hexdigest()
    assert node["properties"]["name"] == malicious
    assert node["properties"]["repo"] == "fixture/service" and node["properties"]["line"] == 4
    client.reset_mock()
    with pytest.raises(HTTPException):
        backends.graph_command(a, "put", {"entities": [{"entity_id": "valid"}], "relationships": [{"from": "a", "to": "b", "type": malicious}]})
    client.execute_open_cypher_query.assert_not_called()
    backends.graph_command(a, "clear", {})
    assert json.loads(client.execute_open_cypher_query.call_args.kwargs["parameters"]) == {"asset": a.asset_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "issuer", "audience", "subject", "expired", "unsigned"])
async def test_refresh_requires_exact_kubernetes_identity(monkeypatch, mutation):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer, subject = "https://issuer.invalid", "system:serviceaccount:fixture:ingestion-refresh"
    monkeypatch.setenv("INGESTION_REFRESH_ISSUER", issuer)
    monkeypatch.setenv("INGESTION_REFRESH_SUBJECT", subject)
    monkeypatch.setattr(refresh, "_keys", lambda _: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())))
    claims = {"iss": issuer, "sub": subject, "aud": "adp-ingestion-refresh", "iat": int(time.time()) - 10, "exp": int(time.time()) + 60}
    if mutation == "issuer":
        claims["iss"] = "https://other.invalid"
    if mutation == "subject":
        claims["sub"] = "system:serviceaccount:fixture:default"
    if mutation == "audience":
        claims["aud"] = "sts.amazonaws.com"
    if mutation == "expired":
        claims["exp"] = int(time.time()) - 2
    token = jwt.encode(claims, key, algorithm="RS256") if mutation != "unsigned" else jwt.encode(claims, None, algorithm="none")
    if mutation:
        with pytest.raises(HTTPException) as exc:
            await refresh.verify_refresh_identity("Bearer " + token)
        assert exc.value.status_code == 403
    else:
        await refresh.verify_refresh_identity("Bearer " + token)


@pytest.mark.asyncio
async def test_github_worker_token_is_restricted_by_immutable_repo_id(monkeypatch):
    a = authority(owner_sub="user-a", installation_id=123)
    monkeypatch.setattr(control, "admit_source", AsyncMock())
    monkeypatch.setattr(control, "_get_ops_app_credentials", AsyncMock(return_value=("app", "key")))
    monkeypatch.setattr(control, "mint_installation_token", AsyncMock(return_value="broad-server-only"))
    mint = AsyncMock(return_value=("one-repo-readonly", None))
    monkeypatch.setattr(control, "mint_installation_token_with_expiry", mint)
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[
            httpx.Response(200, json={"full_name": "fixture/service", "id": 456}, request=httpx.Request("GET", "https://fixture.invalid")),
            httpx.Response(200, json={"full_name": "other/service", "id": 789}, request=httpx.Request("GET", "https://fixture.invalid")),
        ]
    )
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=client)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(control.httpx, "AsyncClient", lambda **kw: manager)
    result = await control.prepare_source(AsyncMock(), a)
    assert result["github_token"] == "one-repo-readonly"
    assert "broad-server-only" not in str(result)
    assert mint.call_args.kwargs == {"repository_ids": [456], "permissions": {"contents": "read", "metadata": "read"}}
    mint.reset_mock()
    with pytest.raises(HTTPException):
        await control.prepare_source(AsyncMock(), a)
    mint.assert_not_called()


def test_vector_prepare_removes_only_prior_generations_of_own_asset(s3):
    a = authority()
    own = f"{a.asset_id}:{a.attempt_id}:chunk"
    old = f"{a.asset_id}:{uuid.uuid4()}:old"
    s3.list_vectors.return_value = {"vectors": [{"key": own}, {"key": old}, {"key": "another-asset:chunk"}]}
    storage.vector_command(a, "prepare", {"index_name": "tenant-tenant-a"})
    assert s3.delete_vectors.call_args.kwargs["keys"] == [old]
