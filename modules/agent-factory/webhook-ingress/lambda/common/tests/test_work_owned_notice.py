"""Tests for the dropped-reply notice (#9541, AC-02)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import github_actor_authority as authority
from common import work_owned_notice as notice


def _fixture(monkeypatch, *, token_status=201, comment_status=201):
    monkeypatch.setattr(authority, "_app_credentials", lambda deadline: ("123", "key"))
    import jwt

    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "signed-app-jwt")
    calls = []

    def respond(path, token, deadline, *, method="GET", body=None):
        calls.append((method, path, token, body))
        if path == "/app/installations/12/access_tokens":
            return token_status, {
                "token": "ghs_installation"
            } if token_status == 201 else None
        if path == "/repos/org/project/issues/26/comments":
            return comment_status, {"id": 1} if comment_status == 201 else None
        raise AssertionError(path)

    monkeypatch.setattr(authority, "_request", respond)
    return calls


def _post(**overrides):
    arguments = dict(
        installation_id=12,
        repo_full_name="org/project",
        repository_id=34,
        issue_number=26,
    )
    arguments.update(overrides)
    return notice.post_work_owned_notice(**arguments)


def test_notice_is_posted_with_a_repository_scoped_issues_write_token(monkeypatch):
    calls = _fixture(monkeypatch)

    assert _post() is True
    mint, comment = calls
    assert mint[0] == "POST" and mint[2] == "signed-app-jwt"
    assert mint[3] == {"repository_ids": [34], "permissions": {"issues": "write"}}
    assert comment[0] == "POST" and comment[2] == "ghs_installation"
    assert comment[3]["body"].startswith(notice.NOTICE_MARKER)
    assert "not** accepted" in comment[3]["body"]
    assert "repeat your reply" in comment[3]["body"]


def test_notice_reports_false_when_the_token_or_comment_is_refused(monkeypatch):
    _fixture(monkeypatch, token_status=404)
    assert _post() is False
    _fixture(monkeypatch, comment_status=403)
    assert _post() is False


def test_notice_skips_incomplete_references_without_calling_github(monkeypatch):
    calls = _fixture(monkeypatch)
    assert _post(issue_number=None) is False
    assert _post(repo_full_name="noslash") is False
    assert _post(installation_id="abc") is False
    assert calls == []


def test_notice_swallows_evidence_errors(monkeypatch):
    _fixture(monkeypatch)

    def explode(*args, **kwargs):
        raise authority.EvidenceUnavailableError("github_unavailable")

    monkeypatch.setattr(authority, "_request", explode)
    assert _post() is False
