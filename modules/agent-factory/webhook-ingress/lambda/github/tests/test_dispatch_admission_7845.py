"""SCN-08: one sender decision precedes both webhook effect paths."""

import hashlib
import hmac
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.github_actor_authority import ActorDecision
from common.identity_resolver import ResolvedIdentity
from intent_parser import DISPATCH_EVENTS

from github import handler as webhook


def _payload(event_type, action, *, engine=False):
    sender = {"id": 56, "login": "labeler", "type": "User"}
    payload = {
        "action": action,
        "installation": {"id": 12},
        "repository": {"id": 34, "full_name": "org/project"},
        "sender": sender,
        "issue": {
            "number": 42,
            "user": {"id": 99},
            "labels": [{"name": "aidlc-intent"}],
            "author_association": "OWNER",
        },
        "label": {"name": "developer"},
        "pull_request": {
            "number": 42,
            "user": {"id": 99},
            "author_association": "OWNER",
            "head": {"ref": "agent/issue-42"},
        },
        "comment": {
            "body": "@agent-engine halt" if engine else "@agent-developer help",
            "user": {"id": 56},
            "author_association": "COLLABORATOR",
        },
    }
    if event_type == "issues" and action == "opened":
        payload["issue"]["user"]["id"] = 56
    if event_type == "pull_request" and action == "opened":
        payload["pull_request"]["user"]["id"] = 56
    return payload


