"""Tests for adp-review — formal verdict submission and its honest fallback (#5350).

The defect: every PR the engine opens is authored by the tenant's GitHub App, and
every reviewer the engine dispatched authenticated as that SAME App. GitHub answers
HTTP 422 "Can not approve your own pull request" to both APPROVE and
REQUEST_CHANGES. COMMENT is accepted but sets no `reviewDecision`. Nothing owned
review submission, so no code saw the 422, and the reviewer silently downgraded to
a comment or to committed review files — output that looks like a verdict and is
not one.

These tests assert OUTCOMES, not source text:
  * a verdict GitHub accepts is reported as recorded, exit 0
  * the self-review 422 still PUBLISHES the verdict (the analysis is not lost) but
    reports verdict_recorded=False and exits 3, never 0
  * the published body names the pending human approval in terms a human can act on
  * an ordinary 422 (a real call bug) is NOT laundered as "approval pending"
  * losing the verdict entirely is an error, unlike merely failing to record it
  * the reviewer identity is requested, and a fallback is reported rather than
    assumed — including against a gateway that predates this change
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from adp_review import client as review_client
from adp_review.client import (
    EXIT_OK,
    EXIT_PENDING_APPROVAL,
    ReviewError,
    pending_approval_notice,
    submit_review,
)

_REPO = "aws-e/adp"
_PR = 5347
_BODY = "Verdict: APPROVE. All 16 checks green at head 2589dc84."

_SELF_REVIEW_422 = json.dumps({"errors": ["Review Can not approve your own pull request"]})
_SELF_REVIEW_422_CHANGES = json.dumps({"errors": ["Review Can not request changes on your own pull request"]})


class _Api:
    """Scripted GitHub API double. Records every call in order."""

    def __init__(self, *responses: tuple[int, str]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, payload: dict | None, token: str) -> tuple[int, str]:
        self.calls.append((method, path, payload))
        if not self._responses:
            raise AssertionError(f"unexpected extra API call: {method} {path}")
        return self._responses.pop(0)

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]


_HEAD = "2589dc84f4a0c7a4b2ee3e6cbe7fa3b02d9a5c17"


def _ok_review(state: str = "APPROVED", commit_id: str | None = _HEAD) -> tuple[int, str]:
    body: dict = {
        "id": 9001,
        "state": state,
        "html_url": f"https://github.com/{_REPO}/pull/{_PR}#pullrequestreview-9001",
    }
    if commit_id is not None:
        body["commit_id"] = commit_id
    return 200, json.dumps(body)


# ---- the receipt: what the provider actually said --------------------------
#
# `pending_approval` was the only thing a consumer could read, and three unrelated
# causes produce it. A downstream reader that assumed the familiar one told operators
# to configure a reviewer App for failures a reviewer App would not have fixed. And
# the commit the verdict landed on was never reported at all, so "this verdict is
# about the revision I read" was an assertion rather than an observation.


def test_a_recorded_verdict_reports_the_commit_github_returned(monkeypatch):
    api = _Api(_ok_review())
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, commit_id=_HEAD, token="t")

    assert result["commit_id"] == _HEAD


def test_the_reported_commit_is_the_provider_s_not_the_request_s(monkeypatch):
    """The receipt must be able to disagree with the request, or it evidences nothing.

    A consumer compares the published commit with the head the reviewer inspected.
    Defaulting this field to the requested `commit_id` would make that comparison
    compare a value with itself — it could never fail, so the same-revision binding it
    exists to establish would never actually be checked.
    """
    other = "f0d2eb968cb5f9d1322da48d92042cd7f45c166a"
    api = _Api(_ok_review(commit_id=other))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, commit_id=_HEAD, token="t")

    assert result["commit_id"] == other, "the request's commit was echoed instead of the provider's"


@pytest.mark.parametrize("returned", [None, "", 12345, True, {"sha": _HEAD}, []])
def test_a_missing_or_unusable_commit_is_reported_as_none(returned, monkeypatch):
    """Absent is reported as absent; the consumer decides what to do about unknown."""
    body: dict = {"id": 9001, "state": "APPROVED", "html_url": "https://x/1"}
    if returned is not None:
        body["commit_id"] = returned
    api = _Api((200, json.dumps(body)))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, commit_id=_HEAD, token="t")

    assert result["commit_id"] is None


def test_the_self_review_refusal_reports_the_identity_cause(monkeypatch):
    api = _Api((422, _SELF_REVIEW_422), _ok_review(state="COMMENTED"))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["refusal_reason"] == review_client.REFUSAL_SELF_REVIEW


def test_a_downgraded_state_does_not_claim_the_identity_cause(monkeypatch):
    """GitHub accepted the call, so nothing about the reviewer's identity is shown."""
    api = _Api(_ok_review(state="COMMENTED"), (201, json.dumps({"html_url": "https://x#c2"})))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["refusal_reason"] == review_client.REFUSAL_STATE_DOWNGRADED
    assert result["refusal_reason"] != review_client.REFUSAL_SELF_REVIEW


