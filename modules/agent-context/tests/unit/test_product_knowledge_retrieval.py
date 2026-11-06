"""Product membership and registration provenance never become tenant-wide ACLs."""

from unittest.mock import AsyncMock, Mock

import pytest

from door import server
from door.acl import CallerPrincipal, SearchHit, filter_results
from door.browse_backend import RepositoryNameIndex, _repo_for_content_key
from personal_context.backends.s3_vectors_backend import S3VectorsCodeStore

PRODUCT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
ATTEMPT = "00000000-0000-0000-0000-000000000003"


def product_hit(asset="asset-a", *, product=PRODUCT, tenant="tenant", repo="org/same-source"):
    return SearchHit(
        repo,
        {
            "repo": repo,
            "visibility": "product",
            "product_id": product,
            "tenant_id": tenant,
            "asset_id": asset,
            "ingestion_attempt_id": ATTEMPT,
            "chunk_text": asset,
        },
    )


def principal(**overrides):
    return CallerPrincipal(
        **{
            "owner_sub": "member",
            "tenant_id": "tenant",
            "run_bound": True,
            "product_id": PRODUCT,
            **overrides,
        }
    )


def test_member_without_github_identity_reads_only_active_product_registrations():
    acl = Mock()
    acl.get_allowed_product_assets.return_value = {"asset-a": ATTEMPT}
    hits = [
        product_hit(),
        product_hit("removed"),
        product_hit(product=OTHER),
        product_hit(tenant="other"),
        SearchHit("org/same-source", {"chunk_text": "unscoped"}),
        product_hit(repo=""),
    ]
    assert filter_results(hits, principal(), acl) == [hits[0], hits[5]]
    acl.get_allowed_product_assets.assert_called_once_with(principal(), {"asset-a", "removed"})
    acl.get_allowed_repos.assert_not_called()


@pytest.mark.parametrize("caller", [principal(product_id=""), principal(run_bound=False), None])
def test_product_data_cannot_inherit_repo_or_tenant_access(caller):
    acl = Mock(get_allowed_repos=Mock(return_value={"org/same-source"}))
    assert filter_results([product_hit()], caller, acl) == []


def test_registry_failure_and_incomplete_provenance_fail_closed():
    acl = Mock(get_allowed_product_assets=Mock(side_effect=ConnectionError()))
    assert filter_results([product_hit()], principal(), acl) == []
    acl.get_allowed_product_assets.side_effect = None
    acl.get_allowed_product_assets.return_value = {"asset-a": ATTEMPT}
    for field in ("product_id", "tenant_id", "asset_id", "visibility", "ingestion_attempt_id"):
        hit = product_hit()
        del hit.data[field]
        assert filter_results([hit], principal(), acl) == []


def test_product_hits_cannot_use_shared_platform_path_exception(monkeypatch):
    acl = Mock(get_allowed_repos=Mock(return_value={"org/same-source"}))
    monkeypatch.setattr(server.state, "acl_store", acl)
    hit = product_hit(repo="")
    hit.data["path"] = "content/skills/safe.md"
    assert server._apply_acl([hit], principal(product_id="")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("browse", {"uri": "tenants/tenant/products/secret"}),
        ("understand", {"target": "org/same-source"}),
        ("impact", {"target": "org/same-source"}),
        ("secure", {"repo": "org/same-source"}),
        ("search", {"scope": "memory"}),
        ("search", {"project": "legacy-project"}),
    ],
)
async def test_product_cannot_fall_through_to_unscoped_backends(name, arguments):
    result = await server._dispatch_tool(
        name, {**arguments, "product_id": PRODUCT}, {}, principal()
    )
    assert result["code"] == "unsupported_product_operation"


