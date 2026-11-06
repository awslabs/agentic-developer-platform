"""Security and compatibility controls for issue #5365.

Per-lineage dispatch belongs on the protected gateway route, where the run
credential identifies the caller and the server derives its parent. The legacy
``/agent/trigger`` route authenticates only one IAM role shared by every worker,
so it may use the server-observed chain head but must refuse a body-selected
ancestor. This preserves the old recursion bound during rollout without
reintroducing the coordinator fix as a shallow-ancestor bypass.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_trigger import handle_agent_trigger
from common.marker_verify import reset_key_cache
from common.spawn_persona import (
    DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH,
    MAX_CHAIN_DEPTH,
    _compute_authorized_user_id,
    spawn_persona,
)

REAL_KEY = "a-real-generated-key-32-bytes!!!"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:trigger-depth"
CHAIN = "21b41962-9cf8-47f4-bc01-83a329fc0010"
CALLER = "inv-coordinator-5134"


@pytest.fixture(autouse=True)
def _clean_env():
    reset_key_cache()
    saved = os.environ.pop("REQUIRE_SIGNED_PROVENANCE", None)
    yield
    os.environ.pop("REQUIRE_SIGNED_PROVENANCE", None)
    if saved is not None:
        os.environ["REQUIRE_SIGNED_PROVENANCE"] = saved
    reset_key_cache()


def _row(*, event_id: str, chain_depth: int, **overrides) -> dict:
    row = {
        "event_id": event_id,
        "arrived_at": "2026-09-17T22:42:56Z",
        "tenant_id": "aws-e",
        "correlation_id": CHAIN,
        "repo": "aws-e/adp",
        "root_human_id": "user-human-operator",
        "is_human_rooted": True,
        "chain_depth": chain_depth,
        "status": "webhook_received",
    }
    row.update(overrides)
    return row


def _body(**overrides) -> dict:
    body = {
        "correlation_id": CHAIN,
        "parent_invocation_id": CALLER,
        "persona": "developer",
        "target": {"repo": "aws-e/adp", "issue": 5337},
        "reason": "story needs a developer",
    }
    body.update(overrides)
    return body


def _event(body: dict) -> dict:
    return {
        "resource": "/agent/trigger",
        "httpMethod": "POST",
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "requestContext": {
            "identity": {
                "userArn": (
                    "arn:aws:sts::123456789012:assumed-role/"
                    "adp-dev-agent-worker-role/shared-session"
                )
            }
        },
    }


def _sign(invocation_id: str, depth: int) -> str:
    value = f"{CHAIN}:user-human-operator:true:{invocation_id}:{depth}"
    digest = hmac.new(REAL_KEY.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _marker(invocation_id: str, depth: int) -> str:
    return (
        f"<!-- adp-correlation:{CHAIN} adp-root-human:user-human-operator "
        f"adp-is-human-rooted:true adp-invocation:{invocation_id} "
        f"adp-chain-depth:{depth} adp-sig:{_sign(invocation_id, depth)} -->"
    )


def _secret_client() -> MagicMock:
    client = MagicMock()

    def get(**kwargs):
        if kwargs.get("VersionStage") == "AWSCURRENT":
            return {"SecretString": REAL_KEY}
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "none"}},
            "GetSecretValue",
        )

    client.get_secret_value.side_effect = get
    return client


class TestLegacyRouteRetired:
    """Even valid signed lineage cannot reopen shared-IAM dispatch."""

    @pytest.mark.parametrize("signed", [False, True])
    @pytest.mark.parametrize("depth", [0, MAX_CHAIN_DEPTH])
    def test_ancestor_selection_cannot_dispatch(self, signed, depth, monkeypatch):
        body = _body(parent_invocation_id="published-ancestor", chain_depth=depth)
        if signed:
            body["provenance_marker"] = _marker("published-ancestor", depth)
        monkeypatch.setenv("REQUIRE_SIGNED_PROVENANCE", str(signed).lower())
        with (
            patch("agent_trigger._resolve_chain") as resolve,
            patch("common.spawn_persona.spawn_persona") as spawn,
        ):
            response = handle_agent_trigger(_event(body), None)
        assert response["statusCode"] == 410
        assert json.loads(response["body"])["error"] == "legacy_agent_trigger_retired"
        resolve.assert_not_called()
        spawn.assert_not_called()


@dataclass
class _Identity:
    tenant_id: str = "aws-e"
    org_id: str = "aws-e"
    user_id: str = "agent:parent"
    user_kind: str = "bot"
    bot_kind: str = "operations"


def _spawn(ctx: dict, *, issue: int) -> dict:
    captured: list[dict] = []

    def publish(envelope: dict) -> str:
        captured.append(envelope)
        return f"message-{issue}"

    with (
        patch("common.spawn_persona._write_pointer_and_provenance"),
        patch("common.spawn_persona._capture_invocation_event"),
        patch(
            "common.sqs_publisher.publish_envelope",
            side_effect=publish,
        ),
    ):
        result = spawn_persona(
            persona="developer",
            correlation_ctx=ctx,
            channel_key=f"github:repo=aws-e/adp,issue={issue}",
            resolved_identity=_Identity(),
            tenant_id="aws-e",
            actor_user_id="agent:parent",
            actor_org_id="aws-e",
            sender={"login": "agent", "id": 1, "type": "User"},
            event_type="agent_trigger",
            action="trigger",
            installation_id=1247,
            repo="aws-e/adp",
            payload={
                "issue": {"number": issue, "title": "test"},
                "repository": {"full_name": "aws-e/adp"},
            },
            intent_trigger="agent_trigger",
        )

    assert result.success
    return captured[0]


def test_credential_horizon_survives_two_real_spawn_and_queue_hops():
    """The conservative counter is serialized, monotonic, and never launders."""
    first = _spawn(
        {
            "correlation_id": CHAIN,
            "root_human_id": "user-human-operator",
            "is_human_rooted": True,
            "is_new_chain": False,
            "parent_invocation_id": "depth-0-ancestor",
            "chain_depth": 0,
            "credential_chain_depth": MAX_CHAIN_DEPTH,
        },
        issue=5337,
    )
    assert first["correlation"]["chain_depth"] == 1
    assert first["correlation"]["credential_chain_depth"] == MAX_CHAIN_DEPTH + 1

    second_ctx = {
        **first["correlation"],
        "is_new_chain": False,
        "parent_invocation_id": first["message_id"],
    }
    second = _spawn(second_ctx, issue=5338)
    assert second["correlation"]["chain_depth"] == 2
    assert second["correlation"]["credential_chain_depth"] == MAX_CHAIN_DEPTH + 2
    assert (
        _compute_authorized_user_id(
            correlation_ctx=second["correlation"],
            cognito_sub="",
            max_credential_chain_depth=DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH,
        )
        == ""
    )
