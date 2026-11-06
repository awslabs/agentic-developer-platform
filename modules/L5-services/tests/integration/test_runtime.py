"""Installed workers cannot silently fall back to developer or foreign targets."""

import asyncio
import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from l5_services import runtime, runtime_support
from l5_services.workers.execution import AuthorityRevoked, load_service


def test_installed_database_session_uses_packaged_rds_trust(monkeypatch):
    for name, value in {
        "L5_RDS_HOST": "database.example.invalid", "L5_RDS_DATABASE": "app",
        "L5_RDS_USER": "worker", "L5_RDS_PORT": "5432", "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    original_context = ssl.create_default_context
    contexts = []

    def checked_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        contexts.append((kwargs, context))
        return context

    monkeypatch.setattr(runtime_support.ssl, "create_default_context", checked_context)
    monkeypatch.setattr(runtime_support, "create_async_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime_support, "async_sessionmaker", lambda *args, **kwargs: "packaged-session")
    assert runtime_support.sessions() == "packaged-session"
    assert len(contexts) == 1
    arguments, context = contexts[0]
    assert "cadata" in arguments and "BEGIN CERTIFICATE" in arguments["cadata"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert any("Amazon RDS us-east-1 Root CA RSA2048 G1" in str(cert["subject"])
               for cert in context.get_ca_certs())


@pytest.mark.parametrize("gateway", [
    "http://testapi.execute-api.us-east-1.amazonaws.com/dev",
    "https://other.example.invalid/dev",
    "https://testapi.execute-api.us-east-1.amazonaws.com/dev/internal",
    "https://testapi.execute-api.us-east-1.amazonaws.com:443/dev",
])
def test_gateway_transport_requires_selected_stage(monkeypatch, gateway):
    monkeypatch.setenv("L5_GATEWAY_URL", gateway)
    with pytest.raises(ValueError, match="API Gateway stage"):
        runtime._gateway_url()


def test_installed_factory_does_not_use_developer_credentials(monkeypatch):
    monkeypatch.setenv("L5_WORKER_FACTORY", "l5_services.runtime:factory")
    monkeypatch.setenv("L5_GATEWAY_URL", "https://testapi.execute-api.us-east-1.amazonaws.com/dev")
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    with pytest.raises(RuntimeError, match="installed Lambda workload"):
        load_service()


@pytest.mark.parametrize("resource", ["_gateway_transport", "_sessions", "_archive_store", "_queue_publisher"])
def test_security_factory_rejects_unavailable_dependencies(monkeypatch, resource):
    resources = {"_gateway_transport": AsyncMock(), "_sessions": lambda: object(),
                 "_archive_store": AsyncMock(), "_queue_publisher": AsyncMock()}
    resources[resource] = None
    for name, value in resources.items():
        monkeypatch.setattr(runtime, name, lambda value=value: value)
    with pytest.raises(RuntimeError, match="dependencies unavailable"):
        runtime.factory()


def test_security_factory_rejects_foreign_identity_and_allow_overrides(monkeypatch):
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    with pytest.raises(AuthorityRevoked, match="does not own"):
        runtime.factory()
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    with pytest.raises(TypeError):
        runtime.factory(allow=True)


@pytest.mark.parametrize("service_id", ["security", "devops", "finops", "transform", "well_architected"])
def test_shared_resources_require_installed_service_identity(monkeypatch, service_id):
    monkeypatch.setenv("L5_SERVICE_ID", service_id)
    monkeypatch.delenv("L5_WORK_QUEUE_URL", raising=False)
    with pytest.raises(ValueError, match="L5_WORK_QUEUE_URL"):
        runtime_support.queue_publisher(service_id)
    foreign = "devops" if service_id == "security" else "security"
    with pytest.raises(ValueError, match="identity does not match"):
        runtime_support.queue_publisher(foreign)


async def test_shared_archive_rejects_foreign_service_reference():
    archive = runtime_support.ArchiveStore("bucket", object(), service_id="devops")
    with pytest.raises(ValueError, match="another run"):
        await archive.get_archive("12345678-1234-1234-1234-123456789012", {
            "artifact_id": "l5/security/work/12345678-1234-1234-1234-123456789012/hash.zip",
            "sha256": "hash", "version": "one",
        })


async def test_source_less_finops_session_rechecks_broker_before_effect(monkeypatch):
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1"}
    admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                           "project_id": "project-one", "configuration_id": "config-one"},
                 "connection_ref": {"tenant_id": "tenant-one", "project_id": "project-one",
                                    "source_connection_id": "selected-one", "record_type": "connection",
                                    "record_id": "selected-one", "record_revision": "revision-one"},
                 "delegation_id": "grant-one", "delegation_version": 2, "provider_identity": identity}
    run = SimpleNamespace(service_id="finops", tenant_id="tenant-one", product_id="product-one",
                          project_id="project-one", run_id="run-one", input={"capability": "cost_inquiry"},
                          admission=admission)
    claim = SimpleNamespace(work_id="attempt-one", owner="worker-one", fence=3)
    issued = {"operation_id": run.run_id, "attempt_id": claim.work_id, "delegation_revision": 2,
              "provider_identity": identity,
              "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    gateway = AsyncMock()
    gateway.post.return_value = SimpleNamespace(json=lambda: issued, raise_for_status=lambda: None)

    class Provider:
        effects = 0

        def client(self, service, region_name):
            assert (service, region_name) == ("ce", "us-east-1")
            return self

        def get_cost_and_usage(self):
            self.effects += 1
            return {"status": "observed"}

    provider = Provider()
    scoped = await runtime_support.run_provider_session(
        run, claim, gateway, AsyncMock(), lambda *_: provider,
        service_id="finops", allowed_clients={"ce"})
    with pytest.raises(AuthorityRevoked, match="outside service scope"):
        scoped.client("securityagent")
    assert await asyncio.to_thread(scoped.client("ce").get_cost_and_usage) == {"status": "observed"}
    assert gateway.post.await_count == 2 and provider.effects == 1
    assert gateway.post.await_args.kwargs["json"]["lease_fence"] == 3
    gateway.post.side_effect = RuntimeError("revoked or stale lease")
    with pytest.raises(AuthorityRevoked, match="current Product work unavailable"):
        await asyncio.to_thread(scoped.client("ce").get_cost_and_usage)
    assert provider.effects == 1
    run.service_id = "security"
    with pytest.raises(AuthorityRevoked, match="does not own"):
        await runtime_support.run_provider_session(run, claim, gateway, AsyncMock(), lambda *_: provider,
                                                   service_id="finops", allowed_clients={"ce"})


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,recovering", (("investigation", False), ("release_verification", False), ("investigation", True)))
async def test_devops_source_free_session_rechecks_scoped_broker_before_native_read(monkeypatch, capability, recovering):
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1"}
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"}
    admission = {"scope": scope, "source_snapshots": [], "delegation_id": "grant-one", "delegation_version": 2,
                 "connection_ref": {"source_connection_id": "connection-one", "record_id": "connection-one"},
                 "native_space_ref": {"tenant_id": "tenant-one", "project_id": "project-one",
                                      "source_connection_id": "connection-one", "record_type": "agent_space",
                                      "record_id": "fictional-space"},
                 "provider_identity": identity}
    run = SimpleNamespace(service_id="devops", **scope, run_id="run-one",
                          input={"capability": capability, "sources": []}, admission=admission)
    claim = SimpleNamespace(work_id="work-one", owner="leased-worker", fence=3)
    if recovering:
        from l5_services.devops.recovery import request_recovery

        run.state, run.origin = "expired", "adp_submitted"
        run.deadline_at = datetime.now(UTC) - timedelta(hours=1)
        run.native_job_ref, run.monitoring = {"record_id": "existing-task"}, None
        run.monitoring = request_recovery(run, datetime.now(UTC))
        run.monitoring["devops_recovery"]["authorization"] = {"delegation_id": "read-only-grant", "delegation_version": 2}
        claim.kind = "poll"
    issued = {"operation_id": "run-one", "attempt_id": "work-one", "delegation_revision": 2,
              "provider_identity": identity, "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    gateway = AsyncMock()
    gateway.post.return_value = SimpleNamespace(json=lambda: issued, raise_for_status=lambda: None)

    class Provider:
        reads = 0

        def client(self, service, region_name):
            assert (service, region_name) == ("devops-agent", "us-east-1")
            return self

        def get_agent_space(self):
            self.reads += 1
            return {"agentSpaceId": "fictional-space"}

    provider = Provider()
    scoped = await runtime_support.run_provider_session(
        run, claim, gateway, AsyncMock(), lambda *_: provider,
        service_id="devops", allowed_clients={"devops-agent"})
    assert await asyncio.to_thread(scoped.client("devops-agent").get_agent_space) == {"agentSpaceId": "fictional-space"}
    assert gateway.post.await_count == 2 and provider.reads == 1
    assert gateway.post.await_args.kwargs["json"]["lease_fence"] == 3
    assert gateway.post.await_args.kwargs["json"]["delegation_id"] == ("read-only-grant" if recovering else "grant-one")
    assert run.admission["delegation_id"] == "grant-one"
    gateway.post.side_effect = RuntimeError("revoked grant or stale lease")
    with pytest.raises(AuthorityRevoked, match="current Product work unavailable"):
        await asyncio.to_thread(scoped.client("devops-agent").get_agent_space)
    assert provider.reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id,capability", (
    ("devops", "investigation"),
    ("devops", "release_readiness"), ("devops", "unqualified"),
    ("security", "threat_model"), ("transform", "transformation"),
    ("well_architected", "architecture_review"),
))
async def test_unqualified_source_free_run_fails_before_broker(monkeypatch, service_id, capability):
    monkeypatch.setenv("L5_SERVICE_ID", service_id)
    run = SimpleNamespace(service_id=service_id, input={"capability": capability, "sources": []},
                          admission={"source_snapshots": []})
    gateway = AsyncMock()
    provider = AsyncMock()
    with pytest.raises(AuthorityRevoked, match="no pinned Product sources"):
        await runtime_support.run_provider_session(
            run, SimpleNamespace(), gateway, AsyncMock(), provider,
            service_id=service_id, allowed_clients={"devops-agent"})
    gateway.post.assert_not_awaited()
    provider.assert_not_called()


async def test_run_session_rejects_wrong_identity_and_expiry(monkeypatch):
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1"}
    run = SimpleNamespace(service_id="finops", tenant_id="tenant-one", product_id="product-one",
                          project_id="project-one", run_id="run-one", input={}, admission={
                              "scope": {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"},
                              "connection_ref": {"record_type": "connection"}, "delegation_id": "grant-one",
                              "delegation_version": 1, "provider_identity": identity})
    claim = SimpleNamespace(work_id="attempt-one", owner="worker-one", fence=1)
    issued = {"operation_id": run.run_id, "attempt_id": claim.work_id, "delegation_revision": 1,
              "provider_identity": {**identity, "account_id": "000000000000"},
              "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    gateway = AsyncMock()
    gateway.post.return_value = SimpleNamespace(json=lambda: issued, raise_for_status=lambda: None)
    provider = AsyncMock()
    with pytest.raises(AuthorityRevoked, match="another work or provider identity"):
        await runtime_support.run_provider_session(run, claim, gateway, AsyncMock(), provider,
                                                   service_id="finops", allowed_clients={"ce"})
    provider.assert_not_called()
    issued["provider_identity"] = identity
    issued["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(AuthorityRevoked, match="session expired"):
        await runtime_support.run_provider_session(run, claim, gateway, AsyncMock(), provider,
                                                   service_id="finops", allowed_clients={"ce"})
    provider.assert_not_called()


async def test_security_factory_rejects_other_capabilities_without_broker_calls(monkeypatch):
    gateway = AsyncMock()
    monkeypatch.setattr(runtime, "_gateway_transport", lambda: gateway)
    monkeypatch.setattr(runtime, "_sessions", lambda: lambda: object())
    monkeypatch.setattr(runtime, "_archive_store", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_queue_publisher", lambda: AsyncMock())
    worker = runtime.factory()
    with pytest.raises(AuthorityRevoked, match="does not own"):
        await worker.authorize(SimpleNamespace(service_id="devops", input={"capability": "investigation"}),
                               SimpleNamespace(), "submit")
    gateway.post.assert_not_called()


async def test_installed_setup_rechecks_current_work_before_native_effect(monkeypatch):
    connection = {"tenant_id": "tenant-one", "project_id": "project-one",
                  "source_connection_id": "selected-one", "record_type": "connection",
                  "record_id": "selected-one", "record_revision": "v1"}
    authorization = {"delegation_id": "parent-one", "delegation_revision": 1,
                     "actor_id": "owner-one", "authorized_at": "2026-10-28T00:00:00Z"}
    intent = SimpleNamespace(service_id="security", tenant_id="tenant-one", operation_id="operation-one",
                             configuration_id="configuration-one", project_id="project-one",
                             request={"action": "create", "configuration_revision": 1},
                             scope={"product_id": "product-one"}, authorization=authorization)
    claim = SimpleNamespace(operation_id="operation-one", work_id="work-one", owner="worker-one", fence=2)
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1", "verified_at": "2026-10-28T00:00:00Z"}
    verified = {"configuration_id": intent.configuration_id, "configuration_revision": 1,
                "scope": intent.scope, "connection_ref": connection, "authorization": authorization,
                "account_id": identity["account_id"], "role_arn": identity["role_arn"], "region": identity["region"]}
    issued = {"operation_id": intent.operation_id, "attempt_id": claim.work_id, "delegation_revision": 1,
              "setup_verified": verified, "provider_identity": identity,
              "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
    gateway = AsyncMock()
    response = SimpleNamespace(status_code=200, json=lambda: issued, raise_for_status=lambda: None)
    gateway.post.return_value = response

    class Sessions:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, model, key):
            return SimpleNamespace(settings={"connection_ref": connection})

    class Provider:
        effects = 0

        def client(self, service, region_name):
            assert service == "securityagent" and region_name == "us-east-1"
            return self

        def list_agent_spaces(self):
            self.effects += 1
            return {"agentSpaceSummaries": []}

    native = Provider()
    monkeypatch.setattr(runtime, "_gateway_transport", lambda: gateway)
    monkeypatch.setattr(runtime, "_sessions", lambda: lambda: Sessions())
    monkeypatch.setattr(runtime, "_archive_store", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_queue_publisher", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_provider_session", lambda *_: native)
    worker = runtime.factory()
    _, adapter = await worker.binding_authorize(intent, claim)
    assert await adapter._read(adapter.client.list_agent_spaces) == {"agentSpaceSummaries": []}
    assert native.effects == 1 and gateway.post.await_count == 2
    assert gateway.post.await_args.kwargs["json"]["lease_fence"] == claim.fence
    gateway.post.return_value = SimpleNamespace(status_code=409)
    with pytest.raises(AuthorityRevoked, match="current setup work unavailable"):
        await adapter._read(adapter.client.list_agent_spaces)
    assert native.effects == 1


def test_lambda_rejects_factory_for_another_service(monkeypatch):
    monkeypatch.setenv("L5_WORKER_FACTORY", "l5_services.runtime:factory")
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    monkeypatch.setattr(runtime, "factory", lambda: SimpleNamespace(service_id="security"))
    with pytest.raises(ValueError, match="does not own"):
        load_service()


def test_installed_probe_does_not_dispatch_or_scan_work():
    from l5_services.workers.execution import lambda_handler as execute
    from l5_services.workers.recovery import lambda_handler as recover

    service = SimpleNamespace(service_id="security", execute=AsyncMock())
    assert execute({"l5_runtime_probe": 1}, None, service=service) == {"service_id": "security"}
    assert recover({"l5_runtime_probe": 1}, None, service=service) == {"service_id": "security"}
    service.execute.assert_not_awaited()


def test_security_factory_mounts_only_qualified_context_contract(monkeypatch):
    import json
    from l5_services.publication.installed import InstalledPublicationClient

    transport, archive, publisher = AsyncMock(), AsyncMock(), AsyncMock()
    sessions = lambda: object()
    for name, value in {"_gateway_transport": transport, "_sessions": sessions,
                        "_archive_store": archive, "_queue_publisher": publisher}.items():
        monkeypatch.setattr(runtime, name, lambda value=value: value)
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    monkeypatch.delenv("L5_CONSUMER_CONTRACTS", raising=False)
    assert runtime.factory().context_client is None
    contract = {"kind": "security_result", "schema_version": "1.0", "private_read_fencing": True,
                "qualification_ref": "qualification-example", "path": "/internal/v1/context/l5-contributions"}
    monkeypatch.setenv("L5_CONSUMER_CONTRACTS", json.dumps({"security": {"product-context": contract}}))
    mounted = runtime.factory().context_client
    assert isinstance(mounted, InstalledPublicationClient)
    assert mounted.service_kind == "security" and mounted.destination == "product-context"
    assert mounted.sessions is sessions and mounted.transport is transport
    contract["private_read_fencing"] = False
    monkeypatch.setenv("L5_CONSUMER_CONTRACTS", json.dumps({"security": {"product-context": contract}}))
    with pytest.raises(ValueError, match="qualified exact peer contract"):
        runtime.factory()


async def test_security_recovery_rechecks_broker_without_rereading_expired_source(monkeypatch):
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1"}
    admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                           "project_id": "project-one", "configuration_id": "config-one"},
                 "connection_ref": {"tenant_id": "tenant-one", "project_id": "project-one",
                                    "source_connection_id": "selected-one", "record_type": "connection",
                                    "record_id": "selected-one", "record_revision": "revision-one"},
                 "delegation_id": "grant-one", "delegation_version": 2, "provider_identity": identity}
    run = SimpleNamespace(service_id="security", tenant_id="tenant-one", product_id="product-one",
                          project_id="project-one", run_id="run-one", input={"capability": "code_review"},
                          admission=admission, state="expired", native_job_ref={"record_id": "original-job"},
                          monitoring=None, deadline_at=datetime.now(UTC) - timedelta(hours=2))
    from l5_services.security_recovery import request_recovery
    run.monitoring = request_recovery(run, datetime.now(UTC))
    run.input["sources"] = [{"record_id": "original-source"}]
    claim = SimpleNamespace(work_id="attempt-one", owner="worker-one", fence=3, kind="poll")
    issued = {"operation_id": run.run_id, "attempt_id": claim.work_id, "delegation_revision": 2,
              "provider_identity": identity,
              "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    gateway = AsyncMock()
    gateway.post.return_value = SimpleNamespace(json=lambda: issued, raise_for_status=lambda: None)

    class Provider:
        effects = 0

        def client(self, service, region_name):
            assert (service, region_name) == ("securityagent", "us-east-1")
            return self

        def batch_get_code_review_jobs(self):
            self.effects += 1
            return {"status": "observed"}

    provider = Provider()
    sources = AsyncMock()
    scoped = await runtime_support.run_provider_session(
        run, claim, gateway, sources, lambda *_: provider,
        service_id="security", allowed_clients={"securityagent"})
    with pytest.raises(AuthorityRevoked, match="outside service scope"):
        scoped.client("s3")
    assert await asyncio.to_thread(scoped.client("securityagent").batch_get_code_review_jobs) == {"status": "observed"}
    assert gateway.post.await_count == 2 and provider.effects == 1
    assert gateway.post.await_args.kwargs["json"]["lease_fence"] == 3
    gateway.post.side_effect = RuntimeError("revoked or stale lease")
    with pytest.raises(AuthorityRevoked, match="current Product work unavailable"):
        await asyncio.to_thread(scoped.client("securityagent").batch_get_code_review_jobs)
    assert provider.effects == 1
    sources.read_source.assert_not_awaited()
