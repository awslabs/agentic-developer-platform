"""Repository plans and existing-source adoption through Product routes."""

import asyncio
import json
import os
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.admin.connections.github_client import GitHubAppClient
from src.auth.dependencies import get_current_user
from src.product_onboarding.callback_auth import PRODUCER, get_callback_user
from src.product_onboarding.handoffs import verify_repository_plan
from src.product_onboarding.routes import fingerprint
from src.products.models import Product, ProductOperation, ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap
from tests.products.source_reuse import assert_source_reuse
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i04 import GatewayClient, cli
from tests.products.test_i08_api import mutation, setup_source
from tests.products.test_i10 import setup_aws


@pytest.mark.asyncio
async def test_repository_preview_requires_matching_confirmation(test_client, db_session, source_member, monkeypatch):
    async def validate(installation_id, plan):
        assert installation_id == 17

    async def create(installation_id, plan, operation_id):
        assert installation_id == 17
        return {"native_id": "91", "owner_native_id": plan["owner_native_id"], "applied_plan": plan}

    monkeypatch.setattr("src.product_onboarding.repository_creation.validate_repository_source", validate)
    monkeypatch.setattr("src.product_onboarding.repository_creation.create_confirmed_repository", create)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    session = await test_client.post(root, json={}, headers=mutation(1, "i13_session_123456"))
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    mapping = ChannelTenantMap(
        org_id=source_member.org_id,
        id="i13-connection",
        provider="github",
        provider_scope_id="27",
        installation_id="17",
        ownership_disputed=False,
    )
    db_session.add(mapping)
    await db_session.commit()
    reference = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": mapping.id,
        "record_type": "repository_template",
        "record_id": "template-42",
        "record_revision": "version-3",
    }
    plan = {
        "connection_ref": {"kind": "github_installation", "id": mapping.id},
        "owner_native_id": "27",
        "name": "run-owned-project",
        "visibility": "private",
        "template_ref": reference,
        "protections_ref": {**reference, "record_type": "repository_protections", "record_id": "policy-3"},
        "confirmed": False,
    }
    preview_path = f"{root}/{session_id}/repository-plans"
    headers = mutation(1, "i13_preview_123456")
    preview = await test_client.post(preview_path, json={"repository_plan": plan}, headers=headers)
    assert preview.status_code == 201, preview.text
    assert preview.json()["repository_plan"] == plan and preview.json()["revision"] == 1
    assert (await test_client.post(preview_path, json={"repository_plan": plan}, headers=headers)).json() == preview.json()
    assert (
        await test_client.post(
            preview_path,
            json={"repository_plan": {**plan, "name": "other-project"}},
            headers=headers,
        )
    ).status_code == 409
    assert (
        await test_client.post(
            preview_path,
            json={"repository_plan": {**plan, "confirmed": True}},
            headers=mutation(1, "i13_invalid_preview_123456"),
        )
    ).status_code == 422
    assert (
        await test_client.post(
            preview_path,
            json={"repository_plan": plan},
            headers=mutation(2, "i13_stale_preview_123456"),
        )
    ).status_code == 412
    assert (
        await test_client.post(
            preview_path,
            json={"repository_plan": {**plan, "owner_native_id": "29"}},
            headers=mutation(1, "i13_wrong_owner_123456"),
        )
    ).status_code == 403
    assert (
        await test_client.post(
            preview_path,
            json={"repository_plan": {**plan, "template_ref": {**reference, "tenant_id": "other-tenant"}}},
            headers=mutation(1, "i13_wrong_scope_123456"),
        )
    ).status_code == 409
    handoff_path = f"{root}/{session_id}/handoffs"
    confirmation = {
        "target": "repository_create",
        "action": "create_repository",
        "preview_id": preview.json()["id"],
        "repository_plan": {**plan, "confirmed": True},
    }
    assert (
        await test_client.post(
            handoff_path,
            json={**confirmation, "repository_plan": {**plan, "name": "other-project", "confirmed": True}},
            headers=mutation(1, "i13_wrong_plan_123456"),
        )
    ).status_code == 409
    assert (
        await test_client.post(
            handoff_path,
            json={**confirmation, "repository_plan": plan},
            headers=mutation(1, "i13_unconfirmed_123456"),
        )
    ).status_code == 422
    assert (
        await test_client.post(
            handoff_path,
            json=confirmation,
            headers=mutation(2, "i13_stale_confirmation_123456"),
        )
    ).status_code == 412
    confirmed = await test_client.post(handoff_path, json=confirmation, headers=mutation(1, "i13_confirm_123456"))
    assert confirmed.status_code == 202, confirmed.text
    assert confirmed.json()["state"] == "succeeded"
    assert confirmed.json()["repository"]["native_id"] == "91"
    operation = await db_session.get(ProductOperation, (source_member.org_id, confirmed.json()["id"]))
    assert operation.request["request"]["repository_plan"] == confirmation["repository_plan"]
    assert operation.expected_revision == 1 and operation.provider_operation_id == "91"
    assert (await db_session.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "startHandoff"))) == 1
    assert (await test_client.post(handoff_path, json=confirmation, headers=mutation(1, "i13_confirm_123456"))).json() == confirmed.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost_response,source_changed,policy_changed,stale_before_effect",
    [(False, False, False, False), (True, False, False, False), (True, True, False, False), (True, False, True, False), (False, False, False, True)],
)
async def test_confirmed_repository_creates_exactly_one_provider_asset(
    test_client, db_session, source_member, monkeypatch, lost_response, source_changed, policy_changed, stale_before_effect
):
    await _confirmed_repository_attempt(
        test_client, db_session, source_member, monkeypatch, lost_response, source_changed, policy_changed, stale_before_effect
    )


async def _confirmed_repository_attempt(
    test_client, db_session, source_member, monkeypatch, lost_response, source_changed, policy_changed, stale_before_effect
):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    session = await test_client.post(root, json={}, headers=mutation(1, "i13_create_session_123456"))
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    db_session.add(
        ChannelTenantMap(
            org_id=source_member.org_id,
            id="i13-installation",
            provider="github",
            provider_scope_id="27",
            installation_id="17",
            ownership_disputed=False,
        )
    )
    await db_session.commit()
    sha = "a" * 40
    protection = {
        "required_status_checks": {"strict": True, "contexts": ["ci/test"]},
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {"dismiss_stale_reviews": True, "require_code_owner_reviews": True, "required_approving_review_count": 2},
        "restrictions": None,
    }
    policy = {
        **protection,
        "enforce_admins": True,
    }
    requests = []
    effect = {
        "created": False,
        "marker": None,
        "protected": False,
        "applied_protection": None,
        "source_changed": False,
        "policy_changed": False,
        "branch_reads": 0,
        "lose_protection_response": False,
    }

    def github(request):
        path = request.url.path
        if path == "/app/installations/17/access_tokens":
            return httpx.Response(201, json={"token": "offline-fixture"})
        requests.append((request.method, path, request.content))
        if path == "/app/installations/17":
            return httpx.Response(
                200,
                json={
                    "id": 17,
                    "account": {"id": 27, "login": "example"},
                    "permissions": {"administration": "write", "contents": "write"},
                    "repository_selection": "all",
                },
            )
        if path == "/repositories/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "owner": {"id": 27, "login": "example"},
                    "name": "template",
                    "default_branch": "main",
                    "is_template": True,
                },
            )
        if path == "/repositories/91":
            if not effect["created"]:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "id": 91,
                    "owner": {"id": 27, "login": "example"},
                    "name": "fresh-project",
                    "visibility": "private",
                    "default_branch": "main",
                    "description": effect["marker"],
                },
            )
        if path == "/repos/example/fresh-project":
            return (
                httpx.Response(
                    200,
                    json={
                        "id": 91,
                        "owner": {"id": 27, "login": "example"},
                        "name": "fresh-project",
                        "description": effect["marker"],
                    },
                )
                if effect["created"]
                else httpx.Response(404, json={"message": "Not Found"})
            )
        if path == "/repos/example/template/branches/main":
            effect["branch_reads"] += 1
            changed = effect["source_changed"] or (stale_before_effect and effect["branch_reads"] >= 4)
            return httpx.Response(200, json={"commit": {"sha": "b" * 40 if changed else sha}})
        if path == "/repos/example/template/branches/main/protection":
            return httpx.Response(
                200,
                json=(
                    {
                        **protection,
                        "required_pull_request_reviews": {
                            **protection["required_pull_request_reviews"],
                            "required_approving_review_count": 3,
                        },
                    }
                    if effect["policy_changed"]
                    else protection
                ),
            )
        if path == "/repos/example/template/generate":
            body = json.loads(request.content)
            assert {key: body[key] for key in ("owner", "name", "private", "include_all_branches")} == {
                "owner": "example",
                "name": "fresh-project",
                "private": True,
                "include_all_branches": False,
            }
            assert body["description"].startswith("ADP repository creation: ")
            effect.update(created=True, marker=body["description"])
            if lost_response:
                raise httpx.ReadTimeout("Provider committed create but response was lost")
            return httpx.Response(201, json={"id": 91, "owner": {"id": 27}, "default_branch": "main"})
        if path == "/repos/example/fresh-project/branches/main/protection":
            if request.method == "PUT":
                applied_protection = (
                    {
                        **protection,
                        "required_pull_request_reviews": {
                            **protection["required_pull_request_reviews"],
                            "required_approving_review_count": 3,
                        },
                    }
                    if effect["policy_changed"]
                    else protection
                )
                assert json.loads(request.content) == {**applied_protection, "enforce_admins": True}
                effect["protected"] = True
                effect["applied_protection"] = applied_protection
                if effect["lose_protection_response"]:
                    effect["lose_protection_response"] = False
                    raise httpx.ReadTimeout("Protection committed but response was lost")
            if not effect["protected"]:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=effect["applied_protection"])
        raise AssertionError(f"Unexpected provider path {path}")

    monkeypatch.setattr("src.admin.connections.github_client._mint_app_jwt", lambda *_: "offline-fixture")
    monkeypatch.setattr("src.product_onboarding.repository_creation._get_github_app_credentials", lambda: ("offline-app", "offline-key"))
    monkeypatch.setattr(
        "src.product_onboarding.repository_creation.GitHubAppClient",
        lambda app_id, private_key: GitHubAppClient(
            app_id,
            private_key,
            http_client=httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github)),
        ),
    )
    options = await test_client.get(
        f"{root}/{session_id}/repository-options",
        params={"connection_id": "i13-installation", "template_id": "42"},
    )
    assert options.status_code == 200, options.text
    assert options.json()["owner_native_id"] == "27"
    assert options.json()["template_ref"]["record_revision"] == sha
    assert options.json()["protections_ref"]["record_revision"] == fingerprint(policy)
    assert options.json()["protections"] == policy
    plan = {key: options.json()[key] for key in ("connection_ref", "owner_native_id", "template_ref", "protections_ref")}
    plan.update(name="fresh-project", visibility="private", confirmed=False)
    preview = await test_client.post(
        f"{root}/{session_id}/repository-plans",
        json={"repository_plan": plan},
        headers=mutation(1, "i13_live_preview_123456"),
    )
    assert preview.status_code == 201, preview.text
    assert not [request for request in requests if request[0] != "GET"]
    confirmation = {
        "target": "repository_create",
        "action": "create_repository",
        "preview_id": preview.json()["id"],
        "repository_plan": {**plan, "confirmed": True},
    }
    result = await test_client.post(
        f"{root}/{session_id}/handoffs",
        json=confirmation,
        headers=mutation(1, "i13_live_create_123456"),
    )
    if stale_before_effect:
        assert result.status_code == 412 and result.json()["detail"]["code"] == "stale_revision", result.text
        assert not [request for request in requests if request[0] in ("POST", "PUT", "PATCH")]
        operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.operation == "startHandoff"))
        await db_session.refresh(operation)
        assert operation.state == "failed" and operation.provider_operation_id is None
        return
    assert result.status_code == 202, result.text
    assert result.json()["state"] == ("outcome_unknown" if lost_response else "succeeded"), result.text
    session_after = await test_client.get(f"{root}/{session_id}")
    assert session_after.status_code == 200, session_after.text
    assert preview.json()["id"] not in session_after.json()["operation_ids"]
    if not lost_response:
        assert result.json()["repository"]["native_id"] == "91"
        assert result.json()["repository"]["applied_plan"] == confirmation["repository_plan"]
    assert [request[1] for request in requests if request[0] == "POST"] == ["/repos/example/template/generate"]
    assert [request[1] for request in requests if request[0] == "PUT"] == (
        [] if lost_response else ["/repos/example/fresh-project/branches/main/protection"]
    )
    replay = await test_client.post(
        f"{root}/{session_id}/handoffs",
        json=confirmation,
        headers=mutation(1, "i13_live_create_123456"),
    )
    assert replay.status_code == 200
    if lost_response:
        effect["source_changed"] = source_changed
        effect["policy_changed"] = policy_changed
        reconciliation = await test_client.post(
            f"/v1/products/{product_id}/projects/{project_id}/operations/{result.json()['id']}/reconcile",
            headers=mutation(1, "i13_reconcile_123456"),
        )
        if source_changed or policy_changed:
            assert reconciliation.status_code == 412, reconciliation.text
            assert reconciliation.json()["detail"]["code"] == "stale_revision"
        else:
            assert reconciliation.status_code == 202, reconciliation.text
            assert reconciliation.json()["state"] == "succeeded"
            assert reconciliation.json()["repository"]["native_id"] == "91"
            assert reconciliation.json()["repository"]["coverage"] == "complete"
        persisted = await db_session.get(ProductOperation, (source_member.org_id, result.json()["id"]))
        await db_session.refresh(persisted)
        assert persisted.provider_operation_id == "91"
        assert persisted.state == ("outcome_unknown" if source_changed or policy_changed else "succeeded")
        assert [request[1] for request in requests if request[0] == "PUT"] == (
            [] if source_changed or policy_changed else ["/repos/example/fresh-project/branches/main/protection"]
        )
        assert (
            await test_client.post(
                f"{root}/{session_id}/handoffs",
                json=confirmation,
                headers=mutation(1, "i13_changed_key_123456"),
            )
        ).status_code == 409
    else:
        assert replay.json()["repository"]["native_id"] == "91"
    assert len([request for request in requests if request[0] == "POST"]) == 1
    return {
        "provider": github,
        "requests": requests,
        "effect": effect,
        "operation_id": result.json()["id"],
        "product_id": product_id,
        "project_id": project_id,
        "session_id": session_id,
        "plan": confirmation["repository_plan"],
        "template_revision": sha,
        "protections": protection,
        "preview_id": preview.json()["id"],
    }


