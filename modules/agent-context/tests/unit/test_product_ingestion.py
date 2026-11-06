"""Registration identity survives ingestion without sharing Product or personal bytes."""

from contextlib import contextmanager
import importlib.util
import ipaddress
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

INGESTION = Path(__file__).resolve().parents[2] / "images" / "ingestion"
sys.path.insert(0, str(INGESTION))
from scope import (  # noqa: E402
    IngestionScope,
    ScopeValidationError,
    parse_scope,
    parse_scope_from_env,
    asset_storage_prefix,
)
from s3_source_guard import check_s3_source  # noqa: E402
from code_embedder import embed_code_repo, embed_registered_documents, S3VectorsCodeWriter  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_") + "_product", INGESTION / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def product(name="product-a"):
    return IngestionScope(visibility="product", tenant_id="tenant-a", product_id=name)


class Vectors:
    def __init__(self):
        self.rows = {}
        self.clears = []

    def clear_asset_vectors(self, index, asset, attempt):
        self.clears.append((index, asset, attempt))
        self.rows = {
            key: value
            for key, value in self.rows.items()
            if key[0] != index or not key[1].startswith(f"{asset}:{attempt}:")
        }

    def put_vectors(self, index, vectors):
        self.rows.update({(index, row["key"]): row for row in vectors})

    def count_asset_vectors(
        self, index, _vector, tenant, product_id, asset, attempt, owner_sub=None
    ):
        return sum(
            key[0] == index
            and row["metadata"].get("asset_id") == asset
            and row["metadata"].get("product_id") == product_id
            and row["metadata"].get("ingestion_attempt_id") == attempt
            and row["metadata"].get("owner_sub") == owner_sub
            for key, row in self.rows.items()
        )

    def count_repo_vectors(self, index, _vector, repo):
        return sum(
            key[0] == index and row["metadata"].get("repo") == repo
            for key, row in self.rows.items()
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"visibility": "product", "product_id": "p"},
        {"visibility": "product", "tenant_id": "t"},
        {"visibility": "product", "tenant_id": "t", "product_id": "../other"},
        {"visibility": "product", "tenant_id": "t", "product_id": "p", "owner_sub": "u"},
        {"visibility": "tenant", "tenant_id": "t", "product_id": "p"},
    ],
)
def test_invalid_product_scope_cannot_become_shared(invalid):
    with pytest.raises(ScopeValidationError):
        parse_scope(invalid)


def test_product_scope_survives_env_and_has_independent_storage(monkeypatch):
    for key, value in product().to_env().items():
        monkeypatch.setenv(key, value)
    assert parse_scope_from_env() == product()
    a = asset_storage_prefix(product(), "content", "asset-a")
    b = asset_storage_prefix(product("product-b"), "content", "asset-b")
    personal = asset_storage_prefix(
        IngestionScope(visibility="personal", owner_sub="alice"), "content", "asset-c"
    )
    assert a == "tenants/tenant-a/products/product-a/content/assets/asset-a"
    assert len({a, b, personal}) == 3


@pytest.mark.parametrize(
    "key,allowed",
    [
        ("tenants/tenant-a/products/product-a/docs/manual.pdf", True),
        ("tenants/tenant-a/products/product-b/docs/manual.pdf", False),
        ("tenants/tenant-a/docs/manual.pdf", False),
        ("users/alice/docs/manual.pdf", False),
        ("tenants/tenant-a/products/product-a/../product-b/manual.pdf", False),
    ],
)
def test_source_import_cannot_escape_product_even_with_broad_allowlist(key, allowed):
    assert (
        check_s3_source(
            f"s3://content-bucket/{key}",
            "content-bucket/tenants",
            "content-bucket",
            scope=product(),
        ).allowed
        is allowed
    )


def test_external_source_requires_explicit_prefix_and_tenant_cannot_import_product():
    assert check_s3_source(
        "s3://external-docs/approved/manual.pdf",
        "external-docs/approved",
        "content-bucket",
        scope=product(),
    ).allowed
    assert not check_s3_source(
        "s3://content-bucket/tenants/tenant-a/products/product-b/manual.pdf",
        "content-bucket/tenants",
        "content-bucket",
        scope=IngestionScope(visibility="tenant", tenant_id="tenant-a"),
    ).allowed


