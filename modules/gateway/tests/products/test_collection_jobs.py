"""Collection admission uses real Product work leases, never caller-made authority."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from sqlalchemy import select

from src.internal.auth_deps import verify_internal_or_irsa
from src.products.models import ConnectorCollectionGrant, ConnectorModuleAdmission, ProductOperation, ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation, UserCredential
from tests.products.test_connector_grants import make_registered_selection


@pytest.fixture
async def two_sources(test_client, db_session, source_member, app, monkeypatch):
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "true")
    selection = await make_registered_selection(test_client, db_session, source_member)
    jira = await db_session.get(ProductSourceBinding, (source_member.org_id, selection["body"]["binding_id"]))
    credential = await db_session.get(UserCredential, jira.credential_id)
    credential.service, credential.provider, credential.credential_type = "jira", "jira", "basic_auth"
    jira.provider, jira.kind, jira.namespace, jira.native_id = "jira", "tracker", "https://fixture.atlassian.net", "91"
    installation = ChannelTenantMap(org_id=source_member.org_id, provider="github", provider_scope_id="27", installation_id="17")
    db_session.add(installation)
    await db_session.flush()
    github = ProductSourceBinding(
        tenant_id=source_member.org_id,
        project_id=selection["project_id"],
        installation_id=installation.id,
        kind="repository",
        provider="github",
        namespace="27",
        native_id="42",
        desired={},
        observed=jira.observed.copy(),
        status="healthy",
    )
    db_session.add(github)
    await db_session.commit()
    ownership = AsyncMock(return_value=installation)
    monkeypatch.setattr("src.products.source_routes.assert_installation_owned_by", ownership)
    monkeypatch.setattr("src.admin.installations.resolver.assert_installation_owned_by", ownership)
    grants = []
    for binding in (jira, github):
        response = await test_client.post(
            selection["path"],
            json={**selection["body"], "binding_id": binding.id},
            headers={**selection["headers"], "Idempotency-Key": "collection-grant-" + binding.id},
        )
        assert response.status_code == 201, response.text
        grants.append(response.json()["id"])
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:registered-worker",
            "agent_registry_id": "registered-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
        }
    )

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    return {
        "path": selection["path"].replace("collection-grants", "collection-jobs"),
        "headers": selection["headers"],
        "body": {"grant_ids": grants, "cutoff": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        "tenant": source_member.org_id,
        "installation": installation,
        "credential": credential,
        "service": service,
    }


async def admit(test_client, selected):
    created = await test_client.post(selected["path"], json=selected["body"], headers=selected["headers"])
    assert created.status_code == 201, created.text
    path = f"/internal/v1/collection-jobs/{selected['tenant']}/{created.json()['job_id']}"
    claim = await test_client.post(path + "/claim", json={"claim_key": "fixture_worker_claim_1234"})
    assert claim.status_code == 200, claim.text
    return created.json(), path, claim.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,kind,credential_type,native_id",
    [
        ("sonarqube", "quality_project", "api_key", "quality"),
        ("jenkins", "ci_job", "basic_auth", "folder/job"),
        ("argocd", "deployment_application", "bearer", "example"),
        ("gitlab", "repository", "api_key", "91"),
        ("pagerduty", "incident_service", "api_key", "P123"),
        ("linear", "tracker_team", "api_key", "team"),
        ("slack", "communication_channel", "oauth_token", "C123"),
    ],
)
async def test_native_grant_is_admitted_with_current_scope(test_client, db_session, two_sources, provider, kind, credential_type, native_id):
    db_session.add(ConnectorModuleAdmission(tenant_id=two_sources["tenant"], enabled=True))
    credential = two_sources["credential"]
    credential.service = credential.provider = provider
    credential.credential_type = credential_type
    if provider == "slack":
        credential.scopes = {"oauth_scopes": ["channels:read", "groups:read"]}
    grant = await db_session.get(ConnectorCollectionGrant, (two_sources["tenant"], two_sources["body"]["grant_ids"][0]))
    binding = await db_session.get(ProductSourceBinding, (two_sources["tenant"], grant.binding_id))
    binding.provider, binding.kind, binding.native_id = provider, kind, native_id
    reference = {**grant.source_selections[0], "record_type": kind, "record_id": native_id}
    grant.source_selections = [reference]
    await db_session.commit()
    _, _, claim = await admit(test_client, two_sources)
    assert len(claim["contexts"]) == 2
    resolved = []
    for request in claim["contexts"]:
        response = await test_client.post("/internal/v1/connector-contexts/resolve", json=request)
        assert response.status_code == 201, response.text
        read = await test_client.get("/internal/v1/connector-contexts/" + response.json()["handle"])
        resolved.append(read.json())
    assert any(context["provider"] == provider and context["source_selections"] == [reference] for context in resolved)


@pytest.mark.asyncio
async def test_native_grant_denies_unsupported_scope_and_credential(test_client, db_session, two_sources):
    grant = await db_session.get(ConnectorCollectionGrant, (two_sources["tenant"], two_sources["body"]["grant_ids"][0]))
    db_session.add(ConnectorModuleAdmission(tenant_id=two_sources["tenant"], enabled=True))
    binding = await db_session.get(ProductSourceBinding, (two_sources["tenant"], grant.binding_id))
    credential = two_sources["credential"]
    credential.service = credential.provider = "gitlab"
    binding.provider, binding.kind, binding.native_id = "gitlab", "repository", "non-numeric"
    grant.source_selections = [{**grant.source_selections[0], "record_id": "non-numeric", "record_type": "repository"}]
    await db_session.commit()
    response = await test_client.post(two_sources["path"], json=two_sources["body"], headers=two_sources["headers"])
    assert response.status_code == 422
    assert not (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "collectConnectorSources"))).all()
    binding.native_id = "91"
    grant.source_selections = [{**grant.source_selections[0], "record_id": "91"}]
    credential.credential_type = "basic_auth"
    await db_session.commit()
    response = await test_client.post(two_sources["path"], json=two_sources["body"], headers=two_sources["headers"])
    assert response.status_code == 422
    assert not (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "collectConnectorSources"))).all()

    credential.credential_type = "api_key"
    binding.kind = "tracker_team"
    grant.source_selections = [{**grant.source_selections[0], "record_type": "tracker_team"}]
    await db_session.commit()
    response = await test_client.post(two_sources["path"], json=two_sources["body"], headers=two_sources["headers"])
    assert response.status_code == 422
    assert not (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "collectConnectorSources"))).all()


@pytest.mark.asyncio
async def test_two_native_grants_share_only_their_current_cell(test_client, db_session, two_sources):
    db_session.add(ConnectorModuleAdmission(tenant_id=two_sources["tenant"], enabled=True))
    first = await db_session.get(ConnectorCollectionGrant, (two_sources["tenant"], two_sources["body"]["grant_ids"][0]))
    second = await db_session.get(ConnectorCollectionGrant, (two_sources["tenant"], two_sources["body"]["grant_ids"][1]))
    first_binding = await db_session.get(ProductSourceBinding, (two_sources["tenant"], first.binding_id))
    second_binding = await db_session.get(ProductSourceBinding, (two_sources["tenant"], second.binding_id))
    credential = two_sources["credential"]
    credential.provider = credential.service = "gitlab"
    credential.credential_type = "api_key"
    first_binding.provider, first_binding.kind, first_binding.native_id = "gitlab", "repository", "91"
    first.source_selections = [{**first.source_selections[0], "record_type": "repository"}]
    second_credential = UserCredential(
        org_id=two_sources["tenant"],
        user_id=credential.user_id,
        service="linear",
        provider="linear",
        credential_type="api_key",
        label="native-pair-fixture",
        secret_arn="synthetic-test-ref",
    )
    db_session.add(second_credential)
    await db_session.flush()
    second_binding.installation_id = None
    second_binding.credential_id = second_credential.id
    second_binding.provider, second_binding.kind, second_binding.native_id = "linear", "tracker_team", "team"
    second.source_selections = [
        {**second.source_selections[0], "source_connection_id": second_credential.id, "record_type": "tracker_team", "record_id": "team"}
    ]
    await db_session.commit()
    _, _, claim = await admit(test_client, two_sources)
    assert len(claim["contexts"]) == 2
    for request in claim["contexts"]:
        response = await test_client.post("/internal/v1/connector-contexts/resolve", json=request)
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_trusted_job_claim_context_renew_and_finish(test_client, db_session, two_sources):
    created, path, claim = await admit(test_client, two_sources)
    jobs = (
        await db_session.scalars(
            select(ProductOperation).where(ProductOperation.operation.in_(("collectConnectorSources", "collectConnectorSource")))
        )
    ).all()
    assert len(jobs) == 3 and all(job.state == "running" for job in jobs)
    delegates = (await db_session.scalars(select(CredentialServiceDelegation))).all()
    assert {(grant.credential_id, grant.installation_id) for grant in delegates} == {
        (two_sources["credential"].id, None),
        (None, two_sources["installation"].id),
    }
    replay = await test_client.post(two_sources["path"], json=two_sources["body"], headers=two_sources["headers"])
    assert replay.status_code == 200 and replay.json()["job_id"] == created["job_id"]
    handles = []
    for requested in claim["contexts"]:
        response = await test_client.post("/internal/v1/connector-contexts/resolve", json=requested)
        assert response.status_code == 201, response.text
        handles.append(response.json()["handle"])
        read = await test_client.get("/internal/v1/connector-contexts/" + handles[-1])
        assert read.status_code == 200 and read.json()["attempt_id"] == claim["attempt_id"], read.text
    competing = await test_client.post(path + "/claim", json={"claim_key": "other_worker_claim_1234"})
    assert competing.status_code == 409
    lease = {key: claim[key] for key in ("attempt_id", "lease_fence")}
    assert (await test_client.post(path + "/renew", json=lease)).status_code == 200
    assert (await test_client.post(path + "/renew", json={**lease, "lease_fence": 2})).status_code == 409
    completed = await test_client.post(path + "/finish", json={**lease, "state": "succeeded", "content_digest": "a" * 64})
    assert completed.status_code == 200, completed.text
    assert (await test_client.get("/internal/v1/connector-contexts/" + handles[0])).status_code == 404
    assert (await test_client.post(path + "/finish", json={**lease, "state": "succeeded", "content_digest": "a" * 64})).status_code == 200


@pytest.mark.asyncio
async def test_off_creates_no_job_and_revocation_denies_renew(test_client, db_session, two_sources, monkeypatch):
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "false")
    response = await test_client.post(two_sources["path"], json=two_sources["body"], headers=two_sources["headers"])
    assert response.status_code == 503
    assert not (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "collectConnectorSources"))).all()
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "true")
    _, path, claim = await admit(test_client, two_sources)
    grant = await db_session.get(ConnectorCollectionGrant, (two_sources["tenant"], two_sources["body"]["grant_ids"][0]))
    grant.state = "revoked"
    await db_session.commit()
    lease = {key: claim[key] for key in ("attempt_id", "lease_fence")}
    assert (await test_client.post(path + "/renew", json=lease)).status_code == 409
    assert (await test_client.post(path + "/finish", json={**lease, "state": "failed"})).status_code == 200


@pytest.mark.asyncio
async def test_expired_lease_reclaims_with_new_fence(test_client, db_session, two_sources):
    created, path, first = await admit(test_client, two_sources)
    job = await db_session.get(ProductOperation, (two_sources["tenant"], created["job_id"]))
    job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    second = await test_client.post(path + "/claim", json={"claim_key": "successor_worker_1234"})
    assert second.status_code == 200, second.text
    assert second.json()["lease_fence"] == first["lease_fence"] + 1
    assert second.json()["attempt_id"] != first["attempt_id"]
    stale = await test_client.post(path + "/finish", json={"attempt_id": first["attempt_id"], "lease_fence": first["lease_fence"], "state": "failed"})
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_installation_dispute_denies_existing_work(test_client, db_session, two_sources):
    _, path, claim = await admit(test_client, two_sources)
    two_sources["installation"].ownership_disputed = True
    await db_session.commit()
    response = await test_client.post(path + "/renew", json={key: claim[key] for key in ("attempt_id", "lease_fence")})
    assert response.status_code in (404, 409), response.text


@pytest.mark.asyncio
async def test_expired_job_retires_children_and_delegation(test_client, db_session, two_sources):
    created, path, _ = await admit(test_client, two_sources)
    job = await db_session.get(ProductOperation, (two_sources["tenant"], created["job_id"]))
    job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    response = await test_client.post(path + "/claim", json={"claim_key": "expired_worker_claim_1234"})
    assert response.status_code == 409
    await db_session.refresh(job)
    assert job.state == "expired"
    assert all(grant.revoked_at is not None for grant in (await db_session.scalars(select(CredentialServiceDelegation))).all())


@pytest.mark.asyncio
async def test_removed_delegation_denies_renew_but_allows_failure_receipt(test_client, db_session, two_sources):
    _, path, claim = await admit(test_client, two_sources)
    delegation = await db_session.scalar(select(CredentialServiceDelegation))
    await db_session.delete(delegation)
    await db_session.commit()
    lease = {key: claim[key] for key in ("attempt_id", "lease_fence")}
    assert (await test_client.post(path + "/renew", json=lease)).status_code == 409
    assert (await test_client.post(path + "/finish", json={**lease, "state": "failed"})).status_code == 200


@pytest.mark.asyncio
async def test_terminal_receipt_recovery_preserves_retired_authority(test_client, db_session, two_sources, app):
    from src.products.models import ConnectorOperationContext

    created, path, claim = await admit(test_client, two_sources)
    for requested in claim["contexts"]:
        assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=requested)).status_code == 201
    lease = {key: claim[key] for key in ("attempt_id", "lease_fence")}
    receipt = {"content_digest": "a" * 64, "batch_id": "12345678-1234-1234-1234-123456789abc", "items": 5}
    finish = {**lease, "state": "succeeded", **receipt}
    assert (await test_client.post(path + "/finish", json=finish)).status_code == 200
    assert (await test_client.post(path + "/finish", json={**finish, "items": 6})).status_code == 409
    assert (await test_client.post(path + "/finish", json=finish)).status_code == 200
    jobs = (
        await db_session.scalars(
            select(ProductOperation)
            .where(ProductOperation.id.in_([created["job_id"], *[requested["operation_id"] for requested in claim["contexts"]]]))
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(jobs) == 3 and all(job.state == "succeeded" and job.receipt == receipt for job in jobs)
    contexts = (await db_session.scalars(select(ConnectorOperationContext).execution_options(populate_existing=True))).all()
    delegations = (await db_session.scalars(select(CredentialServiceDelegation).execution_options(populate_existing=True))).all()
    assert len(contexts) == 2 and all(context.revoked_at for context in contexts)
    assert len(delegations) == 2 and all(delegation.revoked_at for delegation in delegations)
    retired = [(entry.id, entry.revoked_at) for entry in [*contexts, *delegations]]
    job = next(job for job in jobs if job.id == created["job_id"])
    job.lease_until = job.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    original = {"claim_key": "fixture_worker_claim_1234"}
    for _ in range(2):
        recovered = await test_client.post(path + "/receipt", json=original)
        assert recovered.status_code == 200, recovered.text
        assert recovered.headers["Cache-Control"] == "no-store"
        assert recovered.json() == {
            "job_id": created["job_id"],
            "tenant_id": two_sources["tenant"],
            "state": "succeeded",
            **lease,
            "receipt": receipt,
        }
    assert (await test_client.post(path + "/claim", json=original)).status_code == 409
    assert (await test_client.post(path + "/renew", json=lease)).status_code == 409
    assert (await test_client.post(path + "/receipt", json={"claim_key": "different_worker_claim"})).status_code == 404
    assert (await test_client.post(path.replace(two_sources["tenant"], "different-tenant") + "/receipt", json=original)).status_code == 404
    for requested in claim["contexts"]:
        assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=requested)).status_code != 201
    for entry, snapshot in zip([*contexts, *delegations], retired):
        await db_session.refresh(entry)
        assert (entry.id, entry.revoked_at) == snapshot
    await db_session.refresh(job)
    assert (job.lease_owner, job.attempt_count, job.receipt) == (claim["attempt_id"], claim["lease_fence"], receipt)

    async def wrong_consumer(request: Request):
        request.state.token_context = two_sources["service"].model_copy(update={"agent_registry_id": "different-worker"})

    app.dependency_overrides[verify_internal_or_irsa] = wrong_consumer
    assert (await test_client.post(path + "/receipt", json=original)).status_code == 404


@pytest.mark.asyncio
async def test_receipt_probe_does_not_claim_or_extend_running_work(test_client, db_session, two_sources):
    created, path, claim = await admit(test_client, two_sources)
    job = await db_session.get(ProductOperation, (two_sources["tenant"], created["job_id"]), populate_existing=True)
    before = (job.lease_owner, job.attempt_count, job.lease_until)
    successor = {"claim_key": "successor_worker_1234"}
    probe = await test_client.post(path + "/receipt", json=successor)
    assert probe.status_code == 200 and probe.json()["state"] == "running" and probe.json()["receipt"] is None
    assert "attempt_id" not in probe.json() and "lease_fence" not in probe.json()
    await db_session.refresh(job)
    assert (job.lease_owner, job.attempt_count, job.lease_until) == before
    assert (await test_client.post(path + "/claim", json=successor)).status_code == 409
    job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert (await test_client.post(path + "/receipt", json=successor)).json()["receipt"] is None
    reclaimed = await test_client.post(path + "/claim", json=successor)
    assert reclaimed.status_code == 200 and reclaimed.json()["lease_fence"] == claim["lease_fence"] + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{"items": 5}, {"batch_id": "12345678-1234-1234-1234-123456789abc"}, {"items": -1}])
async def test_finish_rejects_partial_receipt(test_client, two_sources, metadata):
    _, path, claim = await admit(test_client, two_sources)
    lease = {key: claim[key] for key in ("attempt_id", "lease_fence")}
    response = await test_client.post(path + "/finish", json={**lease, "state": "succeeded", "content_digest": "a" * 64, **metadata})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_go_worker_recovers_lost_committed_finish_response(test_client, db_session, two_sources):
    from src.products.models import ConnectorOperationContext

    binary = os.getenv("DEVLAKE_S03_RECOVERY_TEST_BINARY")
    if not binary:
        pytest.skip("requires pinned Go worker recovery test binary")
    created, path, claim = await admit(test_client, two_sources)
    for requested in claim["contexts"]:
        assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=requested)).status_code == 201
    loop = asyncio.get_running_loop()
    requests = []
    failures = []

    class GatewayBridge(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                assert self.path in (path + "/finish", path + "/receipt")
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                forwarded = asyncio.run_coroutine_threadsafe(test_client.post(self.path, json=json.loads(payload)), loop).result(timeout=20)
                assert forwarded.status_code == 200, forwarded.text
                requests.append((self.path, json.loads(payload), forwarded.json()))
                if self.path.endswith("/finish"):
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(forwarded.content)))
                self.end_headers()
                self.wfile.write(forwarded.content)
            except Exception as error:
                failures.append(str(error))
                self.send_error(500)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), GatewayBridge)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        fixture = {"url": f"http://127.0.0.1:{server.server_port}", "claim_key": "fixture_worker_claim_1234", "claim": claim}
        process = await asyncio.create_subprocess_exec(
            binary,
            "-test.run=^TestGatewayCommittedReceiptRecovery$",
            "-test.v",
            "-test.timeout=60s",
            env={**os.environ, "DEVLAKE_S03_RECOVERY_FIXTURE": json.dumps(fixture)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, output.decode()
        assert b"--- PASS: TestGatewayCommittedReceiptRecovery" in output
        assert failures == []
        assert [entry[0] for entry in requests] == [path + "/finish", path + "/receipt", path + "/receipt"]
        receipt = {key: requests[0][1][key] for key in ("batch_id", "content_digest", "items")}
        for _, _, recovered in requests[1:]:
            assert recovered["receipt"] == receipt
            assert recovered["attempt_id"] == claim["attempt_id"] and recovered["lease_fence"] == claim["lease_fence"]
        job = await db_session.get(ProductOperation, (two_sources["tenant"], created["job_id"]), populate_existing=True)
        assert job.state == "succeeded" and job.receipt == receipt and job.attempt_count == 1
        contexts = (await db_session.scalars(select(ConnectorOperationContext).execution_options(populate_existing=True))).all()
        delegations = (await db_session.scalars(select(CredentialServiceDelegation).execution_options(populate_existing=True))).all()
        assert len(contexts) == 2 and all(context.revoked_at for context in contexts)
        assert len(delegations) == 2 and all(delegation.revoked_at for delegation in delegations)
        assert (await test_client.post(path + "/claim", json={"claim_key": fixture["claim_key"]})).status_code == 409
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
