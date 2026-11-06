"""Product-owned capture, storage and bounded transfer through the mounted IAM routes."""

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, update

from src.internal.l5_credential_routes import L5WorkPermit, get_l5_work_authorizer
from src.products.models import ProductSourceSnapshot
from src.products.source_capture import SnapshotObjects, canonical_repository, get_snapshot_objects
from src.products.source_snapshots import CapturedSource, SourceSnapshotRequest
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_snapshots import snapshot_fixture


@pytest.mark.asyncio
async def test_pinned_bytes_manifest_and_replay(test_client, db_session, app, source_member, monkeypatch):
    grant, pin, _, path, body, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    content = b"original document bytes"
    manifest = b'{"files":[{"path":"document-a","source_revision":"r1"}]}'
    objects = SnapshotObjects(MagicMock(), "private-test")
    stored = {}

    def put(**kwargs):
        version = "version-1"
        stored[kwargs["Key"], version] = kwargs["Body"]
        return {"VersionId": version}

    def get(**kwargs):
        result = stored[kwargs["Key"], kwargs["VersionId"]]
        return {"Body": io.BytesIO(result)}

    objects.s3.put_object.side_effect = put
    objects.s3.get_object.side_effect = get
    app.dependency_overrides[get_snapshot_objects] = lambda: objects
    calls = 0

    async def capture(binding, mapping, request, store):
        nonlocal calls
        calls += 1
        assert binding.native_id == "document-a" and mapping is None
        return CapturedSource(
            source_ref=request.source_ref,
            exclusions=request.exclusions,
            file_count=1,
            expires_at=clock["now"] + timedelta(hours=2),
            content=store.put(content, "text/plain"),
            file_manifest=store.put(manifest, "application/json"),
        )

    monkeypatch.setattr("src.products.source_snapshot_routes.capture_source", capture)
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id=grant.operation_id,
        attempt_id=grant.attempt_id,
        lease_fence=2,
        binding_generation=1,
        expires_at=grant.expires_at,
    )
    app.dependency_overrides[get_l5_work_authorizer] = lambda: lambda grant, selection: authorize(grant, selection, permit)
    request = pin.model_dump(mode="json")
    first = await test_client.post(path, json=request, headers={"Idempotency-Key": "one"})
    assert first.status_code == 201, first.text
    snapshot_id = first.json()["snapshot_id"]
    permit = replace(permit, admitted_source_snapshot_ids=frozenset({snapshot_id}))
    assert first.json()["source_ref"]["record_revision"] == "r1"
    assert (await db_session.scalar(select(ProductSourceSnapshot).where(ProductSourceSnapshot.id == snapshot_id))).receipt == first.json()
    second = await test_client.post(path, json=request, headers={"Idempotency-Key": "one"})
    assert second.status_code == 200 and second.json() == first.json() and calls == 1
    changed = await test_client.post(path, json={**request, "max_bytes": 512}, headers={"Idempotency-Key": "one"})
    assert changed.status_code == 409 and calls == 1
    read = await test_client.post(f"{path}/{snapshot_id}/read", json=body)
    assert read.status_code == 200 and read.json() == first.json()
    for suffix, expected in (("content", content), ("manifest", manifest)):
        response = await test_client.post(f"{path}/{snapshot_id}/{suffix}", json=body)
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert hashlib.sha256(response.content).hexdigest() == first.json()["file_manifest" if suffix == "manifest" else "content"]["sha256"]
        assert response.headers["Cache-Control"] == "no-store"

    def expire_while_reading(**kwargs):
        clock["now"] += timedelta(hours=3)
        return get(**kwargs)

    objects.s3.get_object.side_effect = expire_while_reading
    stale = await test_client.post(f"{path}/{snapshot_id}/content", json=body)
    assert stale.status_code == 410 and stale.json()["detail"]["code"] == "snapshot_expired"
    clock["now"] -= timedelta(hours=3)
    objects.s3.get_object.side_effect = get
    stored[first.json()["content"]["artifact_id"], "version-1"] = b"changed document bytes"
    corrupted = await test_client.post(f"{path}/{snapshot_id}/content", json=body)
    assert corrupted.status_code == 409 and corrupted.json()["detail"]["code"] == "source_integrity_mismatch"
    unavailable = await test_client.post(
        path, json=pin.model_copy(update={"max_bytes": 7 * 1024 * 1024}).model_dump(mode="json"), headers={"Idempotency-Key": "another"}
    )
    assert unavailable.status_code == 422 and calls == 1
    await db_session.execute(
        update(CredentialServiceDelegation)
        .where(CredentialServiceDelegation.id == grant.id)
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    denied = await test_client.post(f"{path}/{snapshot_id}/manifest", json=body)
    assert denied.status_code == 404 and "document-a" not in denied.text


async def authorize(grant, selection, permit):
    assert selection.connection_ref.source_connection_id == grant.credential_id
    return permit


def zip_bytes(names):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in names:
            archive.writestr(name, data)
    return stream.getvalue()


def test_canonical_capture_rejects_unsafe_expansion_and_preserves_revision():
    from datetime import timedelta

    from src.products.source_snapshots import SourceRef

    request = SourceSnapshotRequest(
        delegation_id="delegation",
        delegation_revision=2,
        source_ref=SourceRef(
            tenant_id="tenant",
            project_id="project",
            source_connection_id="connection",
            record_type="repository",
            record_id="123",
            record_revision="a" * 40,
        ),
        exclusions=[],
        max_bytes=1024,
        max_files=2,
        retain_until=datetime.now(UTC) + timedelta(hours=1),
    )
    content, manifest, count = canonical_repository(zip_bytes([("root/z.txt", b"z"), ("root/a.txt", b"a")]), request)
    assert count == 2
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == ["a.txt", "z.txt"]
        assert archive.read("a.txt") == b"a"
    assert [entry["source_revision"] for entry in json.loads(manifest)["files"]] == ["a" * 40] * 2
    for names in (
        [("../safe", b"unsafe")],
        [("root/../secret", b"unsafe")],
        [("root/x", b"one"), ("root/x", b"two")],
        [("root/x", b"x" * 1025)],
        [("root/a", b"a" * 600), ("root/b", b"b" * 500)],
        [("root/a", b"a"), ("root/b", b"b"), ("root/c", b"c")],
    ):
        with pytest.raises(Exception) as error:
            canonical_repository(zip_bytes(names), request)
        assert error.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("personal", [False, True])
async def test_github_connector_uses_only_installation_and_immutable_commit(monkeypatch, personal):
    from types import SimpleNamespace

    import httpx

    from src.products.source_capture import github_capture
    from src.products.source_snapshots import SourceRef

    revision = "a" * 40
    request = SourceSnapshotRequest(
        delegation_id="delegation",
        delegation_revision=2,
        source_ref=SourceRef(
            tenant_id="tenant",
            project_id="project",
            source_connection_id="installation-map",
            record_type="repository",
            record_id="123",
            record_revision=revision,
        ),
        exclusions=[],
        max_bytes=1024,
        max_files=2,
        retain_until=datetime.now(UTC) + timedelta(hours=1),
    )
    mapping = SimpleNamespace(
        id="installation-map",
        org_id="tenant",
        provider="github",
        ownership_disputed=False,
        installation_id="456",
        provider_scope_id="personal:789:owner" if personal else "789",
        installed_by_user_id="owner",
    )
    binding = SimpleNamespace(
        kind="repository", provider="github", installation_id="installation-map", tenant_id="tenant", native_id="123", namespace="789"
    )
    archive = zip_bytes([("repository-aaaaaaa/source.txt", b"pinned")])
    requests = []

    def handler(sent):
        requests.append((sent.url.host, sent.url.path, sent.headers.get("Authorization")))
        if sent.url.host == "codeload.github.com":
            assert "Authorization" not in sent.headers
            return httpx.Response(200, content=archive)
        assert sent.headers.get("Authorization") == "Bearer synthetic-installation-token"
        if "/commits/" in sent.url.path:
            return httpx.Response(200, json={"sha": revision})
        return httpx.Response(302, headers={"location": "https://codeload.github.com/org/repository/zip/" + revision})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    provider_http = real_client(transport=transport, base_url="https://api.github.com", follow_redirects=False)

    class FakeGithub:
        _http_client = provider_http

        async def get_installation_repository(self, installation_id, repository_id):
            assert (installation_id, repository_id) == (456, 123)
            return {"id": 123, "full_name": "org/repository", "owner": {"id": 789, "login": "org"}}

        async def get_installation_token(self, installation_id):
            return "synthetic-installation-token"

        async def aclose(self):
            await provider_http.aclose()

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("synthetic-app", "synthetic-key"))
    monkeypatch.setattr("src.products.source_capture.GitHubAppClient", lambda app_id, private_key: FakeGithub())
    monkeypatch.setattr("src.products.source_capture.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    content, manifest, count = await github_capture(binding, mapping, request)
    assert count == 1 and json.loads(manifest)["files"][0]["source_revision"] == revision
    with zipfile.ZipFile(io.BytesIO(content)) as pinned:
        assert pinned.read("source.txt") == b"pinned"
    assert [host for host, _, _ in requests] == ["api.github.com", "api.github.com", "codeload.github.com"]


def test_snapshot_store_requires_real_versions_and_validates_bytes():
    from fastapi import HTTPException

    s3 = MagicMock()
    store = SnapshotObjects(s3, "private-test")
    s3.put_object.return_value = {"VersionId": "null"}
    with pytest.raises(HTTPException) as error:
        store.put(b"example", "text/plain")
    assert error.value.status_code == 503
    s3.put_object.return_value = {"VersionId": "version-1"}
    reference = store.put(b"example", "text/plain")
    assert s3.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    s3.get_object.return_value = {"Body": io.BytesIO(b"other")}
    with pytest.raises(HTTPException) as error:
        store.get(reference)
    assert error.value.status_code == 409
    s3.get_object.return_value = {"Body": io.BytesIO(b"example")}
    assert store.get(reference) == b"example"
    assert s3.get_object.call_args.kwargs["VersionId"] == "version-1"


@pytest.mark.asyncio
async def test_unsupported_connector_and_unverified_work_fail_closed(test_client, db_session, app, source_member, monkeypatch):
    grant, pin, _, path, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    app.dependency_overrides[get_snapshot_objects] = lambda: SnapshotObjects(MagicMock(), "private-test")
    unsupported = await test_client.post(path, json=pin.model_dump(mode="json"), headers={"Idempotency-Key": "one"})
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["code"] == "stale_delegation_or_attempt"
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id=grant.operation_id,
        attempt_id=grant.attempt_id,
        lease_fence=2,
        binding_generation=1,
        expires_at=grant.expires_at,
    )
    app.dependency_overrides[get_l5_work_authorizer] = lambda: lambda grant, selection: authorize(grant, selection, permit)
    rejected = await test_client.post(path, json=pin.model_dump(mode="json"), headers={"Idempotency-Key": "one"})
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "immutable_source_required"
    assert "document-a" not in rejected.text