def _event(event_type, payload):
    body = json.dumps(payload)
    digest = hmac.new(b"fixture-secret", body.encode(), hashlib.sha256).hexdigest()
    return {
        "body": body,
        "headers": {
            "x-github-event": event_type,
            "x-github-delivery": "signed-fixture-1",
            "x-hub-signature-256": f"sha256={digest}",
        },
    }


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("GITHUB_AUTO_PR_REVIEW_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    resolved = ResolvedIdentity("org", "org", "user-56", "strict", verification_method="oauth")
    resolver = SimpleNamespace(resolve=MagicMock(return_value=(resolved, "ok")), last_tenant_item={"org_id": "org", "min_author_association": "MEMBER"})
    rate = SimpleNamespace(allowed=True, retry_after_seconds=0)
    store = MagicMock()
    store.channel_key.return_value = "channel-42"
    store.read_pointer.return_value = None
    with (
        patch.object(webhook, "_resolve_webhook_secret", return_value="fixture-secret"),
        patch.object(webhook, "_get_identity_resolver", return_value=resolver),
        patch.object(webhook, "_get_rate_limiter") as limiter,
        patch.object(webhook, "_get_events_log"),
        patch.object(webhook, "_get_metrics"),
        patch.object(webhook, "_get_correlation_store", return_value=store),
        patch.object(webhook, "_pr_marker_text_with_issue_fallback", return_value=(None, False)),
        patch.object(webhook, "_capture_invocation_event") as capture,
        patch("common.spawn_persona.spawn_persona", return_value=SimpleNamespace(success=True, message_id="one", block_reason=None)) as spawn,
        patch("common.github_actor_authority.check_actor", return_value=ActorDecision(True, "allowed", "labeler", "write", "MEMBER")) as authority,
    ):
        limiter.return_value.check_and_increment.return_value = rate
        yield SimpleNamespace(
            execute=lambda event_type, payload: webhook.handler(_event(event_type, payload), None),
            resolver=resolver, authority=authority, capture=capture, spawn=spawn,
            store=store,
        )


@pytest.mark.parametrize("event_type,action", sorted(DISPATCH_EVENTS))
def test_each_signed_dispatch_family_uses_actual_sender(run, event_type, action):
    payload = _payload(event_type, action)
    result = run.execute(event_type, payload)
    assert result["statusCode"] == 202, result
    run.spawn.assert_called_once()
    args = run.authority.call_args.kwargs
    assert args["sender_id"] == 56 and args["repository_id"] == 34
    assert args["floor"] == "MEMBER"
    assert args["require_write"] is (event_type == "issue_comment")
    assert args["association_actor_id"] == (99 if action in ("labeled", "synchronize", "ready_for_review") else 56)
    assert args["signed_association"] == ("COLLABORATOR" if event_type == "issue_comment" else "OWNER")


@pytest.mark.parametrize("event_type,action", sorted(DISPATCH_EVENTS))
def test_each_denied_dispatch_has_no_effect(run, event_type, action):
    run.authority.return_value = ActorDecision(False, "insufficient_permission")
    result = run.execute(event_type, _payload(event_type, action))
    assert result["statusCode"] == 403
    assert json.loads(result["body"])["outcome"] == "insufficient_permission"
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


def test_issue_comment_on_pr_uses_commenter_not_pr_author(run):
    payload = _payload("issue_comment", "created")
    payload["issue"]["pull_request"] = {"url": "https://api.github.com/repos/org/project/pulls/42"}
    payload["issue"]["user"] = {"id": 99}
    assert run.execute("issue_comment", payload)["statusCode"] == 202
    assert run.authority.call_args.kwargs["association_actor_id"] == 56
    assert run.authority.call_args.kwargs["require_write"] is True
    run.spawn.assert_called_once()


@pytest.mark.parametrize("engine", [False, True])
def test_comment_and_engine_share_actor_gate(run, engine):
    payload = _payload("issue_comment", "created", engine=engine)
    run.authority.return_value = ActorDecision(False, "insufficient_permission")
    denied = run.execute("issue_comment", payload)
    assert denied["statusCode"] == 403
    run.spawn.assert_not_called()
    run.capture.assert_not_called()
    run.authority.return_value = ActorDecision(True, "allowed", "labeler", "write")
    accepted = run.execute("issue_comment", payload)
    assert accepted["statusCode"] == (200 if engine else 202)
    if engine:
        assert run.capture.call_args.kwargs["engine_command"] is True
        run.spawn.assert_not_called()
    else:
        run.spawn.assert_called_once()


def test_comment_sender_must_match_comment_author(run):
    payload = _payload("issue_comment", "created")
    payload["comment"]["user"]["id"] = 99
    result = run.execute("issue_comment", payload)
    assert result["statusCode"] == 403
    assert json.loads(result["body"])["outcome"] == "actor_invalid"
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


def test_malformed_comment_author_cannot_dispatch(run):
    payload = _payload("issue_comment", "created")
    payload["comment"]["user"] = "not-an-account"
    response = run.execute("issue_comment", payload)
    assert response["statusCode"] == 403
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


@pytest.mark.parametrize("reason", ["actor_evidence_unavailable", "permission_unavailable", "association_unavailable"])
def test_transient_error_can_recover_on_redelivery(run, reason):
    run.authority.side_effect = [ActorDecision(False, reason), ActorDecision(True, "allowed", "labeler", "write")]
    payload = _payload("issue_comment", "created")
    unavailable = run.execute("issue_comment", payload)
    assert unavailable["statusCode"] == 503
    assert json.loads(unavailable["body"])["outcome"] == "eligibility_unavailable"
    run.spawn.assert_not_called()
    run.capture.assert_not_called()
    assert run.execute("issue_comment", payload)["statusCode"] == 202
    run.spawn.assert_called_once()


def test_unavailable_installation_record_is_not_an_absent_floor(run):
    run.resolver.last_tenant_item = None
    result = run.execute("issues", _payload("issues", "labeled"))
    assert result["statusCode"] == 503
    run.authority.assert_not_called()
    run.spawn.assert_not_called()


def test_invalid_configured_floor_is_terminal(run):
    run.resolver.last_tenant_item["min_author_association"] = "FIRST_TIMER"
    run.authority.return_value = ActorDecision(False, "eligibility_policy_invalid")
    response = run.execute("issues", _payload("issues", "opened"))
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["outcome"] == "eligibility_policy_invalid"
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


def test_bot_without_server_lineage_is_not_human_admission(run):
    payload = _payload("issues", "opened")
    payload["sender"] = {"id": 77, "type": "Bot", "login": "agent[bot]"}
    run.resolver.resolve.return_value = (
        ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot"), "ok"
    )
    response = run.execute("issues", payload)
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["outcome"] == "lineage_unavailable"
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


def test_invalid_floor_also_denies_machine_continuations(run):
    payload = _payload("issues", "opened")
    payload["sender"] = {"id": 77, "type": "Bot", "login": "agent[bot]"}
    run.resolver.resolve.return_value = (
        ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot"), "ok"
    )
    run.resolver.last_tenant_item["min_author_association"] = "unrecognized"
    response = run.execute("issues", payload)
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["outcome"] == "eligibility_policy_invalid"
    run.spawn.assert_not_called()


def test_verified_bot_pr_continuation_keeps_persona_path(run):
    payload = _payload("pull_request", "opened")
    payload["sender"] = {"id": 77, "type": "Bot", "login": "agent[bot]"}
    run.resolver.resolve.return_value = (
        ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot"), "ok"
    )
    trusted_chain = {
        "correlation_id": "verified-server-chain",
        "is_new_chain": False,
        "is_human_rooted": True,
        "root_human_id": "user-56",
        "parent_invocation_id": "server-parent",
        "chain_depth": 1,
    }
    server_chain = {
        "correlation_id": "verified-server-chain",
        "tenant_id": "org",
        "installation_id": "12",
        "repo": "org/project",
        "root_human_id": "user-56",
        "is_human_rooted": True,
        "chain_depth": Decimal(1),
    }
    with (
        patch.object(webhook, "determine_correlation", return_value=trusted_chain),
        patch.object(webhook, "_resolve_chain_record", return_value=server_chain),
    ):
        result = run.execute("pull_request", payload)
    assert result["statusCode"] == 202
    run.authority.assert_not_called()
    run.spawn.assert_called_once()


def test_invalid_signature_never_checks_actor(run):
    payload = _payload("issues", "labeled")
    event = _event("issues", payload)
    event["body"] += " "
    response = webhook.handler(event, None)
    assert response["statusCode"] == 401
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


@pytest.mark.parametrize("event_type,action", [
    ("issues", "edited"), ("issue_comment", "edited"),
    ("pull_request_review", "submitted"), ("installation", "suspend"),
])
def test_no_op_events_do_not_query_actor_or_dispatch(run, event_type, action):
    payload = _payload(event_type, action)
    result = run.execute(event_type, payload)
    assert result["statusCode"] == 200
    run.authority.assert_not_called()
    run.spawn.assert_not_called()


@pytest.mark.parametrize("event_type,action", sorted(DISPATCH_EVENTS))
def test_proven_human_roots_each_event_family_without_borrowing_author(run, event_type, action):
    assert run.execute(event_type, _payload(event_type, action))["statusCode"] == 202
    lineage = run.spawn.call_args.kwargs["correlation_ctx"]
    assert lineage["correlation_id"]
    assert lineage["root_human_id"] == "user-56"
    assert lineage["is_human_rooted"] is True
    assert lineage["lineage_verified"] is True
    assert lineage["parent_invocation_id"] is None
    assert lineage["chain_depth"] == 0


def test_proven_human_protected_envelope_uses_actual_sender(run, monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    payload = _payload("issues", "labeled")
    assert payload["issue"]["user"]["id"] != payload["sender"]["id"]
    assert run.execute("issues", payload)["statusCode"] == 202
    event = run.spawn.call_args.kwargs["trusted_human_event"]
    assert event.human_id == "user-56" and event.repo == "org/project"
    assert event.reference_id.startswith("github-event:")


@pytest.mark.parametrize("method", ["", "self_asserted", "unknown"])
def test_unproven_human_does_not_mint_an_issue_root(run, method):
    run.resolver.resolve.return_value = (
        ResolvedIdentity("org", "org", "user-56", "strict", verification_method=method),
        "ok",
    )
    response = run.execute("issues", _payload("issues", "labeled"))
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["outcome"] == "identity_unproven"
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


def test_missing_bot_pointer_has_no_human_root_or_depth_reset(run):
    from intent_parser import MAX_CHAIN_DEPTH

    bot = ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot")
    payload = _payload("issues", "opened")
    payload["sender"] = {"id": 77, "type": "Bot", "login": "agent[bot]"}
    correlation = webhook.determine_correlation(payload, bot, "channel-42")
    assert correlation["root_human_id"] == ""
    assert correlation["is_human_rooted"] is False
    assert correlation["is_new_chain"] is False
    assert correlation["parent_invocation_id"] is None
    assert correlation["chain_depth"] >= MAX_CHAIN_DEPTH
    run.resolver.resolve.return_value = (bot, "ok")
    result = run.execute("issues", payload)
    assert result["statusCode"] == 503
    assert json.loads(result["body"])["outcome"] == "lineage_unavailable"
    run.spawn.assert_not_called()
    run.capture.assert_not_called()


def test_missing_chain_row_keeps_known_high_depth_without_a_root():
    bot = ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot")
    proposed = {
        "correlation_id": "missing-chain",
        "root_human_id": "claimed-human",
        "is_human_rooted": True,
        "is_new_chain": False,
        "parent_invocation_id": "unverified-parent",
        "chain_depth": 29,
    }
    with patch.object(webhook, "_resolve_chain_record", return_value=None):
        result = webhook._verify_github_lineage(proposed, _payload("issues", "opened"), bot)
    assert result["correlation_id"] == "missing-chain"
    assert result["root_human_id"] == ""
    assert result["parent_invocation_id"] is None
    assert result["chain_depth"] >= 29
    assert result["lineage_verified"] is False


@pytest.mark.parametrize("human_root", ["user-56", ""])
def test_server_bound_bot_continuation_keeps_real_provenance(run, human_root):
    payload = _payload("issues", "opened")
    payload["sender"] = {"id": 77, "type": "Bot", "login": "agent[bot]"}
    run.resolver.resolve.return_value = (
        ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot"), "ok"
    )
    chain = {
        "correlation_id": "server-chain",
        "tenant_id": "org",
        "installation_id": "12",
        "repo": "org/project",
        "root_human_id": human_root,
        "is_human_rooted": bool(human_root),
        "chain_depth": Decimal(3),
    }
    run.store.read_pointer.return_value = {
        "correlation_id": "server-chain", "triggering_invocation_id": "server-parent"
    }
    with patch.object(webhook, "_resolve_chain_record", return_value=chain):
        result = run.execute("issues", payload)
    assert result["statusCode"] == 202
    lineage = run.spawn.call_args.kwargs["correlation_ctx"]
    assert lineage["lineage_verified"] is True
    assert lineage["is_new_chain"] is False
    assert lineage["root_human_id"] == human_root
    assert lineage["is_human_rooted"] is bool(human_root)
    assert lineage["chain_depth"] == 3
    assert lineage["parent_invocation_id"] == "server-parent"
    run.authority.assert_not_called()


@pytest.mark.parametrize("variant", ["missing", "tenant", "installation", "repository", "depth"])
def test_untrusted_or_unresolvable_bot_chain_cannot_publish(run, variant):
    payload = _payload("pull_request", "opened")
    payload["sender"] = {"id": 77, "type": "Bot", "login": "agent[bot]"}
    run.resolver.resolve.return_value = (
        ResolvedIdentity("org", "org", "bot-77", "strict", user_kind="bot"), "ok"
    )
    proposed = {
        "correlation_id": "other-chain", "is_new_chain": False,
        "root_human_id": "other-human", "is_human_rooted": True,
        "parent_invocation_id": "claimed-parent", "chain_depth": 2,
    }
    chain = {
        "tenant_id": "org", "installation_id": "12", "repo": "org/project",
        "root_human_id": "user-56", "is_human_rooted": True,
        "chain_depth": Decimal(5),
    }
    if variant == "tenant":
        chain["tenant_id"] = "other"
    elif variant == "installation":
        chain["installation_id"] = "99"
    elif variant == "repository":
        chain["repo"] = "other/project"
    elif variant == "depth":
        chain["chain_depth"] = True
    with (
        patch.object(webhook, "determine_correlation", return_value=proposed),
        patch.object(webhook, "_resolve_chain_record", return_value=None if variant == "missing" else chain),
        patch("agent_trigger._repo_in_tenant", side_effect=lambda repo, *args: repo.startswith("org/")),
    ):
        response = run.execute("pull_request", payload)
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["outcome"] == "lineage_unavailable"
    run.authority.assert_not_called()
    run.spawn.assert_not_called()
    run.capture.assert_not_called()
