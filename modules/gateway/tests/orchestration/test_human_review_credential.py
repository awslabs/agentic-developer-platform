"""A human's standalone PR review reaches the real broker and reviewer App."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.grants import AuthorityReference
from tests.orchestration.test_runtime_policy import (
    APPROVER,
    GITHUB,
    INSTALLATION_A,
    ORG_A,
    REPO,
)
from tests.orchestration.test_runtime_policy import assignment as assignment_fixture
from tests.orchestration.test_runtime_policy import broker_client as broker_fixture
from tests.orchestration.test_runtime_policy import engine as engine_fixture
from tests.orchestration.test_runtime_policy import healthy_policy_reservations as reservations_fixture
from tests.orchestration.test_runtime_policy import policy_budget_initializers as initializers_fixture
from tests.orchestration.test_runtime_policy import session as session_fixture

assignment = assignment_fixture
broker_client = broker_fixture
engine = engine_fixture
healthy_policy_reservations = reservations_fixture
policy_budget_initializers = initializers_fixture
session = session_fixture


@pytest.fixture
def human_review(assignment, broker_client):
    reference = "github-event:verified-human-comment"
    assignment.grant = replace(
        assignment.grant,
        authority=AuthorityReference("github_event", reference, APPROVER, ORG_A),
        flow_id=reference,
    )
    assignment.execution.clear()
    assignment.execution.update(
        {
            "invocation_id": {"S": "worker"},
            "tenant_id": {"S": ORG_A},
            "flow_id": {"S": reference},
            "persona": {"S": "agent-codex-reviewer"},
            "current_attempt": {"N": "1"},
            "status": {"S": "active"},
            "chain_depth": {"N": "0"},
            "repo": {"S": REPO},
            "issue_number": {"N": "8172"},
            "installation_id": {"N": str(INSTALLATION_A)},
        }
    )
    envelope = {
        "message_id": "worker",
        "tenant_id": ORG_A,
        "channel": "github",
        "persona": "agent-codex-reviewer",
        "actor": {"user_id": APPROVER, "org_id": ORG_A, "github_id": 123},
        "intent": {"trigger": "mentioned", "persona": "agent-codex-reviewer"},
        "source_ref": {"repo": REPO, "issue": 8172, "installation_id": INSTALLATION_A},
        "payload": {
            "sender": {"type": "User", "id": 123},
            "comment": {"body": "@agent-codex-reviewer review this PR", "user": {"type": "User", "id": 123}},
            "issue": {"number": 8172, "pull_request": {"url": "https://api.github.com/repos/aws-e/adp/pulls/8172"}},
        },
    }
    broker_client.journal.update(
        {"invocation_id": {"S": "worker"}, "body": {"S": json.dumps(envelope)}, "envelope_digest": {"S": envelope_digest(envelope)}}
    )
    assignment.execution["envelope_digest"] = {"S": envelope_digest(envelope)}
    return envelope


async def test_human_root_pr_reviewer_gets_scoped_reviewer_app(human_review, broker_client):
    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "identity": "review"})

    assert response.status_code == 200, response.text
    assert response.json()["identity"] == "review"
    broker_client.reviewer.assert_awaited_once_with(ORG_A)
    assert broker_client.mint.await_args.args[:3] == ("review-app-test", "review-test-key", INSTALLATION_A + 1)
    assert broker_client.mint.await_args.kwargs == {
        "repositories": ["adp"],
        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
    }


async def test_human_reviewer_bootstrap_keeps_authoring_identity(human_review, broker_client):
    from src.knowledge.github_app_service import AGENT_RUN_PERMISSIONS

    response = await broker_client.client.post(GITHUB, json=broker_client.body)

    assert response.status_code == 200, response.text
    assert response.json()["identity"] == "default"
    broker_client.reviewer.assert_not_awaited()
    assert broker_client.mint.await_args.args[:2] == ("app-test", "test-key")
    assert broker_client.mint.await_args.kwargs["permissions"] == AGENT_RUN_PERMISSIONS


@pytest.mark.parametrize(
    "case",
    [
        "developer",
        "child",
        "parent",
        "wrong_tenant",
        "wrong_flow",
        "wrong_principal",
        "wrong_attempt",
        "wrong_human",
        "wrong_org",
        "wrong_repo_scope",
        "wrong_repo",
        "wrong_issue",
        "wrong_installation",
        "completed",
        "revoked",
        "expired",
        "short",
        "no_expiry",
        "service_authority",
        "unknown_authority",
        "body_persona",
        "missing_journal",
        "wrong_journal_run",
        "bad_json",
        "array_json",
        "tampered_body",
        "wrong_journal_digest",
        "wrong_execution_digest",
    ],
)
async def test_human_review_refusals_never_mint(human_review, assignment, broker_client, case):
    body = {**broker_client.body, "identity": "review"}
    execution = assignment.execution
    if case in {"developer", "body_persona"}:
        execution["persona"] = {"S": "agent-codex-developer"}
        if case == "body_persona":
            body.update(
                persona="agent-codex-reviewer", authorized_action="review", review_only=True, dispatch_envelope=human_review, pod_uid="victim-pod"
            )
    elif case == "child":
        execution["chain_depth"] = {"N": "1"}
    elif case == "parent":
        execution["parent_principal"] = {"S": "another#1"}
    elif case == "wrong_tenant":
        execution["tenant_id"] = {"S": "other"}
    elif case == "wrong_flow":
        execution["flow_id"] = {"S": "other"}
    elif case == "wrong_principal":
        assignment.grant = replace(assignment.grant, principal="another#1")
    elif case == "wrong_attempt":
        execution["current_attempt"] = {"N": "2"}
    elif case in {"wrong_human", "wrong_org", "service_authority", "unknown_authority"}:
        changes = {
            "wrong_human": {"human_id": "other"},
            "wrong_org": {"org_id": "other"},
            "service_authority": {"kind": "service_policy"},
            "unknown_authority": {"kind": "made-up"},
        }
        assignment.grant = replace(assignment.grant, authority=replace(assignment.grant.authority, **changes[case]))
    elif case == "wrong_repo_scope":
        assignment.grant = replace(assignment.grant, repo_scope=frozenset({"aws-e/other"}))
    elif case == "wrong_repo":
        body["repo_name"] = "other"
    elif case == "wrong_issue":
        execution["issue_number"] = {"N": "999"}
    elif case == "wrong_installation":
        body["installation_id"] += 1
    elif case == "completed":
        execution["status"] = {"S": "completed"}
    elif case == "revoked":
        assignment.grant = replace(assignment.grant, revoked=True)
    elif case in {"expired", "short", "no_expiry"}:
        expiry = None if case == "no_expiry" else datetime.now(UTC) + timedelta(minutes=-1 if case == "expired" else 30)
        assignment.grant = replace(assignment.grant, expires_at=expiry)
    elif case == "missing_journal":
        broker_client.journal.clear()
    elif case == "wrong_journal_run":
        broker_client.journal["invocation_id"] = {"S": "victim"}
    elif case in {"bad_json", "array_json"}:
        broker_client.journal["body"] = {"S": "{" if case == "bad_json" else "[]"}
    elif case == "tampered_body":
        human_review["actor"]["user_id"] = "other"
        broker_client.journal["body"] = {"S": json.dumps(human_review)}
    elif case == "wrong_journal_digest":
        broker_client.journal["envelope_digest"] = {"S": "0" * 64}
    elif case == "wrong_execution_digest":
        execution["envelope_digest"] = {"S": "0" * 64}

    response = await broker_client.client.post(GITHUB, json=body)

    assert response.status_code in {403, 404}, response.text
    broker_client.reviewer.assert_not_awaited()
    broker_client.mint.assert_not_awaited()


@pytest.mark.parametrize(
    "case", ["not_pr", "not_mention", "bot_comment", "different_comment_author", "wrong_actor", "wrong_persona", "wrong_pr", "missing_payload"]
)
async def test_only_original_human_pr_intent_authorizes_review(human_review, assignment, broker_client, case):
    if case == "not_pr":
        human_review["payload"]["issue"].pop("pull_request")
    elif case == "not_mention":
        human_review["intent"]["trigger"] = "labeled"
    elif case == "bot_comment":
        human_review["payload"]["comment"]["user"]["type"] = "Bot"
    elif case == "different_comment_author":
        human_review["payload"]["comment"]["user"]["id"] = 999
    elif case == "wrong_actor":
        human_review["actor"]["user_id"] = "other"
    elif case == "wrong_persona":
        human_review["persona"] = "agent-codex-developer"
    elif case == "wrong_pr":
        human_review["payload"]["issue"]["number"] = 999
    elif case == "missing_payload":
        human_review["payload"] = None
    # This is a different, validly recorded launch, not a worker's modified body.
    digest = {"S": envelope_digest(human_review)}
    broker_client.journal.update(body={"S": json.dumps(human_review)}, envelope_digest=digest)
    assignment.execution["envelope_digest"] = digest

    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "identity": "review", "review_only": True})

    assert response.status_code == 404, response.text
    broker_client.reviewer.assert_not_awaited()
    broker_client.mint.assert_not_awaited()


@pytest.mark.parametrize("during", ["key_lookup", "mint", "audit"])
async def test_human_grant_revocation_during_mint_withholds_token(human_review, assignment, broker_client, monkeypatch, during):
    def revoke():
        assignment.grant = replace(assignment.grant, revoked=True)

    if during == "key_lookup":

        async def key_lookup(*_):
            revoke()
            return "review-app-test", "review-test-key", INSTALLATION_A + 1

        broker_client.reviewer.side_effect = key_lookup
    elif during == "mint":

        async def mint(*_, **__):
            revoke()
            return "scoped-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        broker_client.mint.side_effect = mint
    else:
        from src.internal.routes import _write_audit

        async def audit(*args, **kwargs):
            await _write_audit(*args, **kwargs)
            revoke()

        monkeypatch.setattr("src.internal.routes._write_audit", audit)

    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "identity": "review"})

    assert response.status_code == 404, response.text
    assert "scoped-token" not in response.text
    if during == "key_lookup":
        broker_client.mint.assert_not_awaited()
        broker_client.revoke.assert_not_awaited()
    else:
        broker_client.revoke.assert_awaited_once_with("scoped-token")


async def test_human_review_rejects_provider_token_past_original_grant(human_review, assignment, broker_client):
    broker_client.mint.return_value = ("must-not-escape", (assignment.grant.expires_at + timedelta(seconds=1)).isoformat())

    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "identity": "review"})

    assert response.status_code == 502, response.text
    assert "must-not-escape" not in response.text
    broker_client.revoke.assert_awaited_once_with("must-not-escape")