@pytest.mark.asyncio
async def repository_explicit_create(test_client, test_engine, db_session, source_member, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    monkeypatch.setattr("src.product_onboarding.handoffs.utcnow", lambda: clock)
    attempt = await _confirmed_repository_attempt(test_client, db_session, source_member, monkeypatch, False, False, False, False)
    writes = [(method, path) for method, path, _ in attempt["requests"] if method in ("POST", "PUT", "PATCH", "DELETE")]
    assert writes == [
        ("POST", "/repos/example/template/generate"),
        ("PUT", "/repos/example/fresh-project/branches/main/protection"),
    ]
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(attempt["provider"])) as independent_provider:
        repository = (await independent_provider.get("/repositories/91")).json()
        applied = (await independent_provider.get("/repos/example/fresh-project/branches/main/protection")).json()
    assert (repository["id"], repository["owner"]["id"], repository["name"], repository["visibility"]) == (
        91,
        27,
        attempt["plan"]["name"],
        attempt["plan"]["visibility"],
    )
    assert repository["description"].endswith(attempt["operation_id"])
    assert applied == attempt["protections"]
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent_db:
        operation = await independent_db.get(ProductOperation, (source_member.org_id, attempt["operation_id"]))
        preview = await independent_db.get(ProductOperation, (source_member.org_id, attempt["preview_id"]))
        assert operation is not None and operation.state == "succeeded" and operation.provider_operation_id == "91"
        assert operation.expected_revision == 1 and operation.receipt == {
            "native_id": "91",
            "owner_native_id": "27",
            "applied_plan": attempt["plan"],
        }
        assert preview is not None and preview.provider_operation_id == operation.id
        assert preview.request["plan"] == {**attempt["plan"], "confirmed": False}
        assert (await independent_db.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0
        saved_handoffs = await independent_db.scalar(
            select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "startHandoff")
        )
        assert saved_handoffs == 1

    record_property("scenario_id", "SCN-01")
    record_property("criterion", "AC-01")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("fixture_clock", clock.isoformat())
    record_property("provider_source_revision", attempt["template_revision"])
    record_property("observation", json.dumps({"native_id": repository["id"], "visibility": repository["visibility"], "protections": applied}))
    record_property(
        "attempt_report",
        json.dumps(
            {
                "gateway": {"confirmations": 2, "status": "202 then idempotent 200"},
                "provider": {"create_attempts": 1, "writes": writes, "independent_readback": repository["id"]},
                "database": {"saved_operation": operation.id, "native_id": operation.provider_operation_id},
            }
        ),
    )
    record_property("fixture_cleanup", "Function-scoped run-owned provider fixture and SQLite engine disposed by pytest")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_changed,policy_changed,lose_protection_response",
    [
        (True, False, False),
        (False, True, False),
        (True, False, True),
    ],
)
async def repository_exactly_once(
    test_client,
    test_engine,
    db_session,
    app,
    source_member,
    monkeypatch,
    record_property,
    source_changed,
    policy_changed,
    lose_protection_response,
):
    clock = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    monkeypatch.setattr("src.product_onboarding.handoffs.utcnow", lambda: clock)
    attempt = await _confirmed_repository_attempt(test_client, db_session, source_member, monkeypatch, True, source_changed, policy_changed, False)
    root = f"/v1/products/{attempt['product_id']}/projects/{attempt['project_id']}"
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(attempt["provider"])) as provider:
        repository = (await provider.get("/repositories/91")).json()
        unapplied = await provider.get("/repos/example/fresh-project/branches/main/protection")
    assert repository["id"] == 91 and repository["owner"]["id"] == 27
    assert repository["description"].endswith(attempt["operation_id"])
    assert unapplied.status_code == 404
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent_db:
        operation = await independent_db.get(ProductOperation, (source_member.org_id, attempt["operation_id"]))
        assert operation is not None and operation.state == "outcome_unknown"
        assert operation.provider_operation_id == "91" and operation.receipt["native_id"] == "91"
        assert operation.receipt["coverage"] == "partial" and operation.receipt["protections_applied"] is False
        assert operation.request["request"]["repository_plan"] == attempt["plan"]

    options = await test_client.get(
        f"{root}/onboarding-sessions/{attempt['session_id']}/repository-options",
        params={"connection_id": "i13-installation", "template_id": "42"},
    )
    assert options.status_code == 200, options.text
    if source_changed:
        assert options.json()["template_ref"]["record_revision"] != attempt["template_revision"]
    else:
        assert options.json()["protections_ref"]["record_revision"] != attempt["plan"]["protections_ref"]["record_revision"]
    updated = {**attempt["plan"], "template_ref": options.json()["template_ref"], "protections_ref": options.json()["protections_ref"]}
    preview = await test_client.post(
        f"{root}/onboarding-sessions/{attempt['session_id']}/repository-plans",
        json={"repository_plan": {**updated, "confirmed": False}},
        headers=mutation(1, "i13_fresh_plan_preview_123456"),
    )
    assert preview.status_code == 201, preview.text
    confirmation = {
        "target": "repository_create",
        "action": "create_repository",
        "preview_id": preview.json()["id"],
        "repository_plan": {**updated, "confirmed": True},
    }
    foreign = source_member.model_copy(update={"org_id": "other-tenant", "user_id": "foreign-member"})
    app.dependency_overrides[get_current_user] = lambda: foreign
    try:
        denied = await test_client.post(
            f"{root}/onboarding-sessions/{attempt['session_id']}/handoffs",
            json=confirmation,
            headers=mutation(1, "i13_foreign_renew_123456"),
        )
    finally:
        app.dependency_overrides[get_current_user] = lambda: source_member
    assert denied.status_code == 404
    change_key = "source_changed" if source_changed else "policy_changed"
    attempt["effect"][change_key] = False
    stale = await test_client.post(
        f"{root}/onboarding-sessions/{attempt['session_id']}/handoffs",
        json=confirmation,
        headers=mutation(1, "i13_stale_renew_123456"),
    )
    assert stale.status_code == 412 and stale.json()["detail"]["code"] == "stale_revision"
    attempt["effect"][change_key] = True
    assert len([request for request in attempt["requests"] if request[0] == "POST"]) == 1
    attempt["effect"]["lose_protection_response"] = lose_protection_response
    fresh = await test_client.post(
        f"{root}/onboarding-sessions/{attempt['session_id']}/handoffs",
        json=confirmation,
        headers=mutation(1, "i13_fresh_plan_confirm_123456"),
    )
    assert fresh.status_code == 202 and fresh.json()["state"] == ("outcome_unknown" if lose_protection_response else "succeeded"), fresh.text
    assert fresh.json()["repository"]["native_id"] == "91"
    if lose_protection_response:
        recovered = await test_client.post(
            f"{root}/operations/{fresh.json()['id']}/reconcile",
            headers=mutation(1, "i13_renew_reconcile_123456"),
        )
        assert recovered.status_code == 202 and recovered.json()["state"] == "succeeded", recovered.text
        assert recovered.json()["repository"]["native_id"] == "91"
    else:
        assert fresh.json()["repository"]["applied_plan"] == confirmation["repository_plan"]
    replay = await test_client.post(
        f"{root}/onboarding-sessions/{attempt['session_id']}/handoffs",
        json=confirmation,
        headers=mutation(1, "i13_fresh_plan_confirm_123456"),
    )
    assert replay.status_code == 200 and replay.json()["repository"]["native_id"] == "91"
    writes = [(method, path) for method, path, _ in attempt["requests"] if method in ("POST", "PUT", "PATCH", "DELETE")]
    assert writes == [
        ("POST", "/repos/example/template/generate"),
        ("PUT", "/repos/example/fresh-project/branches/main/protection"),
    ]
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(attempt["provider"])) as provider:
        applied = await provider.get("/repos/example/fresh-project/branches/main/protection")
        assert applied.status_code == 200
        assert applied.json()["required_pull_request_reviews"]["required_approving_review_count"] == (3 if policy_changed else 2)
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent_db:
        preserved = await independent_db.get(ProductOperation, (source_member.org_id, attempt["operation_id"]))
        renewed = await independent_db.get(ProductOperation, (source_member.org_id, fresh.json()["id"]))
        assert preserved.provider_operation_id == "91" and preserved.state == "cancelled"
        assert renewed.provider_operation_id == "91" and renewed.state == "succeeded"
        assert renewed.request["resume_operation_id"] == preserved.id
        assert renewed.receipt["applied_plan"] == {**updated, "confirmed": True}
    old_reconcile = await test_client.post(
        f"{root}/operations/{attempt['operation_id']}/reconcile",
        headers=mutation(1, "i13_old_after_renewal_123456"),
    )
    assert old_reconcile.status_code == 202 and old_reconcile.json()["state"] == "cancelled"
    assert len([request for request in attempt["requests"] if request[0] == "POST"]) == 1

    record_property("scenario_id", "SCN-02")
    record_property("criterion", "AC-02")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("fixture_clock", clock.isoformat())
    record_property("provider_source_revision", attempt["template_revision"])
    record_property(
        "observation",
        json.dumps(
            {
                "native_id": repository["id"],
                "marker": repository["description"],
                "protections": applied.json(),
            }
        ),
    )
    record_property(
        "attempt_report",
        json.dumps(
            {
                "gateway": {"stale_reconcile": 412, "foreign_denial": denied.status_code, "renewal": fresh.status_code},
                "provider": {"create_attempts": 1, "writes": writes, "independent_readback": repository["id"]},
                "database": {"saved_operation": operation.id, "native_id": preserved.provider_operation_id},
            }
        ),
    )
    record_property("fixture_cleanup", "Function-scoped run-owned provider fixture and SQLite engine disposed by pytest")