@pytest.mark.asyncio
async def test_product_argument_cannot_grant_authority(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(server, "_handle_search", search)
    for caller, requested in [
        (principal(product_id=""), PRODUCT),
        (principal(), OTHER),
        (None, PRODUCT),
    ]:
        result = await server._dispatch_tool(
            "search", {"query": "secret", "product_id": requested}, {}, caller
        )
        assert result["code"] == "product_access_denied"
    search.assert_not_called()


@pytest.mark.asyncio
async def test_product_search_uses_semantics_and_preserves_acl_provenance(monkeypatch):
    monkeypatch.setattr(server.config, "semantic_enabled", True)
    acl = Mock(
        get_allowed_product_assets=Mock(return_value={"asset-a": ATTEMPT}),
        get_active_ingestion_attempts=Mock(return_value={ATTEMPT}),
    )
    monkeypatch.setattr(server.state, "acl_store", acl)
    response = Mock()
    response.json.return_value = {"model": "amazon.titan-embed-text-v2:0", "usage": {"prompt_tokens": 8, "total_tokens": 8, "completion_tokens": 0}, "data": [{"embedding": [0.1]}]}
    monkeypatch.setattr(
        server.state, "semantic_http_client", Mock(post=AsyncMock(return_value=response))
    )
    vectors = Mock(
        query_scoped=Mock(
            return_value=[
                {"key": "a", "metadata": product_hit().data},
                {"key": "b", "metadata": product_hit(product=OTHER).data},
                {"key": "c", "metadata": product_hit("removed").data},
            ]
        )
    )
    monkeypatch.setattr(server.state, "semantic_code_store", vectors)
    exact = Mock(search=AsyncMock())
    monkeypatch.setattr(server.state, "zoekt", exact)
    result = await server._dispatch_tool(
        "search", {"query": "secret", "product_id": PRODUCT}, {}, principal()
    )
    assert [r["asset_id"] for r in result["results"]] == ["asset-a"]
    assert vectors.query_scoped.call_args.kwargs["product_id"] == PRODUCT
    assert vectors.query_scoped.call_args.kwargs["active_attempt_ids"] == {ATTEMPT}
    exact.search.assert_not_called()


def test_legacy_s3_paths_cannot_claim_product_artifacts_by_repo_alias():
    index = RepositoryNameIndex(["org/same-source"])
    index.ownership = {"org/same-source": ("tenant", None)}
    assert (
        _repo_for_content_key(
            "tenants/tenant/products/org-same-source/content/assets/a/web/x.md", index, "content"
        )
        == ""
    )


@pytest.fixture
def vectors(monkeypatch):
    client = Mock()
    monkeypatch.setattr(
        "personal_context.backends.s3_vectors_backend.boto3.client", lambda *a, **k: client
    )
    return S3VectorsCodeStore("bucket", shard_count=1), client


def test_product_vector_query_filters_and_rejects_misbehaving_cache(vectors):
    store, client = vectors
    client.query_vectors.return_value = {
        "vectors": [
            {"key": "a", "metadata": product_hit().data},
            {"key": "b", "metadata": product_hit(product=OTHER).data},
            {"key": "c", "metadata": product_hit(tenant="other").data},
            {"key": "legacy", "metadata": {"repo": "org/same-source"}},
        ]
    }
    result = store.query_scoped(
        [0.1],
        "org",
        tenant_id="tenant",
        owner_sub="member",
        product_id=PRODUCT,
        active_attempt_ids={ATTEMPT},
    )
    assert [r["key"] for r in result] == ["a"]
    client.query_vectors.assert_called_once()
    request = client.query_vectors.call_args.kwargs
    assert request["indexName"] == "tenant-tenant"
    assert request["returnMetadata"] is True
    assert {"product_id": {"$eq": PRODUCT}} in request["filter"]["$and"]


@pytest.mark.parametrize("legacy", [False, True])
def test_all_unscoped_vector_paths_exclude_product_data_even_if_filter_ignored(vectors, legacy):
    store, client = vectors
    client.query_vectors.return_value = {
        "vectors": [
            {"key": "private", "metadata": product_hit().data},
            {"key": "legacy", "metadata": {"repo": "org/same-source"}},
        ]
    }
    result = (
        store.query([0.1], "org")
        if legacy
        else store.query_scoped([0.1], "org", tenant_id="tenant", owner_sub="member")
    )
    assert result and all(r["key"] == "legacy" for r in result)
    for call in client.query_vectors.call_args_list:
        assert {"product_id": {"$exists": False}} in call.kwargs["filter"]["$and"]


def test_product_write_cannot_default_to_shared_index(vectors):
    store, client = vectors
    vector = {
        "key": "asset-a:chunk",
        "embedding": [0.1],
        "metadata": {"asset_id": "asset-a", "ingestion_attempt_id": ATTEMPT},
    }
    with pytest.raises(ValueError, match="product_id"):
        store.put_vectors_scoped([vector], "org", visibility="product", tenant_id="tenant")
    with pytest.raises(ValueError, match="tenant_id"):
        store.put_vectors_scoped([vector], "org", visibility="product", product_id=PRODUCT)
    store.put_vectors_scoped(
        [vector], "org", visibility="product", tenant_id="tenant", product_id=PRODUCT
    )
    request = client.put_vectors.call_args.kwargs
    assert request["indexName"] == "tenant-tenant"
    assert request["vectors"][0]["metadata"] == {
        "asset_id": "asset-a",
        "ingestion_attempt_id": ATTEMPT,
        "product_id": PRODUCT,
        "tenant_id": "tenant",
        "visibility": "product",
    }


def personal_hit(**overrides):
    return SearchHit(
        "org/same-source",
        {
            "visibility": "personal",
            "asset_id": "asset-a",
            "owner_sub": "member",
            "tenant_id": "tenant",
            "ingestion_attempt_id": ATTEMPT,
            **overrides,
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_sub": "other"},
        {"tenant_id": "other"},
        {"tenant_id": ""},
        {"ingestion_attempt_id": "old"},
        {"ingestion_attempt_id": ""},
        {"asset_id": "removed"},
        {"product_id": PRODUCT},
    ],
)
def test_personal_registration_cannot_inherit_repository_access(changes):
    acl = Mock(
        get_allowed_repos=Mock(return_value={"org/same-source"}),
        get_allowed_personal_assets=Mock(return_value={"asset-a": ("tenant", ATTEMPT)}),
    )
    caller = principal(product_id="")
    own, foreign = personal_hit(), personal_hit(**changes)
    legacy = SearchHit("org/same-source", {"chunk_text": "legacy"})
    assert filter_results([own, foreign, legacy], caller, acl) == [own, legacy]


def test_personal_vector_scope_reads_only_personal_index(vectors):
    store, client = vectors
    client.query_vectors.return_value = {
        "vectors": [
            {"key": "own", "metadata": personal_hit().data},
            {"key": "product", "metadata": product_hit().data},
        ]
    }
    result = store.query_scoped(
        [0.1],
        "org",
        tenant_id="tenant",
        owner_sub="member",
        personal_only=True,
        active_attempt_ids={ATTEMPT},
    )
    assert [r["key"] for r in result] == ["own"]
    assert client.query_vectors.call_count == 2
    assert client.query_vectors.call_args.kwargs["indexName"] == "personal-member"


@pytest.mark.asyncio
async def test_personal_document_without_repo_uses_current_owner_registration(monkeypatch):
    monkeypatch.setattr(server.config, "semantic_enabled", True)
    acl = Mock(
        get_allowed_personal_assets=Mock(return_value={"asset-a": ("tenant", ATTEMPT)}),
        get_active_ingestion_attempts=Mock(return_value={ATTEMPT}),
    )
    monkeypatch.setattr(server.state, "acl_store", acl)
    hit = personal_hit()
    hit.repo_name = ""
    response = Mock()
    response.json.return_value = {"model": "amazon.titan-embed-text-v2:0", "usage": {"prompt_tokens": 8, "total_tokens": 8, "completion_tokens": 0}, "data": [{"embedding": [0.1]}]}
    monkeypatch.setattr(
        server.state, "semantic_http_client", Mock(post=AsyncMock(return_value=response))
    )
    vectors = Mock(query_scoped=Mock(return_value=[{"metadata": hit.data}]))
    monkeypatch.setattr(server.state, "semantic_code_store", vectors)
    result = await server._handle_search(
        {"query": "notes", "scope": "personal"}, principal(product_id="")
    )
    assert result["results"][0]["asset_id"] == "asset-a"
    assert vectors.query_scoped.call_args.kwargs["personal_only"] is True
    acl.get_allowed_repos.assert_not_called()


def _matches_metadata(metadata, expression):
    """A small filtering backend applies filters before its bounded nearest-neighbor slice."""
    if "$and" in expression:
        return all(_matches_metadata(metadata, part) for part in expression["$and"])
    field, operators = next(iter(expression.items()))
    operation, value = next(iter(operators.items()))
    return {
        "$eq": lambda: metadata.get(field) == value,
        "$ne": lambda: metadata.get(field) != value,
        "$exists": lambda: (field in metadata) == value,
        "$in": lambda: metadata.get(field) in value,
    }[operation]()


@pytest.mark.parametrize("personal", [False, True])
def test_old_identical_chunks_cannot_consume_top_k_before_current_generation(vectors, personal):
    store, client = vectors
    metadata = personal_hit().data if personal else product_hit().data
    corpus = [
        {
            "key": f"old-{i}",
            "distance": 0.01,
            "metadata": {**metadata, "ingestion_attempt_id": "obsolete"},
        }
        for i in range(200)
    ]
    corpus.append({"key": "current", "distance": 0.2, "metadata": metadata})
    if personal:
        corpus.append({"key": "legacy", "distance": 0.3, "metadata": {"repo": "org/same-source"}})

    def query(**kwargs):
        filtered = [row for row in corpus if _matches_metadata(row["metadata"], kwargs["filter"])]
        return {"vectors": sorted(filtered, key=lambda row: row["distance"])[: kwargs["topK"]]}

    client.query_vectors.side_effect = query
    args = {"personal_only": True} if personal else {"product_id": PRODUCT}
    result = store.query_scoped(
        [0.1],
        "org",
        tenant_id="tenant",
        owner_sub="member",
        top_k=2,
        active_attempt_ids={ATTEMPT},
        **args,
    )
    assert [row["key"] for row in result] == (["current", "legacy"] if personal else ["current"])
    assert all(row["metadata"].get("ingestion_attempt_id") != "obsolete" for row in result)


def test_generation_filter_batches_bound_backend_lists_and_merge_global_distance(vectors):
    store, client = vectors
    attempts = {f"attempt-{i:04}" for i in range(201)}

    def query(**kwargs):
        batch = kwargs["filter"]["$and"][-1]["ingestion_attempt_id"]["$in"]
        assert 0 < len(batch) <= 100
        return {
            "vectors": [
                {
                    "key": attempt,
                    "distance": 1000 - int(attempt[-4:]),
                    "metadata": {**product_hit().data, "ingestion_attempt_id": attempt},
                }
                for attempt in batch
            ]
        }

    client.query_vectors.side_effect = query
    result = store.query_scoped(
        [0.1], "org", tenant_id="tenant", product_id=PRODUCT, active_attempt_ids=attempts, top_k=2
    )
    assert [row["key"] for row in result] == ["attempt-0200", "attempt-0199"]
    assert client.query_vectors.call_count == 3


def test_scope_limit_is_explicit_and_empty_product_never_queries_unbounded(vectors):
    store, client = vectors
    with pytest.raises(ValueError, match="1024"):
        store.query_scoped(
            [0.1],
            "org",
            tenant_id="tenant",
            product_id=PRODUCT,
            active_attempt_ids={str(i) for i in range(1025)},
        )
    assert (
        store.query_scoped(
            [0.1], "org", tenant_id="tenant", product_id=PRODUCT, active_attempt_ids=set()
        )
        == []
    )
    client.query_vectors.assert_not_called()


@pytest.mark.asyncio
async def test_scope_limit_reports_error_before_embedding_or_query(monkeypatch):
    monkeypatch.setattr(server.config, "semantic_enabled", True)
    from door.acl import KnowledgeScopeLimitError

    monkeypatch.setattr(
        server.state,
        "acl_store",
        Mock(
            get_active_ingestion_attempts=Mock(
                side_effect=KnowledgeScopeLimitError(
                    "Knowledge scope exceeds 1024 active ingestion attempts"
                )
            )
        ),
    )
    http = Mock(post=AsyncMock())
    vectors = Mock()
    monkeypatch.setattr(server.state, "semantic_http_client", http)
    monkeypatch.setattr(server.state, "semantic_code_store", vectors)
    result = await server._handle_semantic_search("notes", 20, principal())
    assert result["code"] == "knowledge_scope_limit"
    http.post.assert_not_called()
    vectors.query_scoped.assert_not_called()
