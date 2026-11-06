"""Jenkins Profile selection through the production Go executable and Gateway."""

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException, Request
from sqlalchemy import update

from src.internal.auth_deps import verify_internal_or_irsa
from src.product_onboarding import jenkins
from src.products.models import ConnectorModuleAdmission, ProductSourceBinding
from src.shared.models.vault import UserCredential
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i08_api import mutation


@pytest.fixture
async def jenkins_source(test_client, db_session, source_member, monkeypatch):
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    root = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="jenkins",
        credential_type="basic_auth",
        label="Controlled Jenkins",
        secret_arn="fixture-only",
    )
    db_session.add_all([credential, ConnectorModuleAdmission(tenant_id=source_member.org_id, enabled=True)])
    await db_session.commit()
    state = {"reads": [], "version": "fixture-version", "provider_failure": False, "revoke": False}
    payload = json.dumps({"url": "https://jenkins.example.test/jenkins", "username": "fixture-reader", "token": "fixture-only"})
    secrets = Mock(
        current_version_id=Mock(side_effect=lambda _: state["version"]),
        get_secret_at_version=Mock(side_effect=lambda _, version: (payload, version)),
    )
    monkeypatch.setattr("src.auth.vault_routes.get_secrets_manager", lambda: secrets)

    def provider(request):
        state["reads"].append(request)
        assert request.method == "GET" and request.url.host == "jenkins.example.test"
        path = request.url.raw_path.split(b"?", 1)[0]
        assert request.headers["authorization"].startswith("Basic ")
        assert path == b"/jenkins/job/team%20space/job/api/api/json"
        assert request.url.params["tree"] == "fullName,name,_class"
        if state["provider_failure"]:
            return httpx.Response(503, text="fixture unavailable")
        if state.get("wrong_job"):
            return httpx.Response(
                200, json={"_class": "hudson.model.FreeStyleProject", "name": "private-sibling", "fullName": "team space/private-sibling"}
            )
        return httpx.Response(
            200,
            json={
                "_class": "hudson.model.FreeStyleProject",
                "name": "api",
                "fullName": "team space/api",
                "unrequested": "private-sibling",
            },
        )

    # Inject only the native HTTP transport; preserve bounded reads, secret
    # version checks, Gateway routes and production Go provider execution.
    monkeypatch.setattr(
        jenkins,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(provider), **kwargs),
            BasicAuth=httpx.BasicAuth,
        ),
    )
    selection = {
        "kind": "ci_job",
        "provider": "jenkins",
        "namespace": "team space",
        "native_id": "team space/api",
        "connection_ref": {"kind": "credential", "id": credential.id},
    }
    linked = await test_client.post(root + "/bindings", json=selection, headers=mutation(1, "jenkins_binding_123456"))
    assert linked.status_code == 201, linked.text
    assert linked.json()["status"] == "unverified"
    binding_id = linked.json()["id"]
    verified = await test_client.post(root + f"/bindings/{binding_id}/verify", headers=mutation(2, "jenkins_verify_123456"))
    assert verified.status_code == 202 and verified.json()["status"] == "succeeded", verified.text
    listed = await test_client.get(root + "/sources")
    selected = next(source for source in listed.json()["items"] if source["provider"] == "jenkins")
    assert selected["connection_ref"] == selection["connection_ref"] and selected["capabilities"] == ["ci_job"]
    started = await test_client.post(root + "/onboarding-sessions", json={}, headers=mutation(2, "jenkins_session_123456"))
    assert started.status_code == 201, started.text
    return {"root": root, "binding_id": binding_id, "session_id": started.json()["id"], "state": state, "credential": credential}