def test_same_repository_in_two_products_and_personal_stays_separate_and_reindex_prunes(tmp_path):
    (tmp_path / "main.py").write_text("old = 1\n" * 1800)
    writer = Vectors()
    client = Mock(embed=Mock(return_value=[0.1] * 1024))
    for scope, asset in [
        (product(), "asset-a"),
        (product("product-b"), "asset-b"),
        (IngestionScope(visibility="personal", tenant_id="tenant-a", owner_sub="alice"), "asset-c"),
    ]:
        result = embed_code_repo(
            str(tmp_path),
            "acme/same",
            bucket_name="vectors",
            embedding_client=client,
            vector_writer=writer,
            visibility=scope.visibility,
            tenant_id=scope.tenant_id,
            owner_sub=scope.owner_sub,
            product_id=scope.product_id,
            registry_asset_id=asset,
            ingestion_attempt_id="attempt-a",
        )
        assert result["status"] == "complete"
    before_other = {
        key: row for key, row in writer.rows.items() if not key[1].startswith("asset-a:")
    }
    (tmp_path / "main.py").write_text("new = 1\n")
    result = embed_code_repo(
        str(tmp_path),
        "acme/same",
        bucket_name="vectors",
        embedding_client=client,
        vector_writer=writer,
        visibility="product",
        tenant_id="tenant-a",
        product_id="product-a",
        registry_asset_id="asset-a",
        ingestion_attempt_id="attempt-a",
    )
    assert result["status"] == "complete"
    a = [row for key, row in writer.rows.items() if key[1].startswith("asset-a:")]
    assert len(a) == 1 and a[0]["metadata"]["chunk_text"] == "new = 1\n"
    assert {
        key: row for key, row in writer.rows.items() if not key[1].startswith("asset-a:")
    } == before_other
    personal = [
        row["metadata"] for key, row in writer.rows.items() if key[1].startswith("asset-c:")
    ]
    assert all(
        row["visibility"] == "personal" and row["owner_sub"] == "alice" and "product_id" not in row
        for row in personal
    )


def test_cleanup_deletes_only_exact_asset_prefix_across_pages():
    writer = object.__new__(S3VectorsCodeWriter)
    writer.bucket_name = "vectors"
    writer._ensured = {"tenant-tenant-a"}
    writer._client = Mock()
    writer._client.list_vectors.side_effect = [
        {
            "vectors": [
                {"key": "asset-a:attempt-a:code:0"},
                {"key": "asset-a-other:code:0"},
                {"key": "asset-a:newer-attempt:code:0"},
            ],
            "nextToken": "next",
        },
        {"vectors": [{"key": "asset-b:code:0"}, {"key": "asset-a:attempt-a:code:1"}]},
    ]
    writer.clear_asset_vectors("tenant-tenant-a", "asset-a", "attempt-a")
    writer._client.delete_vectors.assert_called_once_with(
        vectorBucketName="vectors",
        indexName="tenant-tenant-a",
        keys=["asset-a:attempt-a:code:0", "asset-a:attempt-a:code:1"],
    )
    with pytest.raises(ValueError):
        writer.clear_asset_vectors("tenant-tenant-a", "asset-a:", "attempt-a")


def test_markdown_embeddings_keep_all_crawl_pages_and_registration_provenance(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "s3_vectors_bucket", "vectors")
    writer = Vectors()
    docs = [
        {
            "source": f"https://docs.example/{i}",
            "source_type": "url",
            "text": f"Page {i} content",
            "s3_key": f"tenants/tenant-a/products/product-a/content/assets/asset-a/web/{i}.md",
        }
        for i in range(2)
    ]
    result = embed_registered_documents(
        docs,
        product(),
        "asset-a",
        ingestion_attempt_id="attempt-a",
        vector_writer=writer,
        embedding_client=Mock(embed=Mock(return_value=[0.1] * 1024)),
    )
    assert result["status"] == "complete" and result["vectors"] == 2
    assert writer.clears == [("tenant-tenant-a", "asset-a", "attempt-a")]
    for (index, key), row in writer.rows.items():
        assert index == "tenant-tenant-a" and key.startswith("asset-a:attempt-a:content:")
        assert row["metadata"]["product_id"] == "product-a"
        assert row["metadata"]["tenant_id"] == "tenant-a"
        assert row["metadata"]["visibility"] == "product"
        assert row["metadata"]["asset_id"] == "asset-a"
        assert row["metadata"]["source_type"] == "url"