def test_an_unreadable_response_reports_unknown_not_a_guessed_cause(monkeypatch):
    api = _Api((200, "<html>not json</html>"), (201, json.dumps({"html_url": "https://x#c3"})))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["refusal_reason"] == review_client.REFUSAL_UNREADABLE_RESPONSE


def test_a_non_dict_success_body_is_not_treated_as_a_review(monkeypatch):
    """A JSON array parses cleanly and carries no state. Unknown, not recorded."""
    api = _Api((200, "[]"), (201, json.dumps({"html_url": "https://x#c4"})))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["verdict_recorded"] is False
    assert result["refusal_reason"] == review_client.REFUSAL_UNREADABLE_RESPONSE


def test_the_three_refusal_causes_are_distinct_values():
    """Collapsed constants would make every branch above vacuously true."""
    causes = {
        review_client.REFUSAL_SELF_REVIEW,
        review_client.REFUSAL_STATE_DOWNGRADED,
        review_client.REFUSAL_UNREADABLE_RESPONSE,
    }
    assert len(causes) == 3


def test_every_pending_approval_path_states_a_cause(monkeypatch):
    """No `pending_approval` may be silent about why, including the last-resort path."""
    scripts = [
        ((422, _SELF_REVIEW_422), _ok_review(state="COMMENTED")),
        ((422, _SELF_REVIEW_422), (422, "no"), (201, json.dumps({"html_url": "https://x#c5"}))),
        (_ok_review(state="COMMENTED"), (201, json.dumps({"html_url": "https://x#c6"}))),
        ((200, "<html>"), (201, json.dumps({"html_url": "https://x#c7"}))),
    ]
    for script in scripts:
        api = _Api(*script)
        monkeypatch.setattr(review_client, "_api", api)
        result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")
        assert result["outcome"] == "pending_approval"
        assert result.get("refusal_reason"), f"no cause reported for {script}"


# ---- the accepted path -----------------------------------------------------


def test_accepted_verdict_is_reported_as_recorded(monkeypatch):
    api = _Api(_ok_review())
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["outcome"] == "submitted"
    assert result["verdict_recorded"] is True
    assert result["state"] == "APPROVED"
    # Exactly one call: no fallback was attempted when none was needed.
    assert api.paths == [f"/repos/{_REPO}/pulls/{_PR}/reviews"]
    assert api.calls[0][2]["event"] == "APPROVE"


def test_commit_id_is_passed_through_when_given():
    """Pinning the verdict to a SHA is what keeps it from outliving the code."""
    api = _Api(_ok_review())
    with patch.object(review_client, "_api", api):
        submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, commit_id="2589dc84", token="t")
    assert api.calls[0][2]["commit_id"] == "2589dc84"


def test_comment_event_is_never_reported_as_a_recorded_verdict():
    """COMMENT is always accepted by GitHub and sets no reviewDecision."""
    api = _Api(_ok_review(state="COMMENTED"))
    with patch.object(review_client, "_api", api):
        result = submit_review(repo=_REPO, pr_number=_PR, event="COMMENT", body=_BODY, token="t")
    assert result["outcome"] == "submitted"
    assert result["verdict_recorded"] is False


# ---- the self-review refusal: the defect itself ----------------------------