@pytest.mark.asyncio
async def test_adopted_assets_survive_unlink_archive_and_late_reconcile(test_client, db_session, app, source_member, monkeypatch):
    path, environment, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    product_id, project_id = path.split("/")[3], path.split("/")[5]
    product = await db_session.get(Product, (source_member.org_id, product_id))
    product.starting_point = "running_product"
    product.lifecycle_status = "active"
    await db_session.commit()
    repository = {
        "kind": "repository",
        "provider": "github",
        "namespace": "27",
        "native_id": "35",
        "connection_ref": {"kind": "github_installation", "id": "run-owned-connection"},
    }
    pipeline = {**repository, "kind": "pipeline", "native_id": "77", "repository_id": "35", "source_revision": "pipeline-rev"}
    provider_assets = {
        "repository": {"native_id": "35", "protected": True},
        "pipeline": {"native_id": "77", "configuration": "unchanged"},
        "cloud": {"account": secret.account, "region": "eu-west-1", "role": secret.role},
    }

    async def provider_readback():
        return deepcopy(provider_assets)

    baseline = await provider_readback()

    async def forbid_provider_write(*args, **kwargs):
        pytest.fail("Adopting or archiving existing assets must not write to a provider")

    monkeypatch.setattr("src.product_onboarding.repository_creation.create_confirmed_repository", forbid_provider_write)
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient.installation_request", forbid_provider_write)

    async def read_pipeline(connection, repository_id, *, workflow_id):
        assert repository_id == "35" and workflow_id == "77"
        return [{"namespace": "27", "source_revision": "pipeline-rev"}], None, "complete"

    monkeypatch.setattr("src.product_onboarding.source_discovery.github_pipeline_resources", read_pipeline)
    selections = [repository, pipeline, environment]
    ids = []
    for revision, selection in enumerate(selections, start=1):
        binding = await test_client.post(path, json=selection, headers=mutation(revision, f"i13_adopt_{revision}_123456"))
        assert binding.status_code == 201, binding.text
        ids.append(binding.json()["id"])
    assert (await test_client.get(path.removesuffix("/bindings"))).json()["product"]["starting_point"] == "running_product"
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 3
    for revision, binding_id in enumerate(ids, start=4):
        unlink = await test_client.delete(f"{path}/{binding_id}", headers=mutation(revision, f"i13_unlink_{revision}_123456"))
        assert unlink.status_code == 200, unlink.text
        assert unlink.json()["status"] == "unlinked"
    session_root = path.removesuffix("/bindings") + "/onboarding-sessions"
    session = await test_client.post(session_root, json={}, headers=mutation(7, "i13_archive_session_123456"))
    assert session.status_code == 201, session.text
    uncertain = ProductOperation(
        tenant_id=source_member.org_id,
        product_id=product_id,
        project_id=project_id,
        session_id=session.json()["id"],
        actor_id=source_member.user_id,
        operation="startHandoff",
        key_hash="a" * 64,
        request_hash="b" * 64,
        state="outcome_unknown",
        request={
            "request": {
                "target": "repository_create",
                "repository_plan": {
                    "connection_ref": repository["connection_ref"],
                    "owner_native_id": "27",
                    "name": "existing",
                    "visibility": "private",
                },
            }
        },
        expected_revision=7,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(uncertain)
    await db_session.commit()
    archived = await test_client.post(
        path.removesuffix("/bindings") + "/lifecycle",
        json={"action": "archive", "reason": "Existing assets remain provider owned"},
        headers=mutation(7, "i13_archive_123456"),
    )
    assert archived.status_code == 200, archived.text
    await db_session.refresh(uncertain)
    assert uncertain.state == "cancelled"

    async def observed(*args):
        return {"native_id": "35", "owner_native_id": "27", "coverage": "partial"}

    monkeypatch.setattr("src.product_onboarding.repository_creation.reconcile_repository", observed)
    monkeypatch.setattr("src.product_onboarding.repository_creation.finish_repository", forbid_provider_write)
    reconcile = await test_client.post(
        f"/v1/products/{product_id}/projects/{project_id}/operations/{uncertain.id}/reconcile",
        headers=mutation(8, "i13_archive_reconcile_123456"),
    )
    assert reconcile.status_code == 202, reconcile.text
    assert reconcile.json()["state"] == "cancelled"
    assert reconcile.json()["repository"]["native_id"] == "35"
    assert credential.id == environment["connection_ref"]["id"]
    assert await provider_readback() == baseline
    await db_session.refresh(product)
    assert product.archived_at is not None
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding).where(ProductSourceBinding.status == "unlinked"))) == 3


@pytest.mark.asyncio
async def test_archived_product_fences_confirmed_create_before_provider_call(test_client, db_session, source_member, monkeypatch):
    path, selection = await setup_source(test_client, db_session, source_member)
    product_id, project_id = path.split("/")[3], path.split("/")[5]
    root = path.removesuffix("/bindings") + "/onboarding-sessions"
    session = await test_client.post(root, json={}, headers=mutation(1, "i13_archive_create_session_123456"))
    assert session.status_code == 201, session.text
    reference = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": selection["connection_ref"]["id"],
        "record_type": "repository",
        "record_id": "42",
        "record_revision": "a" * 40,
    }
    plan = {
        "connection_ref": selection["connection_ref"],
        "owner_native_id": "27",
        "name": "run-owned-repo",
        "visibility": "private",
        "template_ref": reference,
        "protections_ref": {**reference, "record_type": "repository_protections", "record_revision": "b" * 64},
        "confirmed": False,
    }

    async def validate(*args):
        return None

    async def forbid_create(*args):
        pytest.fail("An archived Product must not start provider creation")

    monkeypatch.setattr("src.product_onboarding.repository_creation.validate_repository_source", validate)
    monkeypatch.setattr("src.product_onboarding.repository_creation.create_confirmed_repository", forbid_create)
    preview = await test_client.post(
        f"{root}/{session.json()['id']}/repository-plans",
        json={"repository_plan": plan},
        headers=mutation(1, "i13_archive_preview_123456"),
    )
    assert preview.status_code == 201, preview.text

    async def archive_after_check(db, actor, checked_plan, checked_project):
        mapping = await verify_repository_plan(db, actor, checked_plan, checked_project)
        product = await db_session.get(Product, (source_member.org_id, product_id))
        product.archived_at = datetime.now(UTC)
        product.revision += 1
        await db_session.commit()
        return mapping

    monkeypatch.setattr("src.product_onboarding.handoffs.verify_repository_plan", archive_after_check)
    confirmed = await test_client.post(
        f"{root}/{session.json()['id']}/handoffs",
        json={
            "target": "repository_create",
            "action": "create_repository",
            "preview_id": preview.json()["id"],
            "repository_plan": {**plan, "confirmed": True},
        },
        headers=mutation(1, "i13_archive_confirm_123456"),
    )
    assert confirmed.status_code == 409 and confirmed.json()["detail"]["code"] == "product_archived", confirmed.text
    operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.operation == "startHandoff"))
    assert operation is not None and operation.state == "cancelled" and operation.provider_operation_id is None


class ReadOnlyActions:
    def __init__(self):
        self.assets = {
            "repository": {"id": 35, "owner": {"id": 27, "login": "example"}, "name": "existing", "updated_at": "repo-rev"},
            "workflows": [
                {"id": 77, "name": "Existing build", "updated_at": "pipeline-rev"},
                {"id": 78, "name": "Existing deploy", "updated_at": "pipeline-rev"},
            ],
        }
        self.calls = []
        self.actions_read = True

    async def get_installation(self, installation_id):
        self.calls.append(("GET installation", installation_id))
        return {"id": installation_id, "account": {"id": 27, "login": "example"}}

    async def get_installation_repository(self, installation_id, repository_id):
        self.calls.append(("GET repository", repository_id))
        return self.assets["repository"] if repository_id == 35 else {"id": repository_id, "owner": {"id": 29, "login": "other"}, "name": "foreign"}

    async def list_installation_repositories_page(self, installation_id, page, limit):
        self.calls.append(("GET repositories", page, limit))
        return {"total_count": 1, "repositories": [self.assets["repository"]] if page == 1 else []}

    async def installation_request(self, installation_id, method, path):
        assert method == "GET", "Adoption must never invoke a provider write"
        self.calls.append((method, path))
        if not self.actions_read:
            request = httpx.Request(method, f"https://api.github.com{path}")
            raise httpx.HTTPStatusError("Actions read denied", request=request, response=httpx.Response(403, request=request))
        assert path.startswith("/repos/example/existing/actions/workflows")
        if path.endswith("/77"):
            return self.assets["workflows"][0]
        if path.endswith("/78"):
            return self.assets["workflows"][1]
        if "per_page=1" in path:
            return {"workflows": self.assets["workflows"][int(path.rsplit("page=", 1)[1]) - 1 : int(path.rsplit("page=", 1)[1])], "total_count": 2}
        return {"workflows": self.assets["workflows"], "total_count": 2}

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_read_only_pipeline_discovery_and_adoption(test_client, db_session, source_member, monkeypatch):
    path, repository = await setup_source(test_client, db_session, source_member)
    fixture = ReadOnlyActions()
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.product_onboarding.source_discovery.GitHubAppClient", lambda *args: fixture)
    root = path.removesuffix("/bindings")
    parameters = {
        "connection_kind": "github_installation",
        "connection_id": repository["connection_ref"]["id"],
        "pipeline_repository_id": "35",
        "limit": 1,
    }
    baseline = deepcopy(fixture.assets)
    connections = await test_client.get(root + "/sources")
    assert "pipeline" in connections.json()["items"][0]["capabilities"]
    first = await test_client.get(root + "/sources", params=parameters)
    assert first.status_code == 200, first.text
    assert first.json()["items"][0]["resource_cursor"] == "2"
    assert first.json()["items"][0]["resources"][0]["native_id"] == "77"
    second = await test_client.get(root + "/sources", params={**parameters, "resource_cursor": "2"})
    assert [resource["native_id"] for resource in second.json()["items"][0]["resources"]] == ["78"]
    pipeline = {**repository, "kind": "pipeline", "native_id": "77", "repository_id": "35", "source_revision": "pipeline-rev"}
    created = await test_client.post(path, json=pipeline, headers=mutation(1, "i13_pipeline_adopt_123456"))
    assert created.status_code == 201, created.text
    assert created.json()["selection"] == pipeline
    assert fixture.assets == baseline and all(call[0].startswith("GET") for call in fixture.calls)
    assert (await test_client.delete(f"{path}/{created.json()['id']}", headers=mutation(2, "i13_pipeline_unlink_123456"))).status_code == 200
    assert fixture.assets == baseline and all(call[0].startswith("GET") for call in fixture.calls)


@pytest.mark.asyncio
async def test_pipeline_adoption_requires_read_access_current_revision_and_scope(test_client, db_session, source_member, monkeypatch):
    path, repository = await setup_source(test_client, db_session, source_member)
    fixture = ReadOnlyActions()
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.product_onboarding.source_discovery.GitHubAppClient", lambda *args: fixture)
    root = path.removesuffix("/bindings")
    parameters = {"connection_kind": "github_installation", "connection_id": repository["connection_ref"]["id"], "pipeline_repository_id": "99"}
    foreign = await test_client.get(root + "/sources", params=parameters)
    assert foreign.status_code == 200 and foreign.json()["items"][0]["coverage"] == "denied"
    pipeline = {**repository, "kind": "pipeline", "native_id": "77", "repository_id": "35", "source_revision": "pipeline-rev"}
    rejected_selections = ({**pipeline, "repository_id": "99"}, {**pipeline, "source_revision": "stale"}, {**pipeline, "native_id": "999"})
    for index, changed in enumerate(rejected_selections):
        rejected = await test_client.post(path, json=changed, headers=mutation(1, f"i13_pipeline_denial_{index}_123"))
        assert rejected.status_code in (404, 409), rejected.text
    fixture.actions_read = False
    denied = await test_client.post(path, json=pipeline, headers=mutation(1, "i13_pipeline_no_read_123456"))
    assert denied.status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0
    assert all(call[0].startswith("GET") for call in fixture.calls)


@pytest.mark.asyncio
async def running_product_adoption(test_client, test_engine, db_session, app, source_member, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    path, environment, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=clock)
    product_id, project_id = path.split("/")[3], path.split("/")[5]
    product = await db_session.get(Product, (source_member.org_id, product_id))
    product.starting_point = "running_product"
    product.lifecycle_status = "active"
    await db_session.commit()
    github = ReadOnlyActions()
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.product_onboarding.source_discovery.GitHubAppClient", lambda *args: github)

    async def forbid_provider_write(*args, **kwargs):
        pytest.fail("Read-only adoption must not create a repository")

    monkeypatch.setattr("src.product_onboarding.repository_creation.create_confirmed_repository", forbid_provider_write)

    async def provider_readback():
        repository = await github.get_installation_repository(17, 35)
        pipeline = await github.installation_request(17, "GET", "/repos/example/existing/actions/workflows/77")
        version = secret.current_version_id(credential.secret_arn)
        payload, served_version = secret.get_secret_at_version(credential.secret_arn, version)
        identity = json.loads(payload)
        return {
            "repository": deepcopy(repository),
            "pipeline": deepcopy(pipeline),
            "environment": {
                "account_id": identity["account_id"],
                "region": identity["default_region"],
                "role_ref": identity["role_arn"],
                "source_revision": served_version,
            },
        }

    baseline = await provider_readback()
    root = path.removesuffix("/bindings")
    sources = await test_client.get(root + "/sources")
    assert sources.status_code == 200
    assert {item["provider"] for item in sources.json()["items"]} == {"github", "aws"}
    github_query = {"connection_kind": "github_installation", "connection_id": "run-owned-connection"}
    repositories = await test_client.get(root + "/sources", params=github_query)
    assert repositories.status_code == 200, repositories.text
    selected_repository = repositories.json()["items"][0]["resources"][0]
    assert (selected_repository["kind"], selected_repository["native_id"], selected_repository["source_revision"]) == (
        "repository",
        "35",
        "repo-rev",
    )
    pipelines = await test_client.get(root + "/sources", params={**github_query, "pipeline_repository_id": "35"})
    assert pipelines.status_code == 200, pipelines.text
    selected_pipeline = pipelines.json()["items"][0]["resources"][0]
    assert (selected_pipeline["kind"], selected_pipeline["native_id"], selected_pipeline["repository_id"]) == ("pipeline", "77", "35")
    aws = await test_client.get(root + "/sources", params={"connection_kind": "credential", "connection_id": credential.id})
    assert aws.status_code == 200, aws.text
    selected_environment = aws.json()["items"][0]["resources"][0]
    assert (selected_environment["kind"], selected_environment["source_revision"]) == ("environment", secret.version)
    assert_source_reuse(selected_repository, selected_environment, baseline["repository"], baseline["environment"])
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    scope = ("--product", product_id, "--project", project_id)
    cli_resources = []
    for connection_kind, connection_id, observed in (
        ("github_installation", "run-owned-connection", selected_repository),
        ("credential", credential.id, selected_environment),
    ):
        args = cli.parser().parse_args(("sources", *scope, "--connection-kind", connection_kind, "--connection-id", connection_id))
        result = await asyncio.to_thread(cli.execute, args, gateway)
        cli_resources.append(result["detail"]["items"][0]["resources"][0])
        assert cli_resources[-1]["native_id"] == observed["native_id"]
    assert_source_reuse(*cli_resources, baseline["repository"], baseline["environment"])
    assert await provider_readback() == baseline

    repository = {
        "kind": "repository",
        "provider": "github",
        "namespace": selected_repository["namespace"],
        "native_id": selected_repository["native_id"],
        "connection_ref": {"kind": "github_installation", "id": "run-owned-connection"},
    }
    pipeline = {
        **repository,
        "kind": "pipeline",
        "native_id": selected_pipeline["native_id"],
        "repository_id": selected_pipeline["repository_id"],
        "source_revision": selected_pipeline["source_revision"],
    }
    production_environment = {
        **environment,
        "environment": {
            "name": "Production",
            "environment_class": "production",
            "production": True,
        },
    }
    bindings = []
    for revision, selection in enumerate((repository, pipeline, production_environment), start=1):
        saved = await test_client.post(path, json=selection, headers=mutation(revision, f"i13_running_adopt_{revision}_123"))
        assert saved.status_code == 201, saved.text
        assert saved.json()["selection"] == selection
        bindings.append(saved.json()["id"])
        assert await provider_readback() == baseline
    profile = await test_client.get(root)
    assert profile.status_code == 200 and profile.json()["product"]["starting_point"] == "running_product"
    assert any(item.get("selection", {}).get("environment", {}).get("production") is True for item in (await test_client.get(path)).json()["items"])

    for revision, binding_id in enumerate(bindings, start=4):
        unlinked = await test_client.delete(f"{path}/{binding_id}", headers=mutation(revision, f"i13_running_unlink_{revision}_123"))
        assert unlinked.status_code == 200 and unlinked.json()["status"] == "unlinked", unlinked.text
        assert await provider_readback() == baseline
    archived = await test_client.post(
        root + "/lifecycle",
        json={"action": "archive", "reason": "Keep existing provider assets"},
        headers=mutation(7, "i13_running_archive_123"),
    )
    assert archived.status_code == 200, archived.text
    after = await provider_readback()
    assert after == baseline
    assert all(call[0].startswith("GET") for call in github.calls)
    assert all(read[0] in ("version", "secret") for read in secret.reads)
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent_db:
        stored_product = await independent_db.get(Product, (source_member.org_id, product_id))
        stored_bindings = (
            await independent_db.scalars(
                select(ProductSourceBinding).where(
                    ProductSourceBinding.tenant_id == source_member.org_id,
                    ProductSourceBinding.project_id == project_id,
                )
            )
        ).all()
        assert stored_product.archived_at is not None
        assert len(stored_bindings) == 3 and {binding.status for binding in stored_bindings} == {"unlinked"}
        handoff_count = await independent_db.scalar(
            select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "startHandoff")
        )
        assert handoff_count == 0

    record_property("scenario_id", "SCN-03")
    record_property("criterion", "AC-03")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("fixture_clock", clock.isoformat())
    record_property("provider_source_revision", json.dumps({"repository": "repo-rev", "pipeline": "pipeline-rev", "aws": secret.version}))
    record_property(
        "observation",
        json.dumps(
            {
                "baseline_sha256": sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest(),
                "unchanged": after == baseline,
            }
        ),
    )
    record_property(
        "attempt_report",
        json.dumps(
            {
                "gateway": {"adoptions": 3, "unlinks": 3, "archive_status": archived.status_code},
                "github": {"read_calls": len(github.calls), "write_calls": 0},
                "aws": {"read_calls": len(secret.reads), "write_calls": 0},
                "database": {"archived": stored_product.archived_at is not None, "unlinked_bindings": len(stored_bindings)},
            }
        ),
    )
    record_property("fixture_cleanup", "Function-scoped run-owned GitHub/AWS fixtures and SQLite engine disposed by pytest")


@pytest.mark.asyncio
async def test_repository_callbacks_cannot_duplicate_or_redirect_created_effect(
    test_client,
    db_session,
    source_member,
    monkeypatch,
    app,
    service_account_token_context,
):
    attempt = await _confirmed_repository_attempt(
        test_client,
        db_session,
        source_member,
        monkeypatch,
        False,
        False,
        False,
        False,
    )
    operation_id = attempt["operation_id"]
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "succeeded" and operation.provider_operation_id == "91"
    initial_writes = [(method, path) for method, path, _ in attempt["requests"] if method in ("POST", "PUT", "PATCH", "DELETE")]
    producer = service_account_token_context.model_copy(
        update={
            "user_id": PRODUCER,
            "org_id": source_member.org_id,
            "token_scopes": f"product-callback/{operation_id}",
            "token_use": "access",
        }
    )
    app.dependency_overrides[get_callback_user] = lambda: producer
    callback = {
        "sequence": 1,
        "binding_generation": operation.request["generation"],
        "receipt": {
            "tenant_id": source_member.org_id,
            "product_id": attempt["product_id"],
            "project_id": attempt["project_id"],
            "profile_revision": operation.expected_revision,
            "provider_operation_id": "91",
            "status": "succeeded",
        },
    }
    path = f"/v1/product-callbacks/{operation_id}"
    for payload, code in (
        (callback, "operation_resolved"),
        (callback, "operation_resolved"),
        ({**callback, "binding_generation": operation.request["generation"] + 1}, "callback_scope_mismatch"),
        ({**callback, "receipt": {**callback["receipt"], "tenant_id": "other-tenant"}}, "callback_scope_mismatch"),
        ({**callback, "receipt": {**callback["receipt"], "provider_operation_id": "another-repository"}}, "operation_resolved"),
    ):
        response = await test_client.post(path, json=payload)
        assert response.status_code == 409 and response.json()["detail"]["code"] == code, response.text
        await db_session.refresh(operation)
        assert operation.state == "succeeded" and operation.callback_sequence == 0
        assert operation.provider_operation_id == "91" and operation.receipt["native_id"] == "91"
        assert [(method, path) for method, path, _ in attempt["requests"] if method in ("POST", "PUT", "PATCH", "DELETE")] == initial_writes