@pytest.mark.parametrize(
    "scope",
    [product(), IngestionScope(visibility="personal", tenant_id="tenant-a", owner_sub="alice")],
)
@pytest.mark.parametrize("kind", ["url", "doc"])
@pytest.mark.parametrize("embedded", ["complete", "failed"])
async def test_product_document_pipeline_tracks_embedding_and_never_uses_global_graph(
    monkeypatch, tmp_path, kind, embedded, scope
):
    module = load_script(f"ingest-{kind}")
    monkeypatch.setenv("INGESTION_ATTEMPT_ID", "attempt-a")
    for key, value in scope.to_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "STAGE_TRACKING_AVAILABLE", False)
    store = Mock()
    factory = Mock(return_value=store)
    monkeypatch.setattr(module, "S3ContentStore", factory)
    embed = Mock(
        return_value={
            "status": embedded,
            "vectors": 1 if embedded == "complete" else 0,
            "index": "tenant-tenant-a",
            "error": "embedding unavailable",
        }
    )
    monkeypatch.setattr(module, "embed_registered_documents", embed)
    if kind == "url":
        import url_denylist

        monkeypatch.setattr(
            url_denylist, "_resolve_hostname", lambda _: [ipaddress.ip_address("93.184.216.34")]
        )
        monkeypatch.setattr(module, "discover_pages", lambda *_: ["https://docs.example/guide"])
        monkeypatch.setattr(module, "crawl_url", AsyncMock(return_value="Documentation. " * 10))
        result = await module.ingest_url("https://docs.example/guide", registry_asset_id="asset-a")
        assert result["stage_results"]["embed_vectors"] == (
            "verified" if embedded == "complete" else "failed"
        )
    else:
        document = tmp_path / "manual.md"
        document.write_text("Documentation")
        monkeypatch.setattr(module, "fetch_document", lambda *_: str(document))
        monkeypatch.setattr(module, "convert_to_markdown", lambda *_: "Documentation")
        monkeypatch.setattr(module, "GRAPHRAG_ENABLED", True)
        graph = Mock()
        monkeypatch.setattr(module, "extract_entities_graphrag", graph)
        result = module.ingest_document(
            "s3://external-docs/approved/manual.md", registry_asset_id="asset-a"
        )
        assert result["steps"]["embed_vectors"] == ("ok" if embedded == "complete" else "failed")
        assert result["status"] == ("complete" if embedded == "complete" else "partial")
        graph.assert_not_called()
    assert factory.call_args.kwargs["prefix"].startswith(
        asset_storage_prefix(scope, "content", "asset-a")
    )
    assert embed.call_args.args[1:] == (scope, "asset-a")
    assert embed.call_args.args[0][0]["s3_key"].startswith(
        asset_storage_prefix(scope, "content", "asset-a")
    )


@pytest.mark.parametrize(
    "scope",
    [product(), IngestionScope(visibility="personal", tenant_id="tenant-a", owner_sub="alice")],
)
def test_registered_repo_pipeline_skips_global_catalog_and_all_structural_stores(
    monkeypatch, tmp_path, scope
):
    module = load_script("ingest-repo")
    monkeypatch.setenv("INGESTION_REGISTRY_ASSET_ID", "asset-a")
    monkeypatch.setenv("INGESTION_ATTEMPT_ID", "attempt-a")
    monkeypatch.setattr(module, "parse_scope_from_env", lambda: scope)
    import db

    conn = Mock()
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    catalog = Mock(side_effect=AssertionError("global repo catalog must not be touched"))
    monkeypatch.setattr(db, "ensure_repo_exists", catalog)

    def clone(*_):
        (tmp_path / ".git").mkdir(exist_ok=True)
        return True

    monkeypatch.setattr(module, "git_clone", clone)
    monkeypatch.setattr(module, "_get_commit_sha", lambda *_: "new-sha")
    monkeypatch.setattr(
        module,
        "_run_embed_vectors",
        lambda *_, **__: {"status": "complete", "vectors": 2, "index": "scoped-index"},
    )
    from stage_tracker import StageContext

    trackers = []

    class Tracker:
        def __init__(self, _conn, key, **kwargs):
            self.key, self.results, self.run_id = key, [], "run-a"
            trackers.append(self)

        @contextmanager
        def stage(self, name):
            ctx = StageContext(name)
            yield ctx
            self.results.append(
                SimpleNamespace(stage=name, status="verified" if ctx._verified else "failed")
            )

        def finalize(self):
            pass

    monkeypatch.setattr(module, "StageTracker", Tracker)
    result = module._ingest_repo_in_snapshot("acme/repo", clone_path=str(tmp_path))
    assert trackers[0].key == "asset-a"
    assert result["worker_result"]["steps"] == {"clone": "verified", "embed_vectors": "verified"}
    catalog.assert_not_called()
    conn.close.assert_called_once()