@pytest.mark.parametrize(
    ("event", "refusal"),
    [("APPROVE", _SELF_REVIEW_422), ("REQUEST_CHANGES", _SELF_REVIEW_422_CHANGES)],
)
def test_self_review_refusal_publishes_verdict_but_does_not_record_it(event, refusal):
    """The core contract: the analysis survives, the verdict is not claimed."""
    api = _Api((422, refusal), _ok_review(state="COMMENTED"))
    with patch.object(review_client, "_api", api):
        result = submit_review(repo=_REPO, pr_number=_PR, event=event, body=_BODY, token="t")

    assert result["outcome"] == "pending_approval"
    assert result["verdict_recorded"] is False
    assert result["pending_human_approval"] is True
    # The real verdict was attempted FIRST — the fallback is a fallback, not the
    # default. A reviewer that never tries can never discover the identity is fixed.
    assert api.calls[0][2]["event"] == event
    assert api.calls[1][2]["event"] == "COMMENT"


def test_published_fallback_body_names_the_pending_human_approval():
    api = _Api((422, _SELF_REVIEW_422), _ok_review(state="COMMENTED"))
    with patch.object(review_client, "_api", api):
        submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    published = api.calls[1][2]["body"]
    # The reviewer's actual analysis is preserved, not replaced by the notice.
    assert _BODY in published
    # And a human can tell what happened and what they must do.
    assert "not a formal GitHub review" in published
    assert "human reviewer with write access must submit the formal approval" in published
    assert "does **not** set `reviewDecision`" in published


def test_falls_back_to_an_issue_comment_when_even_a_comment_review_is_refused():
    """Losing the review is worse than recording it in the wrong place."""
    api = _Api(
        (422, _SELF_REVIEW_422),
        (403, json.dumps({"message": "Resource not accessible by integration"})),
        (201, json.dumps({"html_url": f"https://github.com/{_REPO}/pull/{_PR}#issuecomment-1"})),
    )
    with patch.object(review_client, "_api", api):
        result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["outcome"] == "pending_approval"
    assert result["published_as"] == "issue_comment"
    assert result["verdict_recorded"] is False
    assert api.paths[-1] == f"/repos/{_REPO}/issues/{_PR}/comments"


def test_losing_the_verdict_entirely_is_an_error():
    """If nothing reached the PR, the run must fail rather than report success."""
    api = _Api((422, _SELF_REVIEW_422), (500, "boom"), (500, "boom"))
    with patch.object(review_client, "_api", api), pytest.raises(ReviewError, match="fallback comment"):
        submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")


def test_an_ordinary_422_is_not_laundered_as_approval_pending():
    """422 is also returned for real call bugs (bad commit_id, unknown event).

    Reporting those as "a human must approve" would hide a defect in our own call
    behind a plausible platform excuse, and no one would ever fix it.
    """
    api = _Api((422, json.dumps({"errors": [{"message": "commit_id is not part of the pull request"}]})))
    with patch.object(review_client, "_api", api), pytest.raises(ReviewError, match="422"):
        submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, commit_id="deadbeef", token="t")
    # No comment was published: this is a bug to fix, not a verdict to downgrade.
    assert len(api.calls) == 1


# ---- accepted but NOT recorded: the silent no-op (PR #5346) -----------------
#
# Worse than the 422, because nothing fails. On PR #5346 the reviewer check went
# `completed / success` while `GET /pulls/5346/reviews` returned zero reviews and
# `reviewDecision` stayed empty. Deriving "was a verdict recorded?" from the event we
# ASKED for rather than the state GitHub RETURNED reproduces that exactly: a caller
# gating on exit 0 would treat an unapproved PR as approved.


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
def test_accepted_review_with_a_non_verdict_state_is_not_reported_as_recorded(event, monkeypatch):
    """A 2xx whose state carries no verdict must not read as a recorded verdict."""
    api = _Api(_ok_review(state="COMMENTED"), (201, json.dumps({"html_url": "https://github.com/x#issuecomment-2"})))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event=event, body=_BODY, token="t")

    assert result["outcome"] == "pending_approval"
    assert result["verdict_recorded"] is False
    assert result["pending_human_approval"] is True
    assert result["state"] == "COMMENTED"


