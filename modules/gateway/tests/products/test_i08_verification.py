"""Provider observation must prove immutable identity with the selected connection."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.admin.connections.github_client import GitHubAppClient
from src.products.models import ProductSourceBinding
from src.products.verification import observe_binding
from src.shared.models.vault import ChannelTenantMap


class FakeGitHub:
    def __init__(self, *, repository_id=35, owner_id=27, branch="main", revision="a" * 40):
        self.repository_id = repository_id
        self.owner_id = owner_id
        self.calls = []
        self.branch = branch
        self.revision = revision

    async def get_installation(self, installation_id):
        self.calls.append(("installation", installation_id))
        return {"id": 17, "account": {"id": 27, "login": "example"}}

    async def get_installation_repository(self, installation_id, repository_id):
        self.calls.append(("repository", installation_id, repository_id))
        return {
            "id": self.repository_id,
            "owner": {"id": self.owner_id, "login": "example"},
            "name": "app",
            "default_branch": self.branch,
            "updated_at": "2026-01-01T12:00:00Z",
        }

    async def installation_request(self, installation_id, method, path):
        self.calls.append(("commit", installation_id, method, path))
        return {"sha": self.revision}


def selection():
    binding = ProductSourceBinding(
        tenant_id="tenant",
        project_id="project",
        kind="repository",
        provider="github",
        installation_id="connection",
        namespace="0027",
        native_id="0035",
        desired={},
    )
    installation = ChannelTenantMap(
        id="connection",
        org_id="tenant",
        provider="github",
        provider_scope_id="27",
        installation_id="17",
        ownership_disputed=False,
    )
    return binding, installation


@pytest.mark.asyncio
async def test_selected_repository_read_proves_original_identity():
    binding, installation = selection()
    client = FakeGitHub()
    observed_at = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    result = await observe_binding(binding, installation, github_client=client, now=observed_at)
    assert client.calls == [("installation", 17), ("repository", 17, 35), ("commit", 17, "GET", "/repos/example/app/commits/main")]
    assert result == {
        "coverage": "complete",
        "status": "healthy",
        "observed": {
            "observed_at": observed_at.isoformat(),
            "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
            "source_revision": "a" * 40,
            "display_name": "example/app",
            "namespace": "0027",
            "native_id": "0035",
        },
    }
    assert binding.observed is None


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_id,owner_id", [(36, 27), (35, 28)])
async def test_mismatched_repository_or_owner_does_not_produce_evidence(repository_id, owner_id):
    binding, installation = selection()
    result = await observe_binding(binding, installation, github_client=FakeGitHub(repository_id=repository_id, owner_id=owner_id))
    assert result == {"coverage": "denied", "reason": "source_access_denied", "status": "revoked"}
    assert binding.observed is None


@pytest.mark.asyncio
async def test_wrong_tenant_and_unsupported_kind_never_call_provider():
    binding, installation = selection()
    client = FakeGitHub()
    installation.org_id = "different"
    assert (await observe_binding(binding, installation, github_client=client))["coverage"] == "denied"
    installation.org_id = "tenant"
    binding.kind = "tracker"
    assert (await observe_binding(binding, installation, github_client=client)) == {
        "coverage": "unavailable",
        "reason": "unsupported_adapter",
        "status": "unverified",
    }
    assert client.calls == []


@pytest.mark.asyncio
async def test_read_only_github_client_uses_selected_installation_token(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST" and request.url.path == "/app/installations/17/access_tokens":
            return httpx.Response(201, json={"token": "ephemeral-test-token"})
        if request.method == "GET" and request.url.path == "/repositories/35":
            assert request.headers["Authorization"] == "token ephemeral-test-token"
            return httpx.Response(200, json={"id": 35, "owner": {"id": 27}})
        return httpx.Response(404)

    monkeypatch.setattr("src.admin.connections.github_client._mint_app_jwt", lambda *_: "test-jwt")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://api.github.test") as transport:
        client = GitHubAppClient("app-id", "test-key", http_client=transport)
        assert (await client.get_installation_repository(17, 35))["id"] == 35
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/app/installations/17/access_tokens"),
        ("GET", "/repositories/35"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code,coverage", [(403, "denied"), (503, "unavailable")])
async def test_provider_failure_never_produces_evidence(status_code, coverage):
    binding, installation = selection()

    class FailingGitHub(FakeGitHub):
        async def get_installation_repository(self, installation_id, repository_id):
            response = httpx.Response(status_code, request=httpx.Request("GET", "https://api.github.test/repositories/35"))
            response.raise_for_status()

    result = await observe_binding(binding, installation, github_client=FailingGitHub())
    assert result["coverage"] == coverage and "observed" not in result
    assert binding.observed is None


@pytest.mark.asyncio
async def test_bounded_provider_read_reports_unavailable(monkeypatch):
    binding, installation = selection()

    class SlowGitHub(FakeGitHub):
        async def get_installation(self, installation_id):
            await asyncio.sleep(0.1)
            return await super().get_installation(installation_id)

    monkeypatch.setattr("src.products.verification.OBSERVATION_TIMEOUT", 0.001)
    result = await observe_binding(binding, installation, github_client=SlowGitHub())
    assert result["coverage"] == "unavailable" and "observed" not in result


@pytest.mark.asyncio
async def test_repository_refresh_tracks_content_even_when_metadata_timestamp_is_unchanged():
    binding, installation = selection()
    client = FakeGitHub(branch="release/2026")
    first = await observe_binding(binding, installation, github_client=client)
    client.revision = "b" * 40
    second = await observe_binding(binding, installation, github_client=client)
    assert first["observed"]["source_revision"] == "a" * 40
    assert second["observed"]["source_revision"] == "b" * 40
    assert client.calls[-1] == ("commit", 17, "GET", "/repos/example/app/commits/release%2F2026")


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [None, "", "main", "2026-01-01T12:00:00Z", "a" * 39, "g" * 40])
async def test_repository_verification_requires_a_commit_hash(revision):
    binding, installation = selection()
    result = await observe_binding(binding, installation, github_client=FakeGitHub(revision=revision))
    assert result == {"coverage": "unavailable", "reason": "source_revision_unavailable", "status": "unverified"}


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", [None, "", "..", "main\n", "a" * 1025])
async def test_invalid_branch_never_becomes_a_provider_path(branch):
    binding, installation = selection()
    client = FakeGitHub(branch=branch)
    result = await observe_binding(binding, installation, github_client=client)
    assert result["coverage"] == "unavailable"
    assert client.calls == [("installation", 17), ("repository", 17, 35)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code,coverage", [(403, "denied"), (404, "denied"), (409, "unavailable"), (503, "unavailable")])
async def test_commit_read_failure_does_not_fall_back_to_metadata(status_code, coverage):
    binding, installation = selection()

    class FailingCommit(FakeGitHub):
        async def installation_request(self, installation_id, method, path):
            response = httpx.Response(status_code, request=httpx.Request(method, "https://api.github.test" + path))
            response.raise_for_status()

    result = await observe_binding(binding, installation, github_client=FailingCommit())
    assert result["coverage"] == coverage and "observed" not in result


@pytest.mark.asyncio
async def test_explicit_pin_stays_fixed_when_default_branch_changes():
    binding, installation = selection()
    binding.desired = {"source_revision": "c" * 40}
    client = FakeGitHub(revision="c" * 40)
    first = await observe_binding(binding, installation, github_client=client)
    client.branch = "new-default"
    second = await observe_binding(binding, installation, github_client=client)
    assert first["observed"]["source_revision"] == second["observed"]["source_revision"] == "c" * 40
    assert [call[-1] for call in client.calls if call[0] == "commit"] == ["/repos/example/app/commits/" + "c" * 40] * 2
    assert binding.observed is None


@pytest.mark.asyncio
async def test_pinned_commit_read_does_not_require_a_default_branch():
    binding, installation = selection()
    binding.desired = {"source_revision": "c" * 40}
    result = await observe_binding(binding, installation, github_client=FakeGitHub(branch=None, revision="c" * 40))
    assert result["observed"]["source_revision"] == "c" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize("desired", [{"source_revision": value} for value in ("", "main", "c" * 39, "C" * 40, "2026-10-31", 42, [])] + [[], "bad"])
async def test_invalid_stored_pin_never_calls_provider_or_falls_back(desired):
    binding, installation = selection()
    binding.desired = desired
    client = FakeGitHub()
    result = await observe_binding(binding, installation, github_client=client)
    assert result == {"coverage": "unavailable", "reason": "source_revision_invalid", "status": "unverified"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_provider_cannot_replace_requested_commit_with_another_valid_sha():
    binding, installation = selection()
    binding.desired = {"source_revision": "c" * 40}
    client = FakeGitHub(revision="d" * 40)
    result = await observe_binding(binding, installation, github_client=client)
    assert result == {"coverage": "unavailable", "reason": "source_revision_mismatch", "status": "unverified"}
    assert client.calls[-1][-1] == "/repos/example/app/commits/" + "c" * 40
    assert len(client.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code,coverage", [(401, "denied"), (403, "denied"), (404, "denied"), (503, "unavailable")])
async def test_unavailable_pin_is_not_replaced_by_default_branch(status_code, coverage):
    binding, installation = selection()
    binding.desired = {"source_revision": "c" * 40}

    class MissingCommit(FakeGitHub):
        async def installation_request(self, installation_id, method, path):
            self.calls.append(("commit", installation_id, method, path))
            response = httpx.Response(status_code, request=httpx.Request(method, "https://api.github.test" + path))
            response.raise_for_status()

    client = MissingCommit()
    result = await observe_binding(binding, installation, github_client=client)
    assert result["coverage"] == coverage and "observed" not in result
    assert client.calls == [("installation", 17), ("repository", 17, 35), ("commit", 17, "GET", "/repos/example/app/commits/" + "c" * 40)]


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_id,owner_id", [(36, 27), (35, 28)])
async def test_pin_never_bypasses_repository_and_owner_identity(repository_id, owner_id):
    binding, installation = selection()
    binding.desired = {"source_revision": "c" * 40}
    client = FakeGitHub(repository_id=repository_id, owner_id=owner_id, revision="c" * 40)
    assert (await observe_binding(binding, installation, github_client=client))["coverage"] == "denied"
    assert not any(call[0] == "commit" for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute,value", [("id", "other"), ("org_id", "other"), ("provider", "other"), ("ownership_disputed", True)])
async def test_pin_never_bypasses_current_installation_authority(attribute, value):
    binding, installation = selection()
    binding.desired = {"source_revision": "c" * 40}
    setattr(installation, attribute, value)
    client = FakeGitHub(revision="c" * 40)
    assert (await observe_binding(binding, installation, github_client=client))["coverage"] == "denied"
    assert client.calls == []


@pytest.mark.asyncio
async def test_explicit_null_pin_keeps_default_branch_tracking():
    binding, installation = selection()
    binding.desired = {"source_revision": None}
    client = FakeGitHub()
    assert (await observe_binding(binding, installation, github_client=client))["observed"]["source_revision"] == "a" * 40
    assert client.calls[-1][-1] == "/repos/example/app/commits/main"