def test_worker_passes_product_and_asset_to_child_and_uses_asset_state(monkeypatch):
    monkeypatch.setattr("boto3.client", Mock(return_value=Mock()))
    monkeypatch.setattr("boto3.resource", Mock(return_value=Mock()))
    module = load_script("sqs-worker")
    monkeypatch.setattr(module, "SQS_QUEUE_URL", "queue")
    message = {
        "content_type": "repo",
        "source": "acme/repo",
        "scope": product().to_dict(),
        "registry_asset_id": "asset-a",
        "ingestion_attempt_id": "attempt-a",
    }
    monkeypatch.setattr(module, "receive_sqs_message", lambda: (message, "receipt"))
    monkeypatch.setattr(module, "_mint_github_token", lambda: True)
    monkeypatch.setattr(module, "safe_emit", lambda *_, **__: None)
    run = Mock(return_value={"status": "complete", "steps": {"embed_vectors": "verified"}})
    monkeypatch.setattr(module, "ingest_repo", run)
    state, records = Mock(), Mock()
    monkeypatch.setattr(module, "update_dynamo_status", state)
    monkeypatch.setattr(module, "write_dynamo_run_record", records)
    module.main()
    assert run.call_args.kwargs["scope_env"]["INGESTION_SCOPE_PRODUCT_ID"] == "product-a"
    assert run.call_args.kwargs["scope_env"]["INGESTION_REGISTRY_ASSET_ID"] == "asset-a"
    assert run.call_args.kwargs["scope_env"]["INGESTION_ATTEMPT_ID"] == "attempt-a"
    assert all(call.args[0] == "asset-a" for call in state.call_args_list + records.call_args_list)


def test_late_attempt_never_overwrites_newer_attempt(tmp_path):
    (tmp_path / "main.py").write_text("new content")
    writer = Vectors()
    client = Mock(embed=Mock(return_value=[0.1] * 1024))

    def run(attempt):
        return embed_code_repo(
            str(tmp_path),
            "acme/repo",
            bucket_name="vectors",
            embedding_client=client,
            vector_writer=writer,
            visibility="product",
            tenant_id="tenant-a",
            product_id="product-a",
            registry_asset_id="asset-a",
            ingestion_attempt_id=attempt,
        )

    assert run("new-attempt")["status"] == "complete"
    current = dict(writer.rows)
    (tmp_path / "main.py").write_text("old worker finishing late")
    assert run("old-attempt")["status"] == "complete"
    assert all(writer.rows[key] == row for key, row in current.items())
    old = [
        row
        for row in writer.rows.values()
        if row["metadata"]["ingestion_attempt_id"] == "old-attempt"
    ]
    assert len(old) == 1  # Door rejects this generation against the registry's current attempt.


def test_registered_embeddings_without_attempt_write_nothing(tmp_path):
    writer = Vectors()
    with pytest.raises(ScopeValidationError):
        embed_code_repo(
            str(tmp_path),
            "acme/repo",
            bucket_name="vectors",
            embedding_client=Mock(),
            vector_writer=writer,
            visibility="product",
            tenant_id="tenant-a",
            product_id="product-a",
            registry_asset_id="asset-a",
        )
    assert writer.rows == {} and writer.clears == []