def test_a_downgraded_verdict_names_the_pending_approval_on_the_pr(monkeypatch):
    """The PR itself must say an approval is pending, not just the run's exit code.

    Without this the state-mismatch path is silent on the PR — which is how #5346
    left an APPROVE recoverable only from a committed file.
    """
    api = _Api(_ok_review(state="COMMENTED"), (201, json.dumps({"html_url": "https://github.com/x#issuecomment-2"})))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert api.paths[-1] == f"/repos/{_REPO}/issues/{_PR}/comments"
    notice = api.calls[-1][2]["body"]
    assert "not a formal GitHub review" in notice
    assert "human reviewer with write access must submit the formal approval" in notice
    # It must describe what actually happened, not claim a 422 that never occurred.
    assert "COMMENTED" in notice
    assert "422" not in notice
    assert result["notice_url"] == "https://github.com/x#issuecomment-2"


def test_an_unparseable_success_body_is_not_assumed_to_be_a_verdict(monkeypatch):
    """If the state cannot be read, it cannot be claimed. Fail toward honesty."""
    api = _Api((200, "<html>not json</html>"), (201, json.dumps({"html_url": "https://github.com/x#issuecomment-3"})))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["verdict_recorded"] is False
    assert result["outcome"] == "pending_approval"


def test_a_failed_notice_does_not_turn_a_missing_verdict_into_success(monkeypatch):
    """Even if the notice cannot be posted, the verdict is still not recorded."""
    api = _Api(_ok_review(state="COMMENTED"), (500, "boom"))
    monkeypatch.setattr(review_client, "_api", api)

    result = submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body=_BODY, token="t")

    assert result["verdict_recorded"] is False
    assert result["notice_url"] is None


# ---- input validation ------------------------------------------------------


def test_unknown_event_is_refused_before_any_api_call():
    api = _Api()
    with patch.object(review_client, "_api", api), pytest.raises(ReviewError, match="event must be one of"):
        submit_review(repo=_REPO, pr_number=_PR, event="LGTM", body=_BODY, token="t")
    assert api.calls == []


def test_empty_body_is_refused():
    api = _Api()
    with patch.object(review_client, "_api", api), pytest.raises(ReviewError, match="empty"):
        submit_review(repo=_REPO, pr_number=_PR, event="APPROVE", body="   ", token="t")
    assert api.calls == []


# ---- the notice ------------------------------------------------------------


def test_notice_wording_matches_the_verdict_kind():
    assert "formal approval" in pending_approval_notice("APPROVE")
    assert "formal change request" in pending_approval_notice("REQUEST_CHANGES")


# ---- identity selection ----------------------------------------------------


class _Client:
    """Double for GatewayCredentialClient."""

    def __init__(self, result: dict, *, configured: bool = True):
        self._result = result
        self.is_configured = configured
        self.requested: dict | None = None

    def github_installation_token(self, **kwargs):
        self.requested = kwargs
        return self._result


def _mint_with(monkeypatch, stub, *, installation: str = "555001"):
    monkeypatch.setenv("GH_APP_INSTALLATION_ID", installation)
    import lib.gateway_credential_client as gcc

    monkeypatch.setattr(gcc, "GatewayCredentialClient", lambda *a, **k: stub)
    return review_client.mint_review_token(repo=_REPO)


def test_reviewer_identity_is_requested_and_used_when_configured(monkeypatch):
    stub = _Client({"token": "ghs_reviewer", "identity": "review", "app_id": "88002"})
    token, identity = _mint_with(monkeypatch, stub)

    assert stub.requested["identity"] == "review"
    assert token == "ghs_reviewer"
    assert identity == "review"


def test_gateway_fallback_is_reported_not_assumed(monkeypatch):
    """Today's live state: the gateway says it granted the authoring identity."""
    stub = _Client({"token": "ghs_author", "identity": "default", "app_id": "99001"})
    _, identity = _mint_with(monkeypatch, stub)
    assert identity == "default"


def test_a_gateway_without_the_identity_field_is_treated_as_default(monkeypatch):
    """A pre-#5350 gateway omits `identity`. Optimism there is what hid this bug."""
    stub = _Client({"token": "ghs_author", "app_id": "99001"})
    _, identity = _mint_with(monkeypatch, stub)
    assert identity == "default"


