"""Approved user permissions through real policy/SQL/broker endpoint boundaries."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, select

from src.orchestration.execution_policy import Action, ExecutionPolicy, UserCredentialAuthority, stamp_policy
from src.orchestration.models import OrchestrationAcceptedPlan
from src.orchestration.user_credentials import validate_user_credential_authority
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.onboarding import TenantMembership
from src.shared.models.vault import UserCredential
from src.shared.services.credential_resolver import CredentialNotFoundError
from tests.orchestration.test_runtime_policy import APPROVER, ORG_A, _limits, _policy
from tests.orchestration.test_runtime_policy import assignment as assignment_fixture
from tests.orchestration.test_runtime_policy import engine as engine_fixture
from tests.orchestration.test_runtime_policy import healthy_policy_reservations as reservations_fixture
from tests.orchestration.test_runtime_policy import policy_budget_initializers as initializers_fixture
from tests.orchestration.test_runtime_policy import session as session_fixture

engine = engine_fixture
session = session_fixture
assignment = assignment_fixture
healthy_policy_reservations = reservations_fixture
policy_budget_initializers = initializers_fixture
ROLE = "arn:aws:iam::222222222222:role/CustomerDeploy"


def authority(**changes):
    return UserCredentialAuthority(
        permission_mode="user_configured",
        lifetime="provider_managed",
        **{"vault_credential_ids": ["approved-key"], "aws_role_arns": [ROLE], "actions": [Action.DEVELOP], **changes},
    )


def policy(**changes):
    return _policy(schema_version=2, user_credentials=authority(), limits=_limits(max_wall_clock_seconds=7200), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"permission_mode": "scoped"},
        {"lifetime": "immediate_revocation"},
        {"aws_role_arns": [ROLE + "*"]},
        {"vault_credential_ids": [], "aws_role_arns": []},
    ],
)
def test_cannot_disguise_user_permissions_as_narrow_or_instantly_revocable(changes):
    raw = authority().model_dump(mode="json")
    with pytest.raises(ValidationError):
        UserCredentialAuthority.model_validate({**raw, **changes})


def test_old_policy_documents_and_hash_content_are_unchanged():
    old = _policy()
    assert old.schema_version == 1 and "user_credentials" not in old.model_dump(mode="json")
    assert ExecutionPolicy.model_validate(old.model_dump()).model_dump() == old.model_dump()
    with pytest.raises(ValidationError, match="schema_version 2"):
        _policy(user_credentials=authority())


@pytest.fixture
async def broker(session, assignment, monkeypatch):
    from src.internal import assume_role_routes, credential_routes, task_credentials

    cred = UserCredential(
        id="approved-key",
        org_id=ORG_A,
        user_id=APPROVER,
        service="fixture",
        label="deployment",
        credential_type="api_key",
        secret_arn="fixture-secret",
    )
    session.add(cred)
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.plan_document = {
        **plan.plan_document,
        "execution_policy": stamp_policy(policy(), principal_id=APPROVER, org_id=ORG_A).model_dump(mode="json"),
    }
    await session.commit()
    assignment.execution["arrived_at"] = {"S": "2026-09-15T00:00:00Z"}
    events = MagicMock()
    events.get_item.return_value = {"Item": {"authorized_user_id": {"S": APPROVER}}}
    runtime = SimpleNamespace(
        authenticate=lambda *_: (None, SimpleNamespace(tenant_id=ORG_A, invocation_id="worker", principal="worker#1"), None, assignment.grant),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=lambda *_: assignment.execution, client=events),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            pass

    settings = SimpleNamespace(
        enforce_credential_binding=False,
        webhook_events_table="events",
        vault_raw_read_enabled=True,
        vault_materialization_bucket="fixture-files",
        aws_region="us-east-1",
        vault_proxy_require_https=True,
        vault_proxy_host_allowlist="api.example.test",
        vault_enforce_credential_host_binding=False,
    )
    identity = SimpleNamespace(scope="internal", user_id="worker-identity", credential_scopes=["credential:raw-read", "credential:materialize"])
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionContext)
    monkeypatch.setattr("src.shared.config.get_settings", lambda: settings)
    for module in (credential_routes, assume_role_routes, task_credentials):
        monkeypatch.setitem(module.verify_internal_or_irsa.__globals__, "extract_iam_identity_from_headers", lambda _: identity)
    for module in (credential_routes, assume_role_routes):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(
            module, "resolve_credential_binding", lambda **_: SimpleNamespace(resolved_user_id=APPROVER, from_registry=True, drift_detected=False)
        )
    sm = MagicMock()
    sm.get_secret.return_value = "provider-key"
    app = FastAPI()
    for module in (credential_routes, assume_role_routes, task_credentials):
        app.include_router(module.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[credential_routes.get_secrets_manager] = lambda: sm
    app.dependency_overrides[assume_role_routes.get_secrets_manager] = lambda: sm
    return SimpleNamespace(
        client=TestClient(app),
        db=session,
        cred=cred,
        plan=plan,
        assignment=assignment,
        runtime=runtime,
        settings=settings,
        identity=identity,
        sm=sm,
        headers={
            "X-Caller-Identity": "worker",
            "X-Adp-Run-Credential": "proof",
            "X-Adp-Workload-Token": "pod",
            "X-Agent-Scopes": "credential:raw-read,credential:materialize",
        },
        body={
            "user_id": APPROVER,
            "invocation_id": "worker",
            "service": "fixture",
            "label": "deployment",
            "agent_id": "developer",
            "task_id": "work",
        },
    )


async def test_raw_key_rotation_and_audit_use_approved_provider_permissions(broker):
    broker.sm.get_secret.side_effect = ["first-key", "rotated-key"]
    for key in ("first-key", "rotated-key"):
        response = broker.client.post("/internal/v1/credential-raw-read", json=broker.body, headers=broker.headers)
        assert response.status_code == 200, response.text
        assert response.json()["value"] == key
    logs = (await broker.db.scalars(select(AuditLog))).all()
    assert len(logs) == 2
    assert all(row.details["credential_permission_mode"] == "user_configured" and row.details["accepted_plan_version"] == 1 for row in logs)
    assert "first-key" not in str([row.details for row in logs])


@pytest.mark.parametrize(
    "reason",
    [
        "unapproved",
        "revoked",
        "human-gate",
        "wrong-action",
        "owner-lost-access",
        "expired-key",
        "demoted",
        "stale",
        "disabled",
        "missing-registry-scope",
    ],
)
async def test_refusal_precedes_secret_access(broker, reason):
    raw = dict(broker.plan.plan_document["execution_policy"])
    if reason == "unapproved":
        raw["user_credentials"] = {**raw["user_credentials"], "vault_credential_ids": ["other-key"]}
    elif reason == "revoked":
        broker.assignment.grant = replace(broker.assignment.grant, revoked=True)
    elif reason == "human-gate":
        raw["human_gates"] = ["develop"]
    elif reason == "wrong-action":
        raw["user_credentials"] = {**raw["user_credentials"], "actions": ["repair"]}
    elif reason == "owner-lost-access":
        broker.cred.user_id = None
        broker.cred.domain_app_id = "inaccessible-domain-app"
    elif reason == "expired-key":
        broker.cred.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif reason == "demoted":
        membership = await broker.db.scalar(select(TenantMembership).where(TenantMembership.user_id == APPROVER))
        membership.role = "member"
    elif reason == "stale":
        broker.plan.version = 2
        broker.plan.created_at = datetime.now(UTC) + timedelta(seconds=5)
    elif reason == "disabled":
        broker.settings.vault_raw_read_enabled = False
    else:
        broker.identity.credential_scopes = []
    broker.plan.plan_document = {**broker.plan.plan_document, "execution_policy": raw}
    await broker.db.commit()
    response = broker.client.post("/internal/v1/credential-raw-read", json=broker.body, headers=broker.headers)
    assert response.status_code in (403, 404), response.text
    broker.sm.get_secret.assert_not_called()


async def test_cancellation_during_secret_read_prevents_delivery(broker):
    def revoke(*_):
        broker.assignment.grant = replace(broker.assignment.grant, revoked=True)
        return "not-delivered-key"

    broker.sm.get_secret.side_effect = revoke
    response = broker.client.post("/internal/v1/credential-raw-read", json=broker.body, headers=broker.headers)
    assert response.status_code == 404 and "not-delivered-key" not in response.text


async def test_proxy_uses_approved_key_with_existing_destination_rules(broker):
    context = MagicMock()
    upstream = context.__aenter__.return_value
    upstream.request = AsyncMock(return_value=httpx.Response(200, text="provider-ok"))
    with (
        patch("src.internal.credential_routes.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
        patch("src.internal.credential_routes.httpx.AsyncClient", return_value=context),
    ):
        response = broker.client.post(
            "/internal/v1/proxy-request", json={**broker.body, "method": "POST", "url": "https://api.example.test/deploy"}, headers=broker.headers
        )
        assert response.status_code == 200, response.text
        assert upstream.request.await_args.kwargs["headers"]["Authorization"] == "ApiKey provider-key"
        refused = broker.client.post(
            "/internal/v1/proxy-request", json={**broker.body, "method": "POST", "url": "https://outside.test/deploy"}, headers=broker.headers
        )
        assert refused.status_code == 403
    upstream.request.assert_awaited_once()


async def test_file_materialization_retains_provider_value(broker):
    broker.cred.credential_type = "config_file"
    await broker.db.commit()
    with patch("boto3.client") as aws:
        aws.return_value.generate_presigned_url.return_value = "https://fixture.s3.amazonaws.com/file"
        response = broker.client.post("/internal/v1/credential-materialize", json=broker.body, headers=broker.headers)
        assert response.status_code == 201, response.text
        assert aws.return_value.put_object.call_args.kwargs["Body"] == b"provider-key"
        assert aws.return_value.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 300


async def test_saved_role_preserves_permissions_external_id_duration_tags_and_region(broker):
    broker.cred.credential_type = "aws_role"
    await broker.db.commit()
    broker.sm.get_secret.return_value = json.dumps(
        {"role_arn": ROLE, "external_id": "customer-external-id", "session_duration_seconds": 1800, "default_region": "eu-west-1"}
    )
    expiry = datetime.now(UTC) + timedelta(minutes=30)
    with patch("src.internal.sts_assume_service.boto3.client") as aws:
        aws.return_value.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "customer-access",
                "SecretAccessKey": "customer-secret",
                "SessionToken": "customer-token",
                "Expiration": expiry,
            }
        }
        response = broker.client.post("/internal/v1/credential-assume-role", json=broker.body, headers=broker.headers)
        assert response.status_code == 200, response.text
        call = aws.return_value.assume_role.call_args.kwargs
        assert call["RoleArn"] == ROLE and call["ExternalId"] == "customer-external-id"
        assert call["DurationSeconds"] == 1800 and call["Tags"]
        assert "Policy" not in call and "PolicyArns" not in call
        assert response.json()["region"] == "eu-west-1"


async def test_direct_sdk_source_is_limited_to_accepted_role_targets(broker):
    value = {
        "Version": 1,
        "AccessKeyId": "source-key",
        "SecretAccessKey": "source-secret",
        "SessionToken": "source-token",
        "Expiration": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    with patch("src.internal.task_credentials.issue_task_session", return_value=value) as issue:
        response = broker.client.post(
            "/internal/v1/worker-task-credentials", json={"user_id": APPROVER, "invocation_id": "worker"}, headers=broker.headers
        )
        assert response.status_code == 200, response.text
        assert issue.call_args.kwargs["targets"] == [ROLE]
        assert issue.call_args.kwargs["not_after"] <= broker.assignment.grant.expires_at


async def test_credential_selection_must_be_accessible_at_acceptance(broker):
    await validate_user_credential_authority(broker.db, policy=policy(), user_id=APPROVER)
    await broker.db.execute(delete(UserCredential).where(UserCredential.id == broker.cred.id))
    with pytest.raises(CredentialNotFoundError):
        await validate_user_credential_authority(broker.db, policy=policy(), user_id=APPROVER)


@pytest.mark.parametrize("removed", [True, False])
async def test_policy_removal_never_reopens_old_worker_access_but_legacy_still_works(broker, removed):
    if removed:
        broker.plan.superseded_at = datetime.now(UTC)
        broker.db.add(
            OrchestrationAcceptedPlan(
                org_id=ORG_A,
                flow_id=broker.assignment.flow.id,
                version=2,
                plan_document={"execution_policy": None},
                plan_hash="removed-policy",
            )
        )
    else:
        broker.plan.plan_document = {**broker.plan.plan_document, "execution_policy": None}
    await broker.db.commit()
    response = broker.client.post("/internal/v1/credential-raw-read", json=broker.body, headers=broker.headers)
    assert response.status_code == (404 if removed else 200), response.text
    if removed:
        broker.sm.get_secret.assert_not_called()
        from tests.orchestration.test_policy_admission import _authorize

        broker.assignment.node.state = "ready"
        assert not (await _authorize(broker.db, broker.assignment.node)).permitted


async def test_metadata_contains_only_selected_credentials_without_secret_references(broker):
    broker.db.add(
        UserCredential(
            id="not-approved",
            org_id=ORG_A,
            user_id=APPROVER,
            service="another-service",
            label="default",
            credential_type="api_key",
            secret_arn="never-visible",
        )
    )
    await broker.db.commit()
    response = broker.client.get("/internal/v1/user-credentials", params={"user_id": APPROVER, "invocation_id": "worker"}, headers=broker.headers)
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == ["approved-key"]
    assert "secret_arn" not in response.text and "fixture-secret" not in response.text
    broker.sm.get_secret.assert_not_called()


async def test_proxy_cancellation_during_secret_lookup_prevents_provider_effect(broker):
    def revoke(*_):
        broker.assignment.grant = replace(broker.assignment.grant, revoked=True)
        return "never-sent"

    broker.sm.get_secret.side_effect = revoke
    with (
        patch("src.internal.credential_routes.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
        patch("src.internal.credential_routes.httpx.AsyncClient") as provider,
    ):
        response = broker.client.post(
            "/internal/v1/proxy-request", json={**broker.body, "method": "POST", "url": "https://api.example.test/deploy"}, headers=broker.headers
        )
        assert response.status_code == 404
        provider.assert_not_called()


async def test_saved_role_cancellation_during_sts_prevents_session_delivery(broker):
    broker.cred.credential_type = "aws_role"
    await broker.db.commit()
    broker.sm.get_secret.return_value = json.dumps({"role_arn": ROLE})

    def revoke(**_):
        broker.assignment.grant = replace(broker.assignment.grant, revoked=True)
        return {
            "Credentials": {
                "AccessKeyId": "never-delivered",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(minutes=30),
            }
        }

    with patch("src.internal.sts_assume_service.boto3.client") as aws:
        aws.return_value.assume_role.side_effect = revoke
        response = broker.client.post("/internal/v1/credential-assume-role", json=broker.body, headers=broker.headers)
    assert response.status_code == 404 and "never-delivered" not in response.text


@pytest.mark.parametrize("change", ["revoked", "removed-targets", "expired-policy"])
async def test_direct_source_refresh_requires_current_authority(broker, change):
    value = {
        "Version": 1,
        "AccessKeyId": "source-key",
        "SecretAccessKey": "source-secret",
        "SessionToken": "source-token",
        "Expiration": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    body = {"user_id": APPROVER, "invocation_id": "worker"}
    with patch("src.internal.task_credentials.issue_task_session", return_value=value) as issue:
        assert broker.client.post("/internal/v1/worker-task-credentials", json=body, headers=broker.headers).status_code == 200
        raw = dict(broker.plan.plan_document["execution_policy"])
        if change == "revoked":
            broker.assignment.grant = replace(broker.assignment.grant, revoked=True)
        elif change == "removed-targets":
            raw["user_credentials"] = {**raw["user_credentials"], "aws_role_arns": []}
        else:
            raw["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        broker.plan.plan_document = {**broker.plan.plan_document, "execution_policy": raw}
        await broker.db.commit()
        response = broker.client.post("/internal/v1/worker-task-credentials", json=body, headers=broker.headers)
        assert response.status_code == 404 and "source-secret" not in response.text
        issue.assert_called_once()


@pytest.mark.parametrize(
    "credential_id,role,permitted",
    [
        ("approved-key", None, True),
        (None, ROLE, True),
        (None, None, False),
        ("unapproved", None, False),
        ("approved-key", ROLE + "Other", False),
        ("unapproved", ROLE, False),
    ],
)
def test_pure_user_authority_cannot_substitute_unapproved_targets(credential_id, role, permitted):
    from src.orchestration.execution_policy import CredentialScope, authorize_action
    from tests.orchestration.test_execution_policy import _context, _develop, _stamped

    accepted = _stamped(schema_version=2, user_credentials=authority())
    result = authorize_action(
        _context(accepted, credential_scope=CredentialScope.USER_GRANTED),
        Action.DEVELOP,
        replace(_develop(), user_credential_id=credential_id, aws_role_arn=role),
        1,
    )
    assert result.permitted is permitted


def test_summary_cannot_mutate_accepted_credential_authority():
    from src.orchestration.execution_policy import summarize_policy

    accepted = policy()
    summary = summarize_policy(accepted)
    summary.user_credentials.vault_credential_ids.append("injected")
    assert accepted.user_credentials.vault_credential_ids == ["approved-key"]


async def test_metadata_hides_expired_selected_credentials(broker):
    broker.cred.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await broker.db.commit()
    response = broker.client.get("/internal/v1/user-credentials", params={"user_id": APPROVER, "invocation_id": "worker"}, headers=broker.headers)
    assert response.status_code == 404
    broker.sm.get_secret.assert_not_called()


def test_v1_cannot_use_user_granted_scope_even_with_a_selected_target():
    from src.orchestration.execution_policy import CredentialScope, DenyReason, authorize_action
    from tests.orchestration.test_execution_policy import _context, _develop

    result = authorize_action(
        _context(credential_scope=CredentialScope.USER_GRANTED), Action.DEVELOP, replace(_develop(), user_credential_id="approved-key"), 1
    )
    assert result.reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE
