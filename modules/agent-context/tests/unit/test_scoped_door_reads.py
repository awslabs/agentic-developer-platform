"""Read actual tenant/personal ingestion keys with catalogue-backed ownership."""

from io import BytesIO
from unittest.mock import MagicMock

import pytest

from door import server
from door.acl import CallerPrincipal
from door.browse_backend import browse


class Store:
    def __init__(self):
        self.keys = []

    def get_object(self, *, Bucket, Key):
        self.keys.append(Key)
        return {"Body": BytesIO(b"authorized content")}


@pytest.mark.parametrize(
    ("namespace", "tenant", "owner"),
    [
        ("tenants/team-a", "team-a", None),
        ("users/alice", "team-a", "alice"),
    ],
)
@pytest.mark.parametrize(
    "artifact",
    [
        "wikis/org-service-wiki.md",
        "code-indexes/org-service.json",
        "sbom/repos/org/service/source.cdx.json",
    ],
)
async def test_owned_scoped_artifacts_are_readable(namespace, tenant, owner, artifact):
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        ("org/service", tenant, owner)
    ]
    store = Store()
    key = f"{namespace}/{artifact}"
    hits = await browse(
        "read",
        key,
        db_pool=pool,
        s3_client=store,
        bucket="offline-test",
        repo_scope="org/service",
        allowed_repos={"org/service"},
    )
    assert len(hits) == 1
    assert hits[0].repo_name == "org/service"
    assert hits[0].data["content"] == "authorized content"
    assert store.keys == [key]


@pytest.mark.parametrize(
    "key", ["tenants/team-b/wikis/org-service-wiki.md", "users/bob/wikis/org-service-wiki.md"]
)
async def test_caller_scope_cannot_relabel_another_owner_namespace(key):
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        ("org/service", "team-a", "alice")
    ]
    store = Store()
    assert (
        await browse(
            "read",
            key,
            db_pool=pool,
            s3_client=store,
            bucket="offline-test",
            repo_scope="org/service",
            allowed_repos={"org/service"},
        )
        == []
    )
    assert store.keys == []


async def test_dispatch_refuses_foreign_raw_key_before_fetching_bytes(monkeypatch):
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        ("org/secret", "team-b", None)
    ]
    acl = MagicMock()
    acl.get_allowed_repos.return_value = {"org/service"}
    store = Store()
    monkeypatch.setattr(server.state, "db_pool", pool)
    monkeypatch.setattr(server.state, "acl_store", acl)
    monkeypatch.setattr(server.state, "s3_client", store)
    monkeypatch.setattr(server.config, "s3_bucket", "offline-test")
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a")
    result = await server._handle_browse(
        {"action": "read", "uri": "content/wikis/org-secret-wiki.md"}, caller
    )
    assert result["entries"] == []
    assert store.keys == []


@pytest.mark.parametrize("repo", ["example/service", "example/service.py"])
@pytest.mark.parametrize("suffix", ["", "/src/core.py", "::src/core.py::resolve"])
async def test_repository_file_and_symbol_requests_use_catalogued_boundary(monkeypatch, repo, suffix):
    import json
    from door import neptune_client

    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (repo, "team-a", "canonical-user")
    ]
    acl = MagicMock()
    acl.get_allowed_repos.return_value = {repo}
    store = MagicMock()
    store.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    index = {"repo": repo, "symbols": [{"name": "resolve", "type": "function", "file": "src/core.py", "line": 12}]}
    store.get_object.side_effect = lambda **_: {"Body": BytesIO(json.dumps(index).encode())}
    monkeypatch.setattr(server.state, "db_pool", pool)
    monkeypatch.setattr(server.state, "acl_store", acl)
    monkeypatch.setattr(server.state, "s3_client", store)
    monkeypatch.setattr(server.config, "s3_bucket", "offline-test")
    monkeypatch.setattr(neptune_client, "neptune_enabled", lambda: False)
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a", owner_sub="canonical-user")
    result = await server._handle_understand({"target": repo + suffix}, caller)
    assert len(result["definitions"]) == 1
    assert result["definitions"][0]["symbol"] == "resolve"
    assert result["definitions"][0]["repo_id"] == repo
    assert store.get_object.call_args.kwargs["Key"].startswith("users/canonical-user/")
