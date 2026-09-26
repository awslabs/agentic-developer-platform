"""Tenant binding, provider ownership and revocation without live providers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from src.gitlab import service
from src.gitlab.routes import Configure, Connect, Project
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential, UserIdentity
from src.shared.schemas.auth import TokenContext

URL = "https://gitlab.example.test"
PID = service.hashlib.sha256(URL.encode()).hexdigest()[:24]
REV = service.hashlib.sha256(URL.encode()).hexdigest()
CREDENTIAL = str(uuid4())


@pytest.fixture
async def fixture(db_session, monkeypatch):
    db_session.add(Organization(id="tenant", name="Tenant"))
    await db_session.flush()
    db_session.add(User(id="human", org_id="tenant", email="h@example.test", team_id="", cognito_sub="login"))
    await db_session.flush()
    db_session.add(
        UserCredential(
            id=CREDENTIAL, org_id="tenant", user_id="human", service="gitlab", label="test", credential_type="api_key", secret_arn="fixture-only"
        )
    )
    await db_session.commit()
    monkeypatch.setattr(service, "providers", AsyncMock(return_value=[{"id": PID, "url": URL, "kind": "external", "revision": REV}]))
    monkeypatch.setattr(
        service, "root_bindings", lambda: [SimpleNamespace(source="gitlab", instance=URL, project_id=42, tenant_id="tenant", repo="group/project")]
    )
    monkeypatch.setattr(service, "SecretsManagerHelper", lambda: Mock(get_secret=lambda arn: "synthetic-pat"))
    client = AsyncMock()
    client.__aenter__.return_value = client

    def get(url, **kwargs):
        assert url.startswith(URL + "/api/v4/")
        assert kwargs["headers"] == {"PRIVATE-TOKEN": "synthetic-pat"}
        return Mock(
            status_code=200,
            json=lambda: {"id": 7, "username": "human"}
            if url.endswith("/user")
            else {"id": 42, "path_with_namespace": "group/project", "permissions": {"project_access": {"access_level": 40}}},
        )

    client.get.side_effect = get
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: client)
    caller = TokenContext(
        user_id="human", org_id="tenant", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    request = Configure(provider_id=PID, expected_provider_revision=REV, operation_id=uuid4())
    configured = await service.mutate(db_session, caller, "configure", request)
    return caller, configured, client


def connect(revision, **kwargs):
    return Connect(
        **{
            "operation_id": uuid4(),
            "expected_provider_revision": REV,
            "expected_revision": revision,
            "repo": "group/project",
            "project_id": 42,
            "credential_id": CREDENTIAL,
            **kwargs,
        }
    )


async def test_connect_exact_replay_disconnect_and_root_denial(db_session, fixture):
    caller, config, client = fixture
    request = connect(config["revision"])
    result = await service.mutate(db_session, caller, "connect", request)
    replay = await service.mutate(db_session, caller, "connect", request)
    assert replay["replayed"] is True
    assert replay["revision"] == result["revision"]
    identity = (await db_session.scalars(select(UserIdentity))).one()
    assert identity.provider_user_id == URL + "#7"
    assert identity.user_id == "human"
    assert identity.verification_method == "credential_verified"
    binding = service.root_bindings()[0]
    await service.require_managed_association(db_session, binding)
    removal = Project(operation_id=uuid4(), expected_provider_revision=REV, expected_revision=result["revision"], repo="group/project", project_id=42)
    await service.mutate(db_session, caller, "disconnect", removal)
    with pytest.raises(HTTPException) as exc:
        await service.require_managed_association(db_session, binding)
    assert exc.value.status_code == 403
    # Disconnect retains the external identity and provider project/hook.
    assert (await db_session.scalars(select(UserIdentity))).one().id == identity.id
    assert all(call.args[0].startswith(URL) for call in client.get.call_args_list)


async def test_foreign_credential_and_project_are_refused(db_session, fixture):
    caller, config, _ = fixture
    with pytest.raises(HTTPException) as exc:
        await service.mutate(db_session, caller, "connect", connect(config["revision"], credential_id=uuid4()))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await service.mutate(db_session, caller, "connect", connect(config["revision"], project_id=99))
    assert exc.value.status_code == 409


async def test_stale_config_and_operation_payload_cannot_overwrite(db_session, fixture):
    caller, config, _ = fixture
    request = connect(config["revision"])
    result = await service.mutate(db_session, caller, "connect", request)
    with pytest.raises(HTTPException):
        await service.mutate(db_session, caller, "connect", connect(config["revision"]))
    request.expected_revision = result["revision"]
    with pytest.raises(HTTPException):
        await service.mutate(db_session, caller, "connect", request)


async def test_status_separates_access_from_webhook_and_runtime(db_session, fixture):
    caller, _, _ = fixture
    result = await service.describe(db_session, caller, repo="group/project", credential_id=CREDENTIAL)
    assert result["project_access"] == "verified"
    assert result["webhook_delivery"] == result["agent_runtime"] == "unverified"
    assert "synthetic-pat" not in str(result)


@pytest.mark.parametrize(
    "value", ["http://gitlab.example", "https://user:pass@gitlab.example", "https://gitlab.example/../escape", "https://gitlab.example?x=1"]
)
def test_non_origin_provider_configuration_is_refused(value):
    with pytest.raises(HTTPException):
        service.host(value)


async def test_owner_can_reconcile_verified_rename_of_same_numeric_project(db_session, fixture, monkeypatch):
    caller, config, client = fixture
    first = await service.mutate(db_session, caller, "connect", connect(config["revision"]))
    binding = service.root_bindings()[0]
    binding.repo = "new-group/project"
    monkeypatch.setattr(service, "root_bindings", lambda: [binding])
    with pytest.raises(HTTPException):
        await service.require_managed_association(db_session, binding)

    def renamed(url, **kwargs):
        return Mock(
            status_code=200,
            json=lambda: {"id": 7, "username": "human"}
            if url.endswith("/user")
            else {"id": 42, "path_with_namespace": binding.repo, "permissions": {"project_access": {"access_level": 40}}},
        )

    client.get.side_effect = renamed
    result = await service.mutate(db_session, caller, "connect", connect(first["revision"], repo=binding.repo))
    assert result["repo"] == binding.repo
    await service.require_managed_association(db_session, binding)


async def test_human_route_schema_authorization_and_ack(db_session, fixture, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from src.admin import audit_operation
    from src.auth.dependencies import get_current_user
    from src.gitlab import routes
    from src.shared.database import get_db

    caller, config, _ = fixture
    monkeypatch.setattr(audit_operation, "persist", AsyncMock())
    app = FastAPI()
    app.include_router(routes.router)

    async def db():
        yield db_session

    app.dependency_overrides[get_db] = db
    raw_actor = caller.model_copy(update={"user_id": "login"})
    app.dependency_overrides[get_current_user] = lambda: raw_actor
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        denied = await client.post(
            "/gitlab/admin/configure",
            json={
                "operation_id": str(uuid4()),
                "provider_id": PID,
                "expected_provider_revision": REV,
                "expected_revision": config["revision"],
            },
        )
        assert denied.status_code == 403
        request = connect(config["revision"])
        response = await client.post("/gitlab/connect", json=request.model_dump(mode="json"))
        assert response.status_code == 200
        assert response.json()["project_id"] == 42
        assert "synthetic-pat" not in response.text
        status = await client.get("/gitlab/status", params={"repo": "group/project"})
        assert status.status_code == 200
        assert status.json()["identity_linked"] is True
        raw_actor.account_type = "service"
        assert (await client.get("/gitlab/status")).status_code == 403


@pytest.mark.parametrize("path", ["/gitlab", "/services/gitlab", ""])
async def test_platform_provider_preserves_rooted_install_path(monkeypatch, path):
    base = "https://gitlab.example.test" + path
    monkeypatch.setattr(service, "_discover_gitlab_url", lambda: base + "/")
    monkeypatch.setattr(service, "root_bindings", lambda: [])
    (row,) = await service.providers()
    assert row["url"] == base
    assert row["kind"] == "platform"
    assert row["id"] == service.hashlib.sha256(base.encode()).hexdigest()[:24]


@pytest.mark.parametrize(
    "suffix", ["//evil.test", "/%2e%2e/escape", "/gitlab/../escape", "/gitlab/./api", "/gitlab\\escape", "/gitlab?", "/gitlab#", "/gitlab\n"]
)
def test_rooted_provider_refuses_ambiguous_or_escaped_paths(suffix):
    with pytest.raises(HTTPException):
        service.host("https://gitlab.example.test" + suffix)


@pytest.mark.parametrize("redirect", [False, True])
async def test_probe_uses_exact_approved_base_path_without_redirect(db_session, fixture, monkeypatch, redirect):
    import httpx

    caller, _, _ = fixture
    base = "https://gitlab.example.test/gitlab"
    observed = []

    def respond(request):
        observed.append(str(request.url))
        assert request.headers["PRIVATE-TOKEN"] == "synthetic-pat"
        if redirect:
            return httpx.Response(302, headers={"Location": "https://foreign.example/api/v4/user"})
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 7, "username": "human"})
        return httpx.Response(200, json={"id": 42, "path_with_namespace": "group/project", "permissions": {"project_access": {"access_level": 40}}})

    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(respond), **kwargs))
    if redirect:
        with pytest.raises(HTTPException):
            await service.probe(db_session, caller, {"url": base + "/"}, CREDENTIAL, "group/project")
        assert observed == [base + "/api/v4/user"]
    else:
        result = await service.probe(db_session, caller, {"url": base + "/"}, CREDENTIAL, "group/project")
        assert result["project_id"] == 42
        assert observed == [base + "/api/v4/user", base + "/api/v4/projects/group%2Fproject"]