@pytest.fixture
async def go_profile_process(test_client, db_session, app, source_member, jenkins_source, monkeypatch, tmp_path):
    executable = os.getenv("DEVLAKE_CONNECTOR_BINARY")
    if not executable:
        pytest.skip("set DEVLAKE_PROFILE_PYTEST when running scripts/test-providers.sh for the real Go process proof")
    loop = asyncio.get_running_loop()
    state = jenkins_source["state"]
    state["callbacks"] = []
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:registered-connector-api",
            "agent_registry_id": "registered-connector-api",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["connector:profile:read"],
        }
    )

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified

    async def gateway(path, body):
        if path.endswith("/jenkins-page") and state["revoke"]:
            await db_session.execute(
                update(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == source_member.org_id,
                    ProductSourceBinding.id == jenkins_source["binding_id"],
                )
                .values(status="revoked")
            )
            await db_session.commit()
        return await test_client.post(path, json=body)

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - stdlib HTTP handler interface
            assert self.path == "/credentials"
            self.reply(
                200,
                json.dumps(
                    {
                        "AccessKeyId": "fixture-access",
                        "SecretAccessKey": "fixture-secret",
                        "Token": "fixture-session",
                        "Expiration": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    }
                ).encode(),
            )

        def do_POST(self):  # noqa: N802 - stdlib HTTP handler interface
            assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=fixture-access/")
            assert self.headers["X-Amz-Security-Token"] == "fixture-session"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            original = {"tenant_id", "product_id", "project_id", "operation_id", "attempt_id", "binding_id", "cursor", "limit"}
            if self.path.endswith("/provider-page"):
                assert set(body) == original | {"method", "path", "query"} | ({"body"} if body.get("method") == "POST" else set())
            else:
                assert set(body) == original
            state["callbacks"].append(self.path)
            result = asyncio.run_coroutine_threadsafe(gateway(self.path, body), loop).result(timeout=15)
            assert "private-sibling" not in result.text
            self.reply(result.status_code, result.content)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    callback = f"https://127.0.0.1:{server.server_port}"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "CONNECTOR_", "FEATURE_DEVLAKE_")) and key != "SSL_CERT_FILE"
    }
    environment.update(
        {
            "FEATURE_DEVLAKE_CONNECTORS_ENABLED": "true",
            "CONNECTOR_API_LISTEN": f"127.0.0.1:{port}",
            "CONNECTOR_GATEWAY_URL": callback,
            "AWS_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": callback + "/credentials",
            "SSL_CERT_FILE": str(cert_path),
        }
    )
    process = subprocess.Popen([executable], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    address = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(
        "src.product_onboarding.discovery.configured_devlake", lambda: (address + "/v1/profile-discovery", "registered-connector-api")
    )
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            for _ in range(100):
                try:
                    assert (await client.get(address + "/healthz")).status_code == 200
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("Go connector did not start")
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


async def discover(client, source, key, revision=2):
    response = await client.post(
        source["root"] + f"/onboarding-sessions/{source['session_id']}/discoveries",
        json={"binding_ids": [source["binding_id"]], "adapter": "devlake"},
        headers=mutation(revision, key),
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.asyncio
async def test_jenkins_real_go_profile_selection_readback_and_go_loss(test_client, jenkins_source, go_profile_process):
    source = jenkins_source
    discovery = await discover(test_client, source, "jenkins_discovery_123456")
    assert discovery["coverage"] == "complete", discovery
    assert len(discovery["resources"]) == 1
    item = discovery["resources"][0]
    assert (item["kind"], item["namespace"], item["native_id"], item["full_name"]) == ("ci_job", "team space", "team space/api", "team space/api")
    assert item["provenance"] == "observation_only"
    applied = await test_client.post(
        source["root"] + f"/onboarding-sessions/{source['session_id']}/source-enrichment",
        json={"operation_id": discovery["id"]},
        headers=mutation(2, "jenkins_apply_123456"),
    )
    assert applied.status_code == 200 and applied.json()["revision"] == 3, applied.text
    readback = await test_client.get(source["root"])
    assert readback.status_code == 200, readback.text
    reference = readback.json()["configuration"]["advanced"]["pipelines"][0]
    assert reference["record_type"] == "ci_job" and reference["record_id"] == "team space/api"
    assert reference["record_revision"] == item["source_revision"]
    assert len(source["state"]["reads"]) == 2
    assert source["state"]["callbacks"] == ["/internal/v1/profile-discovery/resolve", "/internal/v1/profile-discovery/jenkins-page"]
    go_profile_process.terminate()
    await asyncio.to_thread(go_profile_process.wait, timeout=10)
    failed = await discover(test_client, source, "jenkins_stopped_123456", revision=3)
    assert failed["coverage"] == "unavailable" and not failed.get("resources")
    assert len(source["state"]["reads"]) == 2  # no native fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoke", "provider_failure", "wrong_job"])
async def test_jenkins_real_go_rejects_revoked_work_and_provider_failure(test_client, jenkins_source, go_profile_process, failure):
    source = jenkins_source
    source["state"][failure] = True
    result = await discover(test_client, source, "jenkins_denied_123456")
    assert result["coverage"] in ("unavailable", "denied") and not result.get("resources")
    assert len(source["state"]["reads"]) == (1 if failure == "revoke" else 2)
    readback = await test_client.get(source["root"])
    assert not (readback.json()["configuration"].get("advanced") or {}).get("pipelines")


@pytest.mark.asyncio
async def test_jenkins_flag_off_and_secret_rotation_fail_before_provider(test_client, jenkins_source, monkeypatch):
    source = jenkins_source
    state = source["state"]
    credential = source["credential"]
    state["version"] = "rotated-version"
    secrets = Mock(current_version_id=Mock(return_value=state["version"]))
    with pytest.raises(HTTPException) as failure:
        await jenkins.read_jenkins(credential, secrets, name="team space/api", expected_version="fixture-version")
    assert failure.value.status_code == 409
    secrets.get_secret_at_version.assert_not_called()
    assert len(state["reads"]) == 1
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
    sources = await test_client.get(source["root"] + "/sources")
    assert all(item["provider"] != "jenkins" for item in sources.json()["items"])
    bindings = await test_client.get(source["root"] + "/bindings")
    assert bindings.json()["items"][0]["status"] == "restricted"
    assert len(state["reads"]) == 1


@pytest.mark.asyncio
async def test_sonarqube_real_go_profile_and_stopped_service(test_client, db_session, source_member, jenkins_source, go_profile_process, monkeypatch):
    from src.product_onboarding import expanded_provider

    source = jenkins_source
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="sonarqube",
        credential_type="api_key",
        label="Controlled Sonar",
        secret_arn="fixture-sonar",
    )
    db_session.add(credential)
    await db_session.commit()
    secrets = Mock(
        current_version_id=Mock(return_value="fixture-version"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://sonar.example.test", "token": "fixture-only"}), "fixture-version")),
    )
    monkeypatch.setattr("src.auth.vault_routes.get_secrets_manager", lambda: secrets)
    reads = []

    def provider(request):
        reads.append(request)
        assert request.url.host == "sonar.example.test" and request.headers["authorization"] == "Bearer fixture-only"
        assert request.method == "GET"
        if request.url.path == "/api/components/show":
            assert request.url.params["component"] == "team:api"
            return httpx.Response(200, json={"component": {"key": "team:api", "name": "API"}})
        assert request.url.path == "/api/components/search_projects"
        assert dict(request.url.params) == {"p": "1", "ps": "100"}
        return httpx.Response(200, json={"paging": {"pageIndex": 1, "pageSize": 100, "total": 1}, "components": [{"key": "team:api", "name": "API"}]})

    monkeypatch.setattr(
        expanded_provider,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(provider), **kwargs),
        ),
    )
    selection = {
        "kind": "quality_project",
        "provider": "sonarqube",
        "namespace": "/",
        "native_id": "team:api",
        "connection_ref": {"kind": "credential", "id": credential.id},
    }
    linked = await test_client.post(source["root"] + "/bindings", json=selection, headers=mutation(2, "sonar_binding_123456"))
    assert linked.status_code == 201, linked.text
    verified = await test_client.post(source["root"] + f"/bindings/{linked.json()['id']}/verify", headers=mutation(3, "sonar_verify_123456"))
    assert verified.status_code == 202 and verified.json()["status"] == "succeeded", verified.text
    candidate = {**source, "binding_id": linked.json()["id"]}
    discovered = await discover(test_client, candidate, "sonar_discovery_123456", revision=3)
    assert discovered["coverage"] == "complete" and len(discovered["resources"]) == 1, discovered
    item = discovered["resources"][0]
    assert (item["kind"], item["namespace"], item["native_id"]) == ("quality_project", "/", "team:api")
    assert item["provenance"] == "observation_only"
    applied = await test_client.post(
        source["root"] + f"/onboarding-sessions/{candidate['session_id']}/source-enrichment",
        json={"operation_id": discovered["id"]},
        headers=mutation(3, "sonar_apply_123456"),
    )
    assert applied.status_code == 200 and applied.json()["revision"] == 4, applied.text
    profile = await test_client.get(source["root"])
    reference = profile.json()["configuration"]["advanced"]["observability"][0]
    assert reference["record_type"] == "quality_project" and reference["record_id"] == "team:api"
    assert reference["record_revision"] == item["source_revision"]
    assert source["state"]["callbacks"][-3:] == [
        "/internal/v1/profile-discovery/resolve",
        "/internal/v1/profile-discovery/provider-page",
        "/internal/v1/profile-discovery/resolve",
    ]
    assert len(reads) == 2
    go_profile_process.terminate()
    await asyncio.to_thread(go_profile_process.wait, timeout=10)
    failed = await discover(test_client, candidate, "sonar_stopped_123456", revision=4)
    assert failed["coverage"] == "unavailable" and not failed.get("resources")
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_sonarqube_real_go_does_not_select_by_display_name(
    test_client, db_session, source_member, jenkins_source, go_profile_process, monkeypatch
):
    from src.product_onboarding import expanded_provider

    source = jenkins_source
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="sonarqube",
        credential_type="api_key",
        label="Controlled Sonar",
        secret_arn="fixture-sonar-collision",
    )
    db_session.add(credential)
    await db_session.commit()
    secrets = Mock(
        current_version_id=Mock(return_value="fixture-version"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://sonar.example.test", "token": "fixture-only"}), "fixture-version")),
    )
    monkeypatch.setattr("src.auth.vault_routes.get_secrets_manager", lambda: secrets)
    reads = []

    def provider(request):
        reads.append(request)
        assert request.url.host == "sonar.example.test" and request.headers["authorization"] == "Bearer fixture-only"
        if request.url.path == "/api/components/show":
            assert request.url.params["component"] == "team:api"
            return httpx.Response(200, json={"component": {"key": "team:api", "name": "API"}})
        assert request.url.path == "/api/components/search_projects"
        assert dict(request.url.params) == {"p": "1", "ps": "100"}
        return httpx.Response(
            200,
            json={
                "paging": {"pageIndex": 1, "pageSize": 100, "total": 2},
                "components": [{"key": "team:api", "name": "API"}, {"key": "other:api", "name": "API"}],
            },
        )

    monkeypatch.setattr(
        expanded_provider,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(provider), **kwargs),
        ),
    )
    selection = {
        "kind": "quality_project",
        "provider": "sonarqube",
        "namespace": "/",
        "native_id": "team:api",
        "connection_ref": {"kind": "credential", "id": credential.id},
    }
    bound = await test_client.post(source["root"] + "/bindings", json=selection, headers=mutation(2, "collision_binding_123456"))
    assert bound.status_code == 201, bound.text
    verified = await test_client.post(source["root"] + f"/bindings/{bound.json()['id']}/verify", headers=mutation(3, "collision_verify_123456"))
    assert verified.status_code == 202 and verified.json()["status"] == "succeeded", verified.text
    discovered = await discover(test_client, {**source, "binding_id": bound.json()["id"]}, "collision_discovery_123456", revision=3)
    assert discovered["coverage"] == "complete", discovered
    assert {(item["kind"], item["namespace"], item["native_id"], item["name"]) for item in discovered["resources"]} == {
        ("quality_project", "/", "team:api", "API"),
        ("quality_project", "/", "other:api", "API"),
    }
    assert len({item["source_revision"] for item in discovered["resources"]}) == 2
    applied = await test_client.post(
        source["root"] + f"/onboarding-sessions/{source['session_id']}/source-enrichment",
        json={"operation_id": discovered["id"]},
        headers=mutation(3, "collision_apply_123456"),
    )
    assert applied.status_code == 200 and applied.json()["revision"] == 4, applied.text
    profile = await test_client.get(source["root"])
    references = profile.json()["configuration"]["advanced"]["observability"]
    assert len(references) == 1
    assert (references[0]["record_type"], references[0]["record_id"]) == ("quality_project", "team:api")
    assert len(reads) == 2