def test_registry_refresh_preserves_product_attempt_and_asset_state(monkeypatch):
    from registry_reader import RegistryAsset

    module = load_script("publish-ingestion")
    row = RegistryAsset(
        "asset-a",
        "https://github.com/acme/repo",
        "repo",
        "tenant-a",
        None,
        None,
        None,
        product_id="product-a",
        ingestion_attempt_id="attempt-a",
    )
    monkeypatch.setattr("registry_reader.read_registry_assets", lambda *_, **__: [row])
    state = Mock(return_value={})
    monkeypatch.setattr(module, "get_dynamo_state", state)
    monkeypatch.setattr(module, "has_changed", lambda *_: True)
    publish = Mock(return_value=True)
    monkeypatch.setattr(module, "publish_message", publish)
    assert module.publish_from_registry()["enqueued"] == 1
    assert state.call_args.args == ("asset-a", "repo")
    assert publish.call_args.kwargs["scope"] == product()
    assert publish.call_args.kwargs["ingestion_attempt_id"] == "attempt-a"
    assert publish.call_args.kwargs["registry_asset_id"] == "asset-a"


@pytest.mark.parametrize(
    "scope",
    [product(), IngestionScope(visibility="personal", tenant_id="tenant-a", owner_sub="alice")],
)
def test_refresh_publisher_requires_current_attempt_for_registered_scopes(monkeypatch, scope):
    module = load_script("publish-ingestion")
    sqs = Mock()
    monkeypatch.setattr(module, "sqs_client", lambda: sqs)
    assert not module.publish_message(
        "acme/repo", "repo", {}, scope=scope, registry_asset_id="asset-a"
    )
    sqs.send_message.assert_not_called()
    assert module.publish_message(
        "acme/repo",
        "repo",
        {},
        scope=scope,
        registry_asset_id="asset-a",
        ingestion_attempt_id="attempt-a",
    )
    import json

    message = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert message["ingestion_attempt_id"] == "attempt-a"
    assert message["registry_asset_id"] == "asset-a"
    assert message["steps"] == ["embed_vectors"]
    assert message["scope"] == scope.to_dict()
    assert parse_scope(message["scope"]) == scope


def test_registry_reader_carries_nullable_product_and_attempt_without_hard_schema_binding(
    monkeypatch,
):
    from registry_reader import read_registry_assets
    import db

    conn = Mock()
    cursor = conn.cursor.return_value
    cursor.fetchall.return_value = [
        (
            "asset-a",
            "https://github.com/acme/repo",
            "repo",
            "tenant-a",
            None,
            None,
            None,
            "product-a",
            "attempt-a",
        )
    ]
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    rows = read_registry_assets()
    assert rows[0].product_id == "product-a" and rows[0].ingestion_attempt_id == "attempt-a"
    query = cursor.execute.call_args.args[0]
    assert "to_jsonb(knowledge_assets)->>'product_id'" in query
    assert "to_jsonb(knowledge_assets)->>'ingestion_attempt_id'" in query


def test_personal_markdown_uses_owner_index_and_omits_product_metadata(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "s3_vectors_bucket", "vectors")
    writer = Vectors()
    scope = IngestionScope(visibility="personal", tenant_id="tenant-a", owner_sub="alice")
    result = embed_registered_documents(
        [
            {
                "source": "https://docs.example/guide",
                "source_type": "url",
                "text": "Personal notes",
                "s3_key": "users/alice/content/assets/asset-a/attempts/attempt-a/web/guide.md",
            }
        ],
        scope,
        "asset-a",
        ingestion_attempt_id="attempt-a",
        vector_writer=writer,
        embedding_client=Mock(embed=Mock(return_value=[0.1] * 1024)),
    )
    assert result["status"] == "complete"
    for (index, _), row in writer.rows.items():
        assert index == "personal-alice"
        assert row["metadata"]["visibility"] == "personal"
        assert row["metadata"]["owner_sub"] == "alice"
        assert row["metadata"]["asset_id"] == "asset-a"
        assert row["metadata"]["ingestion_attempt_id"] == "attempt-a"
        assert "product_id" not in row["metadata"]
