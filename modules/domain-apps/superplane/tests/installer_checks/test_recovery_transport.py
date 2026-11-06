import asyncio
import copy
import importlib.util
import json
import socket
import ssl
import threading

import httpx
import pytest
from fastapi import HTTPException

from installation.config import MODULE, Refusal, validate
from installation.manifests import bootstrap_job, migration_job, render
from installation.recovery_transport import generate, validate_material
from .test_paid_worker import native, lifecycle_config

__all__ = ["native"]


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def selected(environment):
    environment["recovery_transport"] = {
        "tls_secret": "adp/dev/superplane/recovery-tls",
        "credential_secret": "adp/dev/superplane/recovery-credential",
    }
    return environment


@pytest.fixture
def material(selected):
    return generate(selected)


def test_no_recovery_resources_by_default(environment, release):
    docs = render(environment, release)
    assert "recovery-tls" not in json.dumps(docs)
    assert "recovery-https" not in json.dumps(docs)


def test_optional_listener_has_no_public_route_or_credential_mount(selected, release):
    validate(selected, release)
    docs = render(selected, release)
    api = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    listener = api["spec"]["template"]["spec"]["containers"][-1]
    assert listener["name"] == "recovery-transport"
    assert "env" not in listener and "envFrom" not in listener
    assert [m["name"] for m in listener["volumeMounts"]] == ["recovery-tls"]
    service = next(
        d
        for d in docs
        if d["kind"] == "Service" and d["metadata"]["name"] == "superplane-recovery"
    )
    assert service["spec"]["type"] == "ClusterIP"
    assert not any(d["kind"] == "Ingress" for d in docs)
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["metadata"]["name"] == "superplane-recovery"
    )
    peers = policy["spec"]["ingress"][0]["from"]
    assert len(peers) == 1 and peers[0]["podSelector"]["matchLabels"] == {
        "app": "bedrockgateway"
    }


def test_material_is_bound_to_org_and_service(selected, material):
    tls, credential = material
    grant = validate_material(selected, tls, credential)
    assert grant["lease_scopes"] == ["controller_recovery/" + selected["org_id"]]
    assert grant["workspaces"] == []
    assert "tls.key" not in credential
    other = dict(selected, org_id="another-org")
    with pytest.raises(Refusal):
        validate_material(other, tls, credential)
    with pytest.raises(Refusal):
        validate_material(dict(selected, namespace="other-service"), tls, credential)


@pytest.mark.parametrize(
    "change", ["key", "ca", "credential", "scope", "workspace", "extra"]
)
def test_refuses_mismatched_material(selected, material, change):
    tls, credential = copy.deepcopy(material)
    if change in {"key", "ca"}:
        other, _ = generate(selected)
        tls[{"key": "tls.key", "ca": "ca.pem"}[change]] = other[
            {"key": "tls.key", "ca": "ca.pem"}[change]
        ]
    elif change == "credential":
        credential["authorization"] = "Bearer wrong"
    elif change in {"scope", "workspace"}:
        grant = json.loads(tls["submitter"])
        grant["lease_scopes" if change == "scope" else "workspaces"].append("*")
        tls["submitter"] = json.dumps(grant)
    else:
        credential["tls.key"] = tls["tls.key"]
    with pytest.raises(Refusal, match="invalid, expired or differs"):
        validate_material(selected, tls, credential)