EXPANDED_SOURCES = [
    (
        "argocd",
        "deployment_application",
        "team/ns",
        "api",
        "bearer",
        "https://argo.example.test",
        "advanced",
        "deployments",
        {"metadata": {"name": "api", "namespace": "ns"}, "spec": {"project": "team"}},
        {"items": [{"metadata": {"name": "api", "namespace": "ns"}, "spec": {"project": "team"}}]},
    ),
    (
        "gitlab",
        "repository",
        "77",
        "901",
        "api_key",
        "https://gitlab.example.test",
        "basic",
        "repositories",
        {"id": 901, "name": "API", "path_with_namespace": "team/api", "namespace": {"id": 77, "kind": "group"}},
        [{"id": 901, "name": "API", "path_with_namespace": "team/api", "namespace": {"id": 77, "kind": "group"}}],
    ),
    (
        "pagerduty",
        "incident_service",
        "/",
        "P123",
        "api_key",
        "https://api.pagerduty.com",
        "advanced",
        "support",
        {"service": {"id": "P123", "name": "API"}},
        {"offset": 0, "limit": 100, "more": False, "services": [{"id": "P123", "name": "API"}]},
    ),
    (
        "linear",
        "tracker_team",
        "/",
        "team-a",
        "bearer",
        "https://api.linear.app",
        "basic",
        "trackers",
        {"data": {"team": {"id": "team-a", "name": "Engineering"}}},
        {
            "data": {
                "teams": {"nodes": [{"id": "team-a", "name": "Engineering", "key": "ENG"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}
            }
        },
    ),
    (
        "slack",
        "communication_channel",
        "T123",
        "C123",
        "oauth_token",
        "https://slack.com",
        "basic",
        "collaboration",
        {"ok": True, "channel": {"id": "C123", "name": "team", "is_channel": True, "context_team_id": "T123"}},
        {
            "ok": True,
            "channels": [{"id": "C123", "name": "team", "is_channel": True, "context_team_id": "T123"}],
            "response_metadata": {"next_cursor": ""},
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,kind,namespace,native_id,credential_type,endpoint,section,field,selected,listing", EXPANDED_SOURCES)
async def test_expanded_real_go_profile_selection_pages_and_go_loss(
    test_client,
    db_session,
    source_member,
    jenkins_source,
    go_profile_process,
    monkeypatch,
    provider,
    kind,
    namespace,
    native_id,
    credential_type,
    endpoint,
    section,
    field,
    selected,
    listing,
):
    from src.product_onboarding import expanded_provider

    source = jenkins_source
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service=provider,
        credential_type=credential_type,
        label=f"Controlled {provider}",
        secret_arn=f"fixture-{provider}",
        scopes={"oauth_scopes": ["channels:read", "groups:read"]} if provider == "slack" else None,
    )
    db_session.add(credential)
    await db_session.commit()
    secrets = Mock(
        current_version_id=Mock(return_value="fixture-version"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": endpoint, "token": "fixture-only"}), "fixture-version")),
    )
    monkeypatch.setattr("src.auth.vault_routes.get_secrets_manager", lambda: secrets)
    reads = []

    def provider_api(request):
        reads.append(request)
        assert request.url.host == httpx.URL(endpoint).host and request.headers.get("authorization", request.headers.get("private-token"))
        if provider == "argocd":
            assert request.url.path in ("/api/v1/applications/api", "/api/v1/applications")
            if request.url.path.endswith("/api"):
                assert request.url.params["appNamespace"] == namespace.split("/", 1)[1]
            return httpx.Response(200, json=selected if request.url.path.endswith("/api") else listing)
        if provider == "gitlab":
            assert request.url.path.startswith("/api/v4/")
            if request.url.path.endswith("/projects/901"):
                return httpx.Response(200, json=selected)
            if request.url.path.endswith("/subgroups"):
                return httpx.Response(200, json=[{"id": 78, "name": "Subteam", "full_path": "team/subteam", "parent_id": 77}])
            assert request.url.path.endswith("/groups/77/projects")
            return httpx.Response(200, json=listing)
        if provider == "pagerduty":
            assert request.url.path in ("/services/P123", "/services")
            return httpx.Response(200, json=selected if request.url.path.endswith("P123") else listing)
        if provider == "linear":
            assert request.method == "POST" and request.url.path == "/graphql"
            assert request.headers["Authorization"] == "Bearer fixture-only"
            query = request.content.decode()
            assert "query" in query and ("team(id:" in query or "teams(first: 100" in query)
            return httpx.Response(200, json=selected if "team(id:" in query else listing)
        assert provider == "slack" and request.url.path in ("/api/conversations.info", "/api/conversations.list")
        return httpx.Response(200, json=selected if request.url.path.endswith(".info") else listing)

    monkeypatch.setattr(
        expanded_provider,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(provider_api), **kwargs),
            HTTPError=httpx.HTTPError,
        ),
    )
    binding = {
        "kind": kind,
        "provider": provider,
        "namespace": namespace,
        "native_id": native_id,
        "connection_ref": {"kind": "credential", "id": credential.id},
    }
    linked = await test_client.post(source["root"] + "/bindings", json=binding, headers=mutation(2, f"{provider}_binding_123456"))
    assert linked.status_code == 201, linked.text
    verified = await test_client.post(source["root"] + f"/bindings/{linked.json()['id']}/verify", headers=mutation(3, f"{provider}_verify_123456"))
    assert verified.status_code == 202 and verified.json()["status"] == "succeeded", verified.text
    chosen = {**source, "binding_id": linked.json()["id"]}
    result = await discover(test_client, chosen, f"{provider}_discovery_123456", revision=3)
    assert result["coverage"] == "complete" and result["resources"], result
    matches = [item for item in result["resources"] if (item["kind"], item["namespace"], item["native_id"]) == (kind, namespace, native_id)]
    assert len(matches) == 1 and matches[0]["provenance"] == "observation_only", result
    assert all(item["provenance"] == "observation_only" for item in result["resources"])
    if provider == "gitlab":
        navigation = [item for item in result["resources"] if item["kind"] == "namespace"]
        assert len(navigation) == 1 and navigation[0]["native_id"] == "78" and navigation[0]["namespace"] == "77"
    applied = await test_client.post(
        source["root"] + f"/onboarding-sessions/{chosen['session_id']}/source-enrichment",
        json={"operation_id": result["id"]},
        headers=mutation(3, f"{provider}_apply_123456"),
    )
    assert applied.status_code == 200 and applied.json()["revision"] == 4, applied.text
    profile = await test_client.get(source["root"])
    reference = profile.json()["configuration"][section][field][0]
    assert (reference["record_type"], reference["record_id"], reference["record_revision"]) == (kind, native_id, matches[0]["source_revision"])
    assert len(profile.json()["configuration"][section][field]) == 1
    assert source["state"]["callbacks"].count("/internal/v1/profile-discovery/provider-page") == (2 if provider == "gitlab" else 1)
    expected_reads = 3 if provider == "gitlab" else 2
    assert len(reads) == expected_reads
    if provider == "gitlab":

        async def native_fallback(*_args, **_kwargs):
            pytest.fail("selected DevLake connection used native GitLab fallback")

        monkeypatch.setattr("src.product_onboarding.source_discovery.gitlab_resources", native_fallback)
    go_profile_process.terminate()
    await asyncio.to_thread(go_profile_process.wait, timeout=10)
    stopped = await discover(test_client, chosen, f"{provider}_stopped_123456", revision=4)
    assert stopped["coverage"] == "unavailable" and not stopped.get("resources")
    assert len(reads) == expected_reads
    if provider == "gitlab":
        unchanged = await test_client.get(source["root"])
        assert unchanged.json()["revision"] == 4
        assert unchanged.json()["configuration"][section][field] == profile.json()["configuration"][section][field]
