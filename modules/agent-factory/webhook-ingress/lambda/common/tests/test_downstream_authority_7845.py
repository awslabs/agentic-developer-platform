"""SCN-10: lineage, vault-owner and generation boundaries."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github.handler import _verify_github_lineage

from common.service_authority import VerifiedServiceEvent
from common.spawn_persona import (
    MAX_CHAIN_DEPTH,
    _compute_authorized_user_id,
    spawn_persona,
)


def context(depth=1, **overrides):
    return {
        "correlation_id": "chain",
        "root_human_id": "human",
        "is_human_rooted": True,
        "parent_invocation_id": "parent",
        "is_new_chain": False,
        "chain_depth": depth,
        **overrides,
    }


def spawn(correlation_ctx, *, bot=True, event_type="issue_comment", service_event=None):
    sender = (
        {"id": 90, "login": "agent[bot]", "type": "Bot"}
        if bot
        else {"id": 91, "login": "writer", "type": "User"}
    )
    identity = SimpleNamespace(
        user_kind="bot" if bot else "human",
        bot_kind="operations" if bot else "",
        user_id="bot" if bot else "human",
        org_id="tenant",
    )
    with (
        patch("common.spawn_persona._capture_invocation_event") as capture,
        patch("common.spawn_persona._capture_blocked_event") as blocked,
        patch("common.spawn_persona._write_pointer_and_provenance"),
        patch("common.spawn_persona._get_max_credential_chain_depth", return_value=5),
        patch(
            "common.sqs_publisher.publish_envelope", return_value="message"
        ) as publish,
    ):
        result = spawn_persona(
            persona="developer",
            correlation_ctx=correlation_ctx,
            channel_key="github:issue:1",
            resolved_identity=identity,
            tenant_id="tenant",
            actor_user_id=identity.user_id,
            actor_org_id="tenant",
            sender=sender,
            event_type=event_type,
            action="created",
            installation_id=11,
            repo="team/repo",
            payload={"issue": {"number": 1}, "sender": sender},
            intent_trigger="mentioned",
            trusted_service_event=service_event,
        )
        return result, capture, blocked, publish


@pytest.fixture(autouse=True)
def legacy_authority(monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")


@pytest.mark.parametrize("depth", [None, True, False, -1, "bad", 1.5])
@pytest.mark.parametrize("bot", [True, False])
def test_invalid_continuation_depth_blocks_before_publication(depth, bot):
    result, capture, blocked, publish = spawn(context(depth), bot=bot)
    assert result.block_reason == "invalid_chain_depth"
    capture.assert_not_called()
    publish.assert_not_called()
    blocked.assert_called_once()


@pytest.mark.parametrize("depth", [None, True, -1, "bad"])
def test_invalid_credential_horizon_cannot_reset(depth):
    result, capture, _, publish = spawn(context(1, credential_chain_depth=depth))
    assert result.block_reason == "invalid_chain_depth"
    capture.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("depth", [0, MAX_CHAIN_DEPTH - 1, MAX_CHAIN_DEPTH])
def test_only_successful_spawn_increments_once(depth):
    ctx = context(depth, credential_chain_depth=depth + 1)
    result, capture, blocked, publish = spawn(ctx)
    assert ctx["chain_depth"] == depth
    if depth == MAX_CHAIN_DEPTH:
        assert result.block_reason == "chain_depth_exceeded"
        assert blocked.call_args.kwargs["correlation_ctx"]["chain_depth"] == depth
        publish.assert_not_called()
    else:
        assert result.success
        correlation = publish.call_args.args[0]["correlation"]
        assert correlation["chain_depth"] == depth + 1
        assert correlation["credential_chain_depth"] == depth + 2
        assert capture.call_args.kwargs["correlation_ctx"]["chain_depth"] == depth + 1


def test_service_root_and_unproven_root_have_no_vault_owner():
    for ctx in (
        context(0, is_new_chain=True, is_human_rooted=False, root_human_id="service"),
        context(
            0, is_new_chain=True, is_human_rooted=False, root_human_id="human-hint"
        ),
    ):
        result, capture, _, publish = spawn(ctx, event_type="eventbridge")
        assert result.success
        assert publish.call_args.args[0]["correlation"]["chain_depth"] == 0
        assert publish.call_args.args[0]["correlation"]["is_human_rooted"] is False
        assert (
            _compute_authorized_user_id(
                correlation_ctx=capture.call_args.kwargs["correlation_ctx"],
                cognito_sub="human-hint",
                max_credential_chain_depth=5,
            )
            == ""
        )


def test_protected_service_root_needs_verified_event_and_no_vault(monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    root = context(0, is_new_chain=True, is_human_rooted=False, root_human_id="service")
    result, capture, _, publish = spawn(root, event_type="eventbridge")
    assert result.block_reason == "delegated_dispatch_required"
    capture.assert_not_called()
    publish.assert_not_called()
    event = VerifiedServiceEvent(
        "event-id",
        "service",
        "tenant",
        "team/repo",
        "arn:aws:events:region:account:rule/r",
    )
    with (
        patch(
            "common.sqs_publisher.prepare_envelope",
            side_effect=lambda envelope: envelope,
        ),
        patch(
            "common.service_authority.provision_service_dispatch",
            side_effect=lambda envelope, event: envelope,
        ) as provision,
    ):
        result, capture, _, publish = spawn(
            root, event_type="eventbridge", service_event=event
        )
    assert result.success
    assert provision.call_args.kwargs["event"] is event
    assert publish.call_args.args[0]["correlation"]["is_human_rooted"] is False
    assert capture.call_args.kwargs["correlation_ctx"]["credential_chain_depth"] == 0


@pytest.mark.parametrize(
    "ctx",
    [
        context(1, credential_chain_depth=5),
        context(1, is_human_rooted=False, root_human_id="human-hint"),
        context(1, root_human_id=""),
        context(-1, credential_chain_depth=-1),
    ],
)
def test_vault_owner_requires_proven_root_and_valid_depth(ctx):
    assert (
        _compute_authorized_user_id(
            correlation_ctx=ctx, cognito_sub="human-hint", max_credential_chain_depth=5
        )
        == ""
    )


@pytest.mark.parametrize("horizon", [4, 5])
def test_server_horizon_survives_webhook_continuation(horizon):
    current = context(1, credential_chain_depth=2)
    chain = context(2, credential_chain_depth=horizon, repo="team/repo")
    with (
        patch("github.handler._resolve_chain_record", return_value=chain),
        patch("github.handler._lineage_context_mismatch", return_value=None),
        patch("agent_trigger._repo_in_tenant", return_value=True),
    ):
        verified = _verify_github_lineage(
            current, {"sender": {"type": "Bot"}}, SimpleNamespace(user_kind="bot")
        )
    assert verified["lineage_verified"] is True
    assert verified["credential_chain_depth"] == horizon
    result, capture, _, publish = spawn(verified)
    assert result.success
    assert (
        publish.call_args.args[0]["correlation"]["credential_chain_depth"]
        == horizon + 1
    )
    assert (
        _compute_authorized_user_id(
            correlation_ctx=capture.call_args.kwargs["correlation_ctx"],
            cognito_sub="",
            max_credential_chain_depth=5,
        )
        == ""
    )


@pytest.mark.parametrize("invalid", [True, -1, "bad"])
def test_invalid_server_horizon_withholds_lineage(invalid):
    with (
        patch(
            "github.handler._resolve_chain_record",
            return_value=context(1, credential_chain_depth=invalid),
        ),
        patch("github.handler._lineage_context_mismatch", return_value=None),
    ):
        verified = _verify_github_lineage(
            context(1), {"sender": {"type": "Bot"}}, SimpleNamespace(user_kind="bot")
        )
    assert verified["lineage_verified"] is False
    assert verified["is_human_rooted"] is False