@pytest.mark.parametrize("legacy", [False, True])
def test_gateway_tls_verifies_real_certificate_and_hostname(
    selected, tmp_path, monkeypatch, legacy
):
    from cryptography import x509

    if legacy:
        add = x509.CertificateBuilder.add_extension

        def legacy_extensions(self, extension, critical):
            if isinstance(
                extension, (x509.AuthorityKeyIdentifier, x509.SubjectKeyIdentifier)
            ):
                return self
            return add(self, extension, critical)

        monkeypatch.setattr(x509.CertificateBuilder, "add_extension", legacy_extensions)
    gateway = module_at(
        "observation_transport",
        MODULE.parents[1] / "gateway/src/internal/domain_observation_transport.py",
    )
    tls, credential = generate(selected)
    cert, key = tmp_path / "cert", tmp_path / "key"
    cert.write_text(tls["tls.crt"])
    key.write_text(tls["tls.key"])
    key.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)

    def serve():
        for _ in range(3 if legacy else 4):
            connection, _ = listener.accept()
            try:
                with server_context.wrap_socket(connection, server_side=True) as secure:
                    secure.recv(1)
            except ssl.SSLError:
                connection.close()
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    auth, context = gateway.transport(json.dumps(credential))
    assert auth == credential["authorization"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    host = f"superplane-recovery.{selected['namespace']}.svc.cluster.local"
    address = listener.getsockname()
    with socket.create_connection(address, timeout=5) as conn:
        with context.wrap_socket(conn, server_hostname=host) as secure:
            secure.send(b"x")
    if not legacy:
        strict = ssl.create_default_context(cadata=tls["ca.pem"])
        strict.verify_flags |= ssl.VERIFY_X509_STRICT
        with socket.create_connection(address, timeout=5) as conn:
            with strict.wrap_socket(conn, server_hostname=host) as secure:
                secure.send(b"x")
    for ctx, name in [(context, "wrong.example"), (ssl.create_default_context(), host)]:
        with socket.create_connection(address, timeout=5) as conn:
            with pytest.raises(ssl.SSLCertVerificationError):
                ctx.wrap_socket(conn, server_hostname=name)
    thread.join(6)
    assert not thread.is_alive()


def test_public_transport_keeps_system_tls_verification():
    gateway = module_at(
        "public_observation_transport",
        MODULE.parents[1] / "gateway/src/internal/domain_observation_transport.py",
    )
    _, context = gateway.transport("Bearer example")
    expected = ssl.create_default_context()
    assert context.verify_flags == expected.verify_flags
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "value",
    [
        '{"authorization":"secret","ca_pem":"bad"}',
        '{"authorization":"secret"}',
        "Bearer secret\nInjected: true",
    ],
)
def test_gateway_refuses_invalid_transport_without_echo(value):
    gateway = module_at(
        "observation_transport",
        MODULE.parents[1] / "gateway/src/internal/domain_observation_transport.py",
    )
    with pytest.raises(HTTPException) as error:
        gateway.transport(value)
    assert error.value.status_code == 503
    assert "secret" not in error.value.detail


