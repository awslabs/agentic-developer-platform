"""SCN-08: installation-scoped actor and floor evidence without live GitHub access."""

import sys
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import github_actor_authority as authority


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    authority._cache.clear()
    monkeypatch.setattr(
        authority, "_app_credentials", lambda deadline: ("123", "test-private-key")
    )
    import jwt

    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "signed-app-jwt")


def check(**kwargs):
    arguments = dict(
        installation_id=12,
        repository_id=34,
        repository_full_name="org/project",
        sender_id=56,
        floor=None,
    )
    arguments.update(kwargs)
    return authority.check_actor(**arguments)


def fixture_api(
    monkeypatch, *, permission="write", account_type="Organization", membership=404
):
    calls = []

    def respond(path, token, deadline, *, method="GET", body=None):
        calls.append((path, token, method, body))
        if path == "/app/installations/12":
            return 200, {
                "id": 12,
                "account": {"id": 7, "login": "org", "type": account_type},
            }
        if path == "/app/installations/12/access_tokens":
            assert method == "POST" and body == {"repository_ids": [34]}
            assert token == "signed-app-jwt"
            return 201, {"token": "installation-token"}
        assert token == "installation-token"
        if path == "/repos/org/project":
            return 200, {"id": 34, "full_name": "org/project", "owner": {"id": 7}}
        if path == "/user/56":
            return 200, {"id": 56, "login": "current-login", "type": "User"}
        if path == "/repos/org/project/collaborators/current-login/permission":
            return 200, {"permission": permission, "user": {"id": 56}}
        if path == "/orgs/org/members/current-login":
            return membership, None
        if path.startswith("/repos/org/project/commits?"):
            return 200, [{"author": {"id": 56}}]
        raise AssertionError(path)

    monkeypatch.setattr(authority, "_request", respond)
    return calls


@pytest.mark.parametrize("permission", ["admin", "maintain", "write"])
def test_writer_bound_to_numeric_sender_and_installation(monkeypatch, permission):
    calls = fixture_api(monkeypatch, permission=permission)
    decision = check()
    assert decision.allowed and decision.permission == permission
    assert decision.login == "current-login"
    assert calls[1][3] == {"repository_ids": [34]}
    assert check() == decision
    assert len(calls) == 5


@pytest.mark.parametrize("permission", ["read", "triage"])
def test_comment_requires_writer_even_without_floor(monkeypatch, permission):
    fixture_api(monkeypatch, permission=permission)
    assert check().reason == "insufficient_permission"


def test_signed_actor_association_only_for_matching_numeric_id(monkeypatch):
    calls = fixture_api(monkeypatch, membership=404)
    assert (
        check(floor="OWNER", signed_association="OWNER", association_actor_id=99).reason
        == "insufficient_association"
    )
    assert check(
        floor="OWNER", signed_association="OWNER", association_actor_id=56
    ).allowed
    assert not any(path.startswith("/orgs/") for path, *_ in calls[-5:])


def test_current_org_member_and_contributor_bounds(monkeypatch):
    fixture_api(monkeypatch, permission="read", membership=204)
    assert check(floor="MEMBER", require_write=False).allowed
    authority._cache.clear()
    calls = fixture_api(monkeypatch, permission="read")
    assert check(floor="CONTRIBUTOR", require_write=False).allowed
    assert any("/commits?" in path for path, *_ in calls)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("installation_id", 0, "actor_evidence_invalid"),
        ("repository_id", True, "actor_evidence_invalid"),
        ("sender_id", "unknown", "actor_evidence_invalid"),
        ("repository_full_name", "other/project/extra", "actor_evidence_invalid"),
        ("floor", "FIRST_TIMER", "eligibility_policy_invalid"),
    ],
)
def test_bad_input_is_denied_without_api(monkeypatch, field, value, reason):
    monkeypatch.setattr(
        authority, "_request", Mock(side_effect=AssertionError("called"))
    )
    arguments = dict(
        installation_id=12,
        repository_id=34,
        repository_full_name="org/project",
        sender_id=56,
        floor=None,
    )
    arguments[field] = value
    assert authority.check_actor(**arguments).reason == reason


@pytest.mark.parametrize(
    "bad_path,body,reason",
    [
        (
            "/app/installations/12",
            {"id": 13, "account": {"id": 7, "login": "org"}},
            "installation_invalid",
        ),
        (
            "/repos/org/project",
            {"id": 99, "full_name": "org/project", "owner": {"id": 7}},
            "repository_invalid",
        ),
        (
            "/user/56",
            {"id": 99, "login": "current-login", "type": "User"},
            "actor_invalid",
        ),
        (
            "/repos/org/project/collaborators/current-login/permission",
            {"permission": "write", "user": {"id": 99}},
            "actor_invalid",
        ),
    ],
)
def test_mismatched_installation_repo_or_actor(monkeypatch, bad_path, body, reason):
    fixture_api(monkeypatch)
    original = authority._request

    def mismatch(path, *args, **kwargs):
        if path == bad_path:
            return 200, body
        return original(path, *args, **kwargs)

    monkeypatch.setattr(authority, "_request", mismatch)
    assert check().reason == reason


def test_provider_fault_not_cached_as_success(monkeypatch):
    fixture_api(monkeypatch)
    original = authority._request
    attempts = 0

    def outage(path, *args, **kwargs):
        nonlocal attempts
        if path == "/app/installations/12" and attempts == 0:
            attempts += 1
            raise authority.EvidenceUnavailableError("github_unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(authority, "_request", outage)
    assert check().reason == "actor_evidence_unavailable"
    assert check().allowed


def test_cache_expires_within_sixty_seconds(monkeypatch):
    calls = fixture_api(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(authority.time, "monotonic", lambda: clock[0])
    assert check().allowed
    assert check().allowed
    assert len(calls) == 5
    clock[0] += 61
    assert check().allowed
    assert len(calls) == 10


def test_http_redirect_and_permission_fault_fail_closed(monkeypatch):
    fixture_api(monkeypatch)
    original = authority._request

    def refused(path, *args, **kwargs):
        if path.endswith("/permission"):
            raise authority.EvidenceUnavailableError("github_unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(authority, "_request", refused)
    assert check().reason == "actor_evidence_unavailable"


@pytest.mark.parametrize("status", [302, 403, 429])
def test_transport_refuses_redirect_missing_permission_and_quota(monkeypatch, status):
    fake_opener = Mock()
    fake_opener.open.side_effect = HTTPError(
        "https://api.github.com/user/56", status, "unavailable", {}, None
    )
    monkeypatch.setattr(authority, "build_opener", lambda *handlers: fake_opener)
    with pytest.raises(authority.EvidenceUnavailableError):
        authority._request("/user/56", "test-token", authority.time.monotonic() + 10)
    assert fake_opener.open.call_args.kwargs["timeout"] <= 3