def test_unreachable_gateway_degrades_to_the_runs_own_identity(monkeypatch):
    """Not fatal: the run can still publish a verdict with a pending notice."""
    stub = _Client({}, configured=False)
    token, identity = _mint_with(monkeypatch, stub)
    assert token is None
    assert identity == "default"


def test_missing_installation_id_degrades_to_the_runs_own_identity(monkeypatch):
    monkeypatch.delenv("GH_APP_INSTALLATION_ID", raising=False)
    token, identity = review_client.mint_review_token(repo=_REPO)
    assert token is None
    assert identity == "default"


# ---- exit codes ------------------------------------------------------------


def test_cli_reads_rotated_token_instead_of_expired_inherited_token(monkeypatch, tmp_path, capsys):
    """Long reviews outlive the initial GH_TOKEN; the existing mount is refreshed."""
    from adp_review import __main__ as cli

    token_file = tmp_path / "token"
    monkeypatch.setenv("ADP_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("GH_TOKEN", "expired-inherited-token")
    monkeypatch.setenv("GITHUB_TOKEN", "another-expired-token")
    monkeypatch.delenv("ADP_REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(cli, "mint_review_token", lambda **kwargs: (None, "default"))
    observed = []

    def api(method, path, payload, token):
        observed.append(token)
        assert payload["commit_id"] == "a" * 40
        assert payload["event"] == "APPROVE"
        if token != token_file.read_text().strip():
            return 401, '{"message":"Bad credentials"}'
        return _ok_review()

    monkeypatch.setattr(review_client, "_api", api)
    for token in ("rotated-token-one", "rotated-token-two"):
        token_file.write_text(token + "\n")
        with pytest.raises(SystemExit) as result:
            cli.cmd_submit(
                [
                    "--repo",
                    _REPO,
                    "--pr",
                    str(_PR),
                    "--event",
                    "APPROVE",
                    "--body",
                    _BODY,
                    "--commit",
                    "a" * 40,
                ]
            )
        assert result.value.code == EXIT_OK
        output = capsys.readouterr()
        assert json.loads(output.out)["verdict_recorded"] is True
        assert "same identity that authored" not in output.err
        assert "GitHub will refuse" not in output.err
        assert token not in output.out + output.err
        assert "expired-inherited-token" not in output.out + output.err
    assert observed == ["rotated-token-one", "rotated-token-two"]


def test_explicit_review_token_takes_precedence_over_regular_rotated_token(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("ordinary-bot-token")
    monkeypatch.setenv("ADP_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ADP_REVIEW_TOKEN", "explicit-reviewer-token")
    assert review_client._github_token() == "explicit-reviewer-token"


@pytest.mark.parametrize("file_state", ["missing", "empty", "unreadable", "non_ascii"])
def test_token_without_usable_mount_keeps_existing_environment_fallback(
    monkeypatch, tmp_path, file_state
):
    token_file = tmp_path / "token"
    if file_state == "empty":
        token_file.write_text(" \n")
    elif file_state == "unreadable":
        token_file.mkdir()
    elif file_state == "non_ascii":
        token_file.write_bytes(b"\xff-corrupt-token")
    monkeypatch.setenv("ADP_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("ADP_REVIEW_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ordinary-environment-token")
    assert review_client._github_token() == "ordinary-environment-token"


def test_default_identity_does_not_claim_a_pr_specific_verdict_capability(monkeypatch, capsys):
    from adp_review import __main__ as cli

    monkeypatch.setattr(cli, "mint_review_token", lambda **kwargs: (None, "default"))
    with pytest.raises(SystemExit) as result:
        cli.cmd_identity(["--repo", _REPO])
    assert result.value.code == EXIT_PENDING_APPROVAL
    assert json.loads(capsys.readouterr().out)["can_record_verdict"] is None


def test_exit_codes_separate_recorded_from_published_only():
    """A caller checking only `exit == 0` must never conclude a verdict exists."""
    assert EXIT_OK == 0
    assert EXIT_PENDING_APPROVAL == 3
    assert EXIT_OK != EXIT_PENDING_APPROVAL