def test_listener_restricts_surface_and_bounds_body(monkeypatch):
    proxy = module_at(
        "recovery_proxy", MODULE / "src/superplane-api/app/recovery_transport.py"
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="https://private"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            for path in (
                "/workspaces",
                "/internal/observations",
                "/docs",
                "/openapi.json",
                "/internal/controller/recovery/unknown",
            ):
                assert (await client.post(path)).status_code == 404
            assert (
                await client.post("/internal/controller/recovery/inventory")
            ).status_code == 401
            assert (
                await client.post(
                    "/internal/controller/recovery/inventory",
                    headers={"Authorization": "Bearer test"},
                    content=b"x" * (proxy.LIMIT + 1),
                )
            ).status_code == 413

    asyncio.run(exercise())


def test_preparation_reuses_pair_after_partial_creation(
    selected, material, monkeypatch
):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from installation import recovery_preparation as preparation
    from installation.config import identity

    stored = {"recovery_tls": material[0]}
    created = []

    def owned(installer, kind, factory):
        if kind not in stored:
            stored[kind] = factory()
            created.append(kind)
        return stored[kind], kind + "-version"

    monkeypatch.setattr(preparation, "owned_secret", owned)
    monkeypatch.setattr(
        preparation, "generate", lambda _: pytest.fail("must not regenerate stored TLS")
    )
    original = dict(selected["secrets"])
    installer = SimpleNamespace(
        env=selected,
        owner=identity(selected),
        receipt={},
        verify_deployment_identity=lambda: None,
        exclusive=nullcontext,
        save=lambda: None,
    )
    preparation.prepare(installer)
    preparation.prepare(installer)
    assert created == ["recovery_credential"]
    assert stored["recovery_credential"] == material[1]
    assert selected["secrets"] == original
    assert installer.receipt["status"] == "recovery-credentials-prepared"
    assert installer.receipt["recovery_transport"]["listener_ready"] is False
    assert material[1]["authorization"] not in json.dumps(installer.receipt)


def test_preparation_refuses_conflicting_credential(selected, material, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from installation import recovery_preparation as preparation

    original = dict(selected["secrets"])
    stored = {
        "recovery_tls": material[0],
        "recovery_credential": dict(material[1], authorization="Bearer changed"),
    }
    monkeypatch.setattr(
        preparation, "owned_secret", lambda _, kind, factory: (stored[kind], "version")
    )
    installer = SimpleNamespace(
        env=selected,
        receipt={},
        verify_deployment_identity=lambda: None,
        exclusive=nullcontext,
        save=lambda: None,
    )
    with pytest.raises(Refusal):
        preparation.prepare(installer)
    assert selected["secrets"] == original
    assert installer.receipt == {}


def test_bootstrap_listener_and_native_egress_match(native):
    from installation import native_egress
    from installation.config import LABEL, identity

    env, lock = native
    lifecycle_config(env)
    env["api_adapters"]["dispatcher"]["operation_database_secret_ref"] = {
        "name": "superplane-operation-api-db",
        "key": "dsn",
    }
    env["recovery_transport"] = {
        "tls_secret": "adp/dev/superplane/recovery-tls",
        "credential_secret": "adp/dev/superplane/recovery-credential",
    }
    env["cluster_dns_ip"] = "172.20.0.10"
    env["paid_worker"]["egress"] = {
        "mode": "public-https",
        "database": {"cidr": "10.0.1.2/32", "port": 5432},
    }
    docs = render(env, lock)
    ingress = next(
        d["spec"]["ingress"]
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["metadata"]["name"] == "superplane-recovery"
    )
    assert ingress[0]["from"][1]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "superplane-paid-worker",
        LABEL: identity(env),
    }
    rules = native_egress.rules(env)
    selected = [r for r in rules if r["ports"] == [{"protocol": "TCP", "port": 8443}]]
    assert len(selected) == 1
    assert selected[0]["to"][0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "superplane-api",
        LABEL: identity(env),
    }
    assert {"ipBlock": {"cidr": "172.20.0.10/32"}} in rules[-1]["to"]


@pytest.mark.parametrize("status", [200, 403, 302])
def test_scoped_bootstrap_forwarding_preserves_auth_and_refuses_redirect(
    monkeypatch, status
):
    proxy = module_at(
        "recovery_proxy", MODULE / "src/superplane-api/app/recovery_transport.py"
    )
    real_client = httpx.AsyncClient
    path = (
        "/api/v1/workspaces/00000000-0000-4000-8000-000000000001/bootstrap-observation"
    )
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"detail": "scoped result"},
            headers={"Location": "https://wrong.invalid", "Set-Cookie": "forbidden=1"},
        )

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(backend), **kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)

    async def exercise():
        async with real_client(
            transport=httpx.ASGITransport(app=proxy.app), base_url="https://private"
        ) as client:
            denied = await client.get(path)
            assert denied.status_code == 401 and not calls
            response = await client.get(
                path,
                headers={
                    "Authorization": "sp-bootstrap-read-test",
                    "Cookie": "must-not-forward",
                },
            )
            assert response.status_code == (503 if status == 302 else status)
            assert (
                "location" not in response.headers
                and "set-cookie" not in response.headers
            )

    asyncio.run(exercise())
    assert len(calls) == 1 and calls[0].method == "GET"
    assert str(calls[0].url) == "http://127.0.0.1:8000" + path
    assert calls[0].headers["Authorization"] == "sp-bootstrap-read-test"
    assert "cookie" not in calls[0].headers


@pytest.mark.parametrize("builder", [migration_job, bootstrap_job])
def test_installation_jobs_exclude_long_running_listener(selected, release, builder):
    job = builder(selected, release, "finite-job")
    pod = job["spec"]["template"]
    assert len(pod["spec"]["containers"]) == 1
    assert "recovery-tls" not in {v["name"] for v in pod["spec"]["volumes"]}
    assert "eks.amazonaws.com/skip-containers" not in pod["metadata"]["annotations"]
    assert pod["spec"]["restartPolicy"] == "Never"
    container = pod["spec"]["containers"][0]
    assert container["readinessProbe"] == {
        "exec": {"command": ["python", "-S", "-c", "raise SystemExit(1)"]}
    }
    assert "livenessProbe" not in container and "startupProbe" not in container
    assert job["spec"]["backoffLimit"] == 0
    api = next(
        d
        for d in render(selected, release)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    assert (
        api["spec"]["template"]["spec"]["containers"][-1]["name"]
        == "recovery-transport"
    )
    assert pod["metadata"]["labels"] == api["spec"]["template"]["metadata"]["labels"]
