"""EXT02-t2/t3: installation visibility is not the human's repository visibility."""

import asyncio

import httpx
import pytest

from src.activity.external_authorization import ProviderAccessUnavailableError, github_repository_users, gitlab_repository_users

TOKEN = "synthetic-app-token"
INSTANCE = "https://gitlab.example.invalid/instance"


def run(coroutine):
    return asyncio.run(coroutine)


async def access(provider, handler, identities=frozenset({"17"})):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if provider == "github":
            return await github_repository_users(client, TOKEN, "org/private", identities)
        return await gitlab_repository_users(client, "synthetic-user-token", INSTANCE, 71, "org/private", identities)


def github_response(request, *, permitted=True, alias="renamed-account"):
    assert request.url.host == "api.github.com"
    assert request.headers["authorization"] == "Bearer synthetic-app-token"
    if request.url.path == "/user/17":
        return httpx.Response(200, json={"id": 17, "login": alias, "type": "User"})
    assert request.url.path == f"/repos/org/private/collaborators/{alias}/permission"
    return httpx.Response(200, json={"permission": "read", "user": {"id": 17}}) if permitted else httpx.Response(404)


def gitlab_response(request, *, permitted=True):
    assert request.url.host == "gitlab.example.invalid"
    assert request.headers["private-token"] == "synthetic-user-token"
    if request.url.path.endswith("/api/v4/user"):
        return httpx.Response(200, json={"id": 17, "username": "current-alias"})
    assert request.url.path.endswith("/api/v4/projects/71")
    return (
        httpx.Response(
            200,
            json={
                "id": 71,
                "path_with_namespace": "org/private",
                "permissions": {"project_access": {"access_level": 10}},
            },
        )
        if permitted
        else httpx.Response(404)
    )


def test_app_visible_github_private_repository_denied_without_human_permission():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return github_response(request, permitted=False)

    assert run(access("github", handler)) == frozenset()
    assert calls == ["/user/17", "/repos/org/private/collaborators/renamed-account/permission"]


def test_gitlab_user_credential_cannot_use_an_app_visible_private_project():
    assert run(access("gitlab", lambda request: gitlab_response(request, permitted=False))) == frozenset()


def test_revoked_github_permission_is_rechecked_without_cached_data():
    permitted = True
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return github_response(request, permitted=permitted)

    assert run(access("github", handler)) == frozenset({"17"})
    permitted = False
    assert run(access("github", handler)) == frozenset()
    assert calls.count("/user/17") == 2
    assert calls.count("/repos/org/private/collaborators/renamed-account/permission") == 2


def test_revoked_gitlab_membership_is_rechecked_without_cached_data():
    permitted = True
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return gitlab_response(request, permitted=permitted)

    assert run(access("gitlab", handler)) == frozenset({"17"})
    permitted = False
    assert run(access("gitlab", handler)) == frozenset()
    assert calls.count("/instance/api/v4/projects/71") == 2


def test_old_username_never_nominates_an_identity_and_mismatched_token_is_denied():
    def github_handler(request):
        if request.url.path == "/user/17":
            return httpx.Response(200, json={"id": 18, "login": "stored-alias", "type": "User"})
        raise AssertionError("Stored login was used without an immutable ID match")

    assert run(access("github", github_handler)) == frozenset()

    def gitlab_handler(request):
        assert request.url.path.endswith("/api/v4/user")
        return httpx.Response(200, json={"id": 18})

    with pytest.raises(ProviderAccessUnavailableError, match="credential_identity_mismatch"):
        run(access("gitlab", gitlab_handler))


def test_provider_fault_is_not_treated_as_an_empty_authorized_result():
    with pytest.raises(ProviderAccessUnavailableError, match="permission_check_unavailable"):
        run(access("github", lambda request: httpx.Response(403)))


def test_malformed_permission_payload_fails_closed():
    def github_handler(request):
        if request.url.path == "/user/17":
            return httpx.Response(200, json={"id": 17, "type": "User", "login": "current"})
        return httpx.Response(200, json={"permission": "read", "user": "forged"})

    with pytest.raises(ProviderAccessUnavailableError, match="permission_check_unavailable"):
        run(access("github", github_handler))

    def gitlab_handler(request):
        if request.url.path.endswith("/api/v4/user"):
            return httpx.Response(200, json={"id": 17})
        return httpx.Response(200, json={"id": 71, "path_with_namespace": "org/private", "permissions": "forged"})

    with pytest.raises(ProviderAccessUnavailableError, match="permission_check_unavailable"):
        run(access("gitlab", gitlab_handler))
