"""Original Security setup over real authentication and tenant-local SQL.

Only external identity/provider transports are test doubles. No browser is
available, and these offline checks never assert native release qualification.
"""

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from src.auth import dependencies, native_launch_revocation
from src.auth.aws_connection_authority import connection_binding
from src.auth.cognito_jwt import CognitoJWTValidator
from src.products.models import ProductSourceBinding
from src.shared import database
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, UserCredential
from src.shared.schemas.auth import TokenContext
from tests.internal.test_l5_security_tools import compiled_dispatch_catalogue  # noqa: F401
from tests.orchestration.security_original_release_fixture import build_original_security_release, store  # noqa: F401
from tests.products.membership import add_team_member
from tests.products.security_original_transport import cli_profile, isolated_child_transport, loopback_gateway, private_json
from tests.products.test_i05 import REQUEST


@pytest.fixture
async def test_engine(tmp_path, sqlite_schemas):
    """Human polling and HTTP effects must never share one physical transaction."""
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "security.sqlite"), connect_args={"check_same_thread": False})

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection, _):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

    await sqlite_schemas.create_all(engine, Base.metadata)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def original_setup(test_client, app, test_engine, db_session_factory, monkeypatch):
    """Seed ordinary memberships; every HTTP actor must validate an RS256 JWT."""
    monkeypatch.setenv("BG_COGNITO_USER_POOL_ID", "us-east-1_offline")
    monkeypatch.setenv("BG_COGNITO_CLIENT_ID", "offline-client")
    monkeypatch.setattr(database, "_session_factory", db_session_factory)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    validator = CognitoJWTValidator(user_pool_id="us-east-1_offline", client_id="offline-client", region="us-east-1")
    jwks = {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "offline-key", "use": "sig"}]}
    # Replace the external JWKS fetch, retaining key selection and verification.
    monkeypatch.setattr(validator.jwk_client, "fetch_data", lambda: jwks)
    monkeypatch.setattr(dependencies, "_cognito_validator", validator)
    redis_values = {}

    async def put(key, value, **_):
        redis_values[key] = value
        return True

    monkeypatch.setattr(native_launch_revocation, "_client", lambda: SimpleNamespace(get=AsyncMock(side_effect=redis_values.get), set=put))
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    cases = []
    for number in (1, 2):
        owner = str(uuid4())
        tenant = f"original-security-tenant-{number}"
        team = f"original-security-team-{number}"
        actor = TokenContext(
            user_id=owner,
            org_id=tenant,
            team_id=team,
            department_id="fixture-department",
            account_type="human",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        async with db_session_factory() as db:
            db.add(Organization(id=tenant, name=f"Offline Security {number}"))
            db.add(
                User(
                    id=owner,
                    cognito_sub=owner,
                    cognito_username=owner,
                    org_id=tenant,
                    team_id=team,
                    email=f"owner{number}@example.test",
                    user_kind="human",
                )
            )
            await add_team_member(db, actor)
            await db.commit()
        now = datetime.now(UTC)
        claims = {
            "sub": owner,
            "username": owner,
            "iss": validator.issuer,
            "client_id": "offline-client",
            "token_use": "access",
            "exp": now + timedelta(hours=1),
            "iat": now,
            "custom:org_id": tenant,
            "custom:team_id": team,
            "custom:department_id": "fixture-department",
            "custom:account_type": "human",
        }
        tokens = {
            slot: jwt.encode({**claims, "jti": str(uuid4())}, key, algorithm="RS256", headers={"kid": "offline-key"})
            for slot in ("api", "cli", "mcp", "legacy_ui", "human")
        }
        headers = {"Authorization": "Bearer " + tokens["human"]}
        created = await test_client.post(
            "/v1/products",
            json={**REQUEST, "name": f"Original Security {number}"},
            headers={**headers, "Idempotency-Key": f"original_security_product_{number}"},
        )
        assert created.status_code == 201, created.text
        product, project = created.json()["product_id"], created.json()["project_id"]
        credential = UserCredential(
            id=str(uuid4()),
            org_id=tenant,
            user_id=owner,
            service="aws",
            credential_type="aws_role",
            label="Offline selected provider",
            secret_arn=f"arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-{number}",
            strict=True,
            agent_consent=True,
            aws_external_id="offline-external-id",
            aws_verification_attempt="offline-attempt",
            aws_verified_at=now,
            aws_verified_version_id="credential-version-1",
            scopes={
                "status": "verified",
                "source": "imported_role",
                "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/OfflineSecurity",
            },
        )
        credential.aws_verified_binding = connection_binding(credential)
        mapping = ChannelTenantMap(
            id=str(uuid4()),
            org_id=tenant,
            provider="github",
            provider_scope_id=f"offline-org-{number}",
            installation_id=str(100 + number),
            ownership_disputed=False,
        )
        async with db_session_factory() as db:
            db.add_all([credential, mapping])
            await db.flush()
            db.add_all(
                [
                    ProductSourceBinding(
                        tenant_id=tenant,
                        project_id=project,
                        kind="repository",
                        provider="github",
                        installation_id=mapping.id,
                        namespace=f"offline-org-{number}",
                        native_id=str(200 + number),
                        desired={"source_revision": "a" * 40},
                        observed={"source_revision": "a" * 40},
                        status="healthy",
                    ),
                    ProductSourceBinding(
                        tenant_id=tenant,
                        project_id=project,
                        kind="environment",
                        provider="aws",
                        credential_id=credential.id,
                        namespace="123456789012",
                        native_id="us-east-1",
                        desired={},
                        observed={"source_revision": "credential-version-1"},
                        status="healthy",
                    ),
                ]
            )
            await db.commit()
        source = {
            "tenant_id": tenant,
            "project_id": project,
            "source_connection_id": mapping.id,
            "record_type": "repository_commit",
            "record_id": str(200 + number),
            "record_revision": "a" * 40,
        }
        connection_ref = {
            "tenant_id": tenant,
            "project_id": project,
            "source_connection_id": credential.id,
            "record_type": "connection",
            "record_id": credential.id,
            "record_revision": "credential-version-1",
        }
        profile_path = f"/v1/products/{product}/projects/{project}"
        patched = await test_client.patch(
            profile_path + "/configuration",
            json={
                "configuration": {
                    "basic": {"repositories": [source]},
                    "governance": {"data_classification": "internal"},
                    "selected_capabilities": [{"capability": "code_review", "ownership_mode": "managed"}],
                }
            },
            headers={**headers, "Idempotency-Key": f"original_security_profile_{number}", "If-Profile-Revision": "1"},
        )
        assert patched.status_code == 200, patched.text
        path = profile_path + "/services/security"
        binding_id = str(uuid4())
        choices = {
            "enabled": False,
            "capabilities": ["code_review"],
            "connection_ref": connection_ref,
            "region": "us-east-1",
            "source_refs": [source],
            "overrides": [
                {"field": field, "value": value, "reason": "Owner-selected offline setup"}
                for field, value in (("connection_ref", connection_ref), ("region", "us-east-1"), ("source_refs", [source]))
            ],
            "limits": {
                "deadline_seconds": 300,
                "max_provider_attempts": 3,
                "max_polls": 30,
                "concurrency": 1,
                "max_cost": "1.00",
                "currency": "USD",
                "policy_ref": {**connection_ref, "record_type": "policy", "record_id": "example-budget", "record_revision": "budget-policy-v1"},
            },
            "triggers": [],
        }
        saved = await test_client.put(
            path + "/configuration",
            json=choices,
            headers={**headers, "Idempotency-Key": f"original_security_configuration_{number}", "If-None-Match": "*"},
        )
        assert saved.status_code == 201, saved.text
        identity = {"account_id": "123456789012", "role_arn": credential.scopes["role_arn"], "region": "us-east-1"}
        async with db_session_factory() as db:
            db.add(
                Binding(
                    tenant_id=tenant,
                    product_id=product,
                    project_id=project,
                    service_id="security",
                    binding_id=binding_id,
                    configuration_id=saved.json()["configuration_id"],
                    scope=saved.json()["scope"],
                    revision=1,
                    generation=1,
                    state="ready",
                    ownership="created",
                    connection_ref=connection_ref,
                    connection_key=hashlib.sha256(json.dumps(connection_ref).encode()).hexdigest(),
                    native_resource_ref={**connection_ref, "record_type": "security_space", "record_id": f"offline-space-{number}"},
                    native_key=f"offline-space-{number}",
                    provider_identity=identity,
                    gaps=[],
                    readiness=[
                        {
                            "kind": "code_review_source_registration",
                            "observed_at": now.isoformat(),
                            "registration": {"service_role": "arn:aws:iam::123456789012:role/OfflineReader", "bucket": "offline-source-bucket"},
                        }
                    ],
                )
            )
            await db.commit()
        choices.update(
            enabled=True,
            triggers=[
                {
                    "trigger_id": "manual",
                    "kind": "manual",
                    "enabled": True,
                    "capability": "code_review",
                    "binding_id": binding_id,
                    "source_refs": [source],
                }
            ],
        )
        activated = await test_client.put(
            path + "/configuration",
            json=choices,
            headers={**headers, "Idempotency-Key": f"original_security_enable_{number}", "If-Match": saved.headers["etag"]},
        )
        assert activated.status_code == 200, activated.text
        cases.append(
            SimpleNamespace(
                number=number,
                actor=actor,
                tokens=tokens,
                headers=headers,
                product=product,
                project=project,
                credential=credential,
                mapping=mapping,
                path=path,
                source=source,
                connection=connection_ref,
                configuration=activated.json(),
                binding_id=binding_id,
                choices=choices,
                identity=identity,
            )
        )
    return SimpleNamespace(cases=cases, client=test_client, app=app, factory=db_session_factory, key=key, redis=redis_values)


async def test_original_setup_real_authentication(original_setup):
    c = original_setup
    for case in c.cases:
        response = await c.client.get(case.path + "/configuration", headers=case.headers)
        assert response.status_code == 200, response.text
        foreign = next(other for other in c.cases if other is not case)
        response = await c.client.get(case.path + "/configuration", headers=foreign.headers)
        assert response.status_code == 404, response.text
        response = await c.client.get(case.path + "/configuration", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401, response.text


async def native_consent(c, case):
    """The real owner reviews and replaces the exact revision-bound grant."""
    listed = await c.client.get(case.path + "/delegations", headers=case.headers)
    assert listed.status_code == 200, listed.text
    for grant in listed.json()["items"]:
        if grant["active"]:
            revoked = await c.client.delete(
                case.path + "/delegations/" + grant["delegation_id"], headers={**case.headers, "If-Match": '"' + str(grant["revision"]) + '"'}
            )
            assert revoked.status_code == 200, revoked.text
    preview = await c.client.get(case.path + "/delegation-scope", headers=case.headers)
    assert preview.status_code == 200, preview.text
    response = await c.client.post(
        case.path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers={**case.headers, "If-Match": preview.headers["etag"], "Idempotency-Key": "offline_consent_" + uuid4().hex},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def external_security_providers(monkeypatch, db_session_factory):
    """STS, source archive storage and SQS only; keep their domain callers real."""
    from src import l5_queue_registry
    from src.products import l5_admission_capture, security_provider_authority
    from src.products.source_snapshots import ArtifactRef, CapturedSource

    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    role = "arn:aws:iam::123456789012:role/OfflineSecurity"
    helper = SimpleNamespace(
        current_version_id=lambda _: "credential-version-1",
        get_secret_at_version=lambda *_: (
            json.dumps({"role_arn": role, "external_id": "offline-external-id", "account_id": "123456789012", "default_region": "us-east-1"}),
            "credential-version-1",
        ),
    )
    monkeypatch.setattr(security_provider_authority, "SecretsManagerHelper", lambda: helper)
    assumed = SimpleNamespace(
        access_key_id="offline-key",
        secret_access_key="offline-secret",
        session_token="offline-session",
        assumed_role_arn="arn:aws:sts::123456789012:assumed-role/OfflineSecurity/offline",
    )
    monkeypatch.setattr(security_provider_authority, "assume_role", lambda **_: assumed)
    monkeypatch.setattr(
        security_provider_authority,
        "boto3",
        SimpleNamespace(
            Session=lambda **_: SimpleNamespace(
                client=lambda _: SimpleNamespace(get_caller_identity=lambda: {"Account": "123456789012", "Arn": assumed.assumed_role_arn})
            )
        ),
    )
    captured = []

    async def capture(binding, mapping, request, objects):
        assert binding.installation_id == mapping.id and mapping.org_id == request.source_ref.tenant_id
        captured.append(request)
        ref = ArtifactRef(
            artifact_id="products/source-snapshots/" + uuid4().hex, version="offline-version", sha256="a" * 64, media_type="application/json", bytes=5
        )
        return CapturedSource(
            source_ref=request.source_ref,
            content=ref,
            file_manifest=ref,
            file_count=1,
            exclusions=request.exclusions,
            expires_at=request.retain_until,
        )

    monkeypatch.setattr(l5_admission_capture, "capture_source", capture)
    monkeypatch.setattr(l5_admission_capture, "get_snapshot_objects", object)
    sent = []
    sent_work = []

    async def send(envelope, delay):
        from l5_services.models.work import Work

        async with db_session_factory() as db:
            row = await db.scalar(select(Work).where(Work.work_id == envelope["work_id"]))
            sent_work.append((row.kind, row.run_id) if row else None)
        sent.append((envelope, delay))

    monkeypatch.setenv("L5_WORK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/offline-security")
    monkeypatch.setattr(l5_queue_registry, "sqs_publisher", lambda _: send)
    return SimpleNamespace(captured=captured, sent=sent, sent_work=sent_work)


async def test_real_owner_consent_and_provider_admission(original_setup, external_security_providers):
    c = original_setup
    for case in c.cases:
        api = {"Authorization": "Bearer " + case.tokens["api"]}
        response = await c.client.get(case.path + "/release-evidence/provider/" + case.binding_id, headers=api)
        assert response.status_code == 403 and response.json()["detail"]["code"] == "delegation_denied", response.text
        await native_consent(c, case)
        response = await c.client.get(case.path + "/release-evidence/provider/" + case.binding_id, headers=api)
        assert response.status_code == 200, response.text
        assert response.json()["provider_identity"] == case.identity


# Real selector/CLI processes have a 180-second deadline; independent readback
# has a separate 60-second deadline, in addition to authenticated fixture setup.
@pytest.mark.timeout(300)
async def test_original_selector_readback_and_partial_receipts(
    original_setup,
    external_security_providers,
    compiled_dispatch_catalogue,  # noqa: F811
    store,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from l5_services.models.configuration import ConfigurationReceipt
    from l5_services.models.run import Run

    from src.agentauth import arc_model
    from src.l5_integration import GatewayConfigurationBackend
    from src.orchestration import operations_release_job
    from src.products.models import ProductOperation
    from src.products.security_evidence_model import SecurityEvidenceEvent
    from src.products.source_capture_jobs import claim_capture, run_capture

    c = original_setup
    async with c.factory() as db:
        release = await build_original_security_release(db, c.factory, monkeypatch, store=store)
    c.app.dependency_overrides[operations_release_job.service] = lambda: release.service
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "L5-services/tests/live"))
    import run_security_live

    jwks = {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(c.key.public_key())), "kid": "offline-key", "use": "sig"}]}
    original_http = httpx.AsyncHTTPTransport.handle_async_request

    async def external_identity(transport, request):
        if str(request.url) == arc_model.ISSUER + "/.well-known/jwks":
            return httpx.Response(200, json=jwks, request=request)
        return await original_http(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", external_identity)
    for case in c.cases:
        await native_consent(c, case)
    tmp_path.chmod(0o700)
    guard_path = isolated_child_transport(tmp_path)
    selector_commands = []
    children = []

    def child(command, **kwargs):
        # Preserve actual pytest and CLI execution; inject only offline external
        # transport and explicit browser poisons into the child interpreter.
        environment = dict(kwargs["env"])
        environment["PYTHONPATH"] = guard_path + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTEST_PLUGINS"] = "offline_security_pytest"
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        selector_commands.append(command)
        timeout = kwargs.pop("timeout")
        kwargs.pop("check")
        assert kwargs.pop("capture_output")
        process = subprocess.Popen(command, **{**kwargs, "env": environment}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    monkeypatch.setattr(run_security_live.subprocess, "run", child)
    saves = []
    original_save = GatewayConfigurationBackend.save

    async def delayed_save(self, *args, **kwargs):
        if len(args) > 4 and args[4].startswith("offline_ui_receipt_"):
            saves.append(args[4])
            await asyncio.sleep(3)
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(GatewayConfigurationBackend, "save", delayed_save)
    try:
        async with loopback_gateway(c.app, tmp_path, release.claims, c.key) as (base, certificate, oidc_url):
            job, claims = release.job, release.claims
            environment = {
                "L5_SERVICE": "security",
                "L5_ENVIRONMENT": "test",
                "L5_RELEASE_OPERATION": "release",
                "GITHUB_REPOSITORY": claims["repository"],
                "GITHUB_REPOSITORY_ID": claims["repository_id"],
                "GITHUB_RUN_ID": claims["run_id"],
                "GITHUB_RUN_ATTEMPT": claims["run_attempt"],
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_WORKFLOW_REF": claims["workflow_ref"],
                "GITHUB_WORKFLOW_SHA": job.dispatch_revision,
                "GITHUB_SHA": job.dispatch_revision,
                "ADP_CORRELATION": job.correlation,
                "ADP_CONTRACT_DIGEST": job.contract_digest,
                "ADP_SOURCE_REVISION": job.source_revision,
                "ADP_DEFINITION_REVISION": job.dispatch_revision,
                "SOURCE_SHA": job.source_revision,
                "L5_CHECKOUT_REVISION": job.source_revision,
                "L5_RELEASE_ROLE_ARN": job.role_arn,
                "L5_MUTATION_ROLE_ARN": job.mutation_role_arn,
                "ADP_GATEWAY_BASE_URL": base,
                "ACTIONS_ID_TOKEN_REQUEST_URL": oidc_url,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "offline-request-token",
                "SSL_CERT_FILE": certificate,
                "NO_PROXY": "*",
                "no_proxy": "*",
            }
            credentials, fixture_cases = {}, []
            for case in c.cases:
                cli_home, deployment = cli_profile(case, tmp_path, base)
                current_binding = await c.client.get(case.path + "/bindings/" + case.binding_id, headers=case.headers)
                assert current_binding.status_code == 200, current_binding.text
                binding = current_binding.json()
                settings = copy.deepcopy(case.choices)
                settings["region"] = "us-west-2"
                settings["overrides"] = [item for item in settings["overrides"] if item["field"] != "region"] + [
                    {"field": "region", "value": "us-west-2", "reason": "Original CLI edit"}
                ]
                settings_file = tmp_path / f"settings-{case.number}.json"
                private_json(settings_file, settings)
                scope = case.configuration["scope"]
                record = {
                    "scope": scope,
                    "api_url": base,
                    "ui_url": base,
                    "cli_home": cli_home,
                    "deployment_id": deployment,
                    "journal_file": str(tmp_path / f"attempts-{case.number}.jsonl"),
                    "cli_settings_file": str(settings_file),
                    "initial_region": "us-east-1",
                    "cli_region": "us-west-2",
                    "ui_region": "us-east-1",
                    "ui_reason": "Original human UI edit",
                    "cli_receipt": f"offline_cli_receipt_{case.number}",
                    "ui_receipt": f"offline_ui_receipt_{case.number}",
                    "mcp_receipt": f"offline_mcp_receipt_{case.number}",
                    "spend_limit_usd": "1.00",
                    "provider": {**case.identity, "label": "Offline selected provider"},
                    "selected": {
                        "scope": scope,
                        "configuration_id": case.configuration["configuration_id"],
                        "binding_id": case.binding_id,
                        "connection_ref": case.connection,
                        "provider_identity": case.identity,
                        "sources": [case.source],
                        "binding_state": binding["state"],
                        "readiness": binding["readiness"],
                    },
                    "run_input": {"binding_id": case.binding_id, "capability": "code_review", "sources": [case.source]},
                }
                for field, slot in (
                    ("api_token_env", "api"),
                    ("mcp_token_env", "mcp"),
                    ("ui_access_token_env", "legacy_ui"),
                    ("ui_id_token_env", "legacy_ui"),
                ):
                    name = f"OFFLINE_SECURITY_{case.number}_{field.upper()}"
                    record[field] = name
                    credentials[name] = case.tokens[slot]
                fixture_cases.append(record)
            fixture = {
                "schema_version": 1,
                "authorized": True,
                "fixture_owner": "Offline test owner",
                "authorization": {
                    "source_sha": job.source_revision,
                    "scenarios": ["setup"],
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
                "release": {component: job.source_revision for component in ("api", "cli", "ui", "mcp", "worker")},
                "limits": {"max_attempts": 3, "deadline_seconds": 180, "max_spend_usd": "2.00"},
                "cases": fixture_cases,
            }
            fixture_path = tmp_path / "fixture.json"
            private_json(fixture_path, fixture)
            environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
            for name, value in environment.items():
                monkeypatch.setenv(name, value)

            processed = set()
            completed_captures = []

            async def capture_worker():
                while True:
                    identity = await claim_capture(c.factory)
                    if identity is None:
                        await asyncio.sleep(0.05)
                        continue
                    await run_capture(c.factory, identity)
                    async with c.factory() as db:
                        job = await db.get(ProductOperation, identity[:2])
                        assert job.state == "succeeded", job.receipt
                        assert job.attempt_count == 1 and job.lease_owner is None
                        completed_captures.append(job)

            async def human_actions():
                while True:
                    async with c.factory() as db:
                        requests = list(await db.scalars(select(SecurityEvidenceEvent).where(SecurityEvidenceEvent.kind == "request")))
                    for request in requests:
                        if request.id in processed:
                            continue
                        case = next(item for item in c.cases if item.actor.org_id == request.tenant_id)
                        path = case.path + "/release-evidence/requests/" + request.request_id
                        status = await c.client.get(path, headers=case.headers)
                        assert status.status_code == 200, status.text
                        kinds = {receipt["kind"] for receipt in status.json()["receipts"]}
                        if not {"credential_" + slot for slot in ("api", "cli", "mcp", "legacy_ui")} <= kinds:
                            continue
                        automation = {"Authorization": "Bearer " + case.tokens["api"]}
                        denied = await c.client.post(path + "/preview", json={}, headers=automation)
                        assert denied.status_code == 403, denied.text
                        foreign = next(item for item in c.cases if item is not case)
                        denied = await c.client.get(path, headers=foreign.headers)
                        assert denied.status_code == 404, denied.text
                        preview = await c.client.post(path + "/preview", json={}, headers=case.headers)
                        assert preview.status_code == 200, preview.text
                        value = preview.json()
                        consent = await c.client.post(
                            path + "/endorse",
                            json={
                                "snapshot_sha256": value["snapshot_sha256"],
                                "expires_at": value["maximum_expires_at"],
                                "observer_id": case.actor.user_id,
                            },
                            headers=case.headers,
                        )
                        assert consent.status_code == 200, consent.text
                        prepared = await c.client.post(path + "/prepare", json={}, headers=case.headers)
                        assert prepared.status_code == 200, prepared.text
                        phase = request.document["selection"]["phase"]
                        selected = request.document["selection"]
                        if phase == "edit":
                            before = prepared.json()["snapshot"]["configuration"]
                            overrides = [item for item in before["settings"]["overrides"] if item["field"] != "region"] + [
                                {"field": "region", "value": selected["ui_region"], "reason": selected["ui_reason"]}
                            ]
                            response = await c.client.put(
                                case.path + "/configuration",
                                json={"region": selected["ui_region"], "overrides": overrides},
                                headers={
                                    **case.headers,
                                    "If-Match": '"' + before["hash"] + '"',
                                    "Idempotency-Key": selected["ui_receipt"],
                                    "X-ADP-Security-Evidence": request.request_id,
                                },
                            )
                            assert response.status_code == 200, response.text
                            await native_consent(c, case)
                        elif phase == "display":
                            response = await c.client.post(
                                path + "/observe",
                                headers=case.headers,
                                json={
                                    "snapshot_sha256": prepared.json()["snapshot_sha256"],
                                    "observation": "displayed",
                                    "configuration_revision": selected["configuration_revision"],
                                    "binding_revision": selected["binding_revision"],
                                    "run_id": selected["run_id"],
                                    "run_revision": selected["run_revision"],
                                },
                            )
                            assert response.status_code == 200, response.text
                        processed.add(request.id)
                    await asyncio.sleep(0.05)

            actor_task = asyncio.create_task(human_actions())
            capture_task = asyncio.create_task(capture_worker())
            selector_task = asyncio.create_task(
                asyncio.to_thread(run_security_live.run, "setup", job.source_revision, str(fixture_path), str(tmp_path), json.dumps(credentials))
            )
            try:
                done, _ = await asyncio.wait((actor_task, capture_task, selector_task), return_when=asyncio.FIRST_COMPLETED, timeout=190)
                assert done, "original selector exceeded the offline test deadline"
                if actor_task in done:
                    await actor_task
                if capture_task in done:
                    await capture_task
                assert await selector_task == 0
            finally:
                actor_task.cancel()
                capture_task.cancel()
                for process in children:
                    if process.poll() is None:
                        process.terminate()
                await asyncio.gather(actor_task, capture_task, return_exceptions=True)
                await asyncio.gather(selector_task, return_exceptions=True)
            assert len(saves) == 2 and len(processed) == 6
            assert len(external_security_providers.captured) == 2
            assert len(completed_captures) == 2
            for capture_job, case in zip(completed_captures, fixture_cases):
                assert capture_job.tenant_id == case["scope"]["tenant_id"]
                assert capture_job.request["admission_key"] == case["mcp_receipt"]
                assert capture_job.request["run_input"] == {**case["run_input"], "configuration_revision": 4}
            async with c.factory() as db:
                assert await db.scalar(select(func.count()).select_from(Run)) == 2
                for capture_job in completed_captures:
                    run = await db.scalar(select(Run).where(Run.tenant_id == capture_job.tenant_id))
                    assert run.input == capture_job.request["run_input"]
                    assert run.deadline_at == capture_job.expires_at
                    assert datetime.fromisoformat(capture_job.request["source_request"]["retain_until"]) == run.deadline_at.replace(tzinfo=UTC)
                assert await db.scalar(select(func.count()).select_from(ConfigurationReceipt).where(ConfigurationReceipt.operation == "edit")) == 6
                exports_before = await db.scalar(
                    select(func.count()).select_from(SecurityEvidenceEvent).where(SecurityEvidenceEvent.kind == "export")
                )
            # Run the independent original reader in another browser-poisoned
            # process. It must obtain fresh protected exports without effects.
            scripts = Path(__file__).resolve().parents[3] / "L5-services/scripts"
            reader = tmp_path / "independent_reader.py"
            reader.write_text("""import importlib.util, json, os, sys
from pathlib import Path
from offline_security_pytest import pytest_sessionstart
spec = importlib.util.spec_from_file_location("original_security_readbacks", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pytest_sessionstart(None)
fixture = json.loads(Path(os.environ["PRIVATE_FIXTURE"]).read_text())
credentials = json.loads(Path(sys.argv[2]).read_text())
records = [module.read_setup(case, fixture["limits"], credentials) for case in fixture["cases"]]
output = Path(sys.argv[3])
output.write_text(json.dumps(records))
output.chmod(0o600)
""")
            reader.chmod(0o600)
            credentials_path = tmp_path / "independent_credentials.json"
            private_json(credentials_path, credentials)
            readback_path = tmp_path / "setup_readback.json"
            result = await asyncio.to_thread(
                child,
                [sys.executable, str(reader), str(scripts / "qualify-security-readbacks.py"), str(credentials_path), str(readback_path)],
                env={**os.environ, "PRIVATE_FIXTURE": str(fixture_path)},
                capture_output=True,
                timeout=60,
                check=False,
            )
            assert result.returncode == 0, result.stderr.decode()
            readbacks = json.loads(readback_path.read_text())
            assert len(readbacks) == 2
            async with c.factory() as db:
                assert await db.scalar(select(func.count()).select_from(Run)) == 2
                assert await db.scalar(select(func.count()).select_from(ConfigurationReceipt).where(ConfigurationReceipt.operation == "edit")) == 6
                assert (
                    await db.scalar(select(func.count()).select_from(SecurityEvidenceEvent).where(SecurityEvidenceEvent.kind == "export"))
                    > exports_before
                )
            assert len(saves) == 2 and len(external_security_providers.captured) == 2
            assert len(external_security_providers.sent) == 2, external_security_providers.sent_work

            from l5_services.selector_receipts import SELECTOR_NODES, read_selector_receipts

            stem = f"security-{claims['run_id']}-{claims['run_attempt']}-setup"
            selector_log = (tmp_path / (stem + ".log")).read_bytes()
            receipt = json.loads((tmp_path / (stem + ".json")).read_text())
            assert receipt["selector_node"] == SELECTOR_NODES["setup"]
            assert receipt["source_revision"] == job.source_revision
            assert receipt["fixture_sha256"] == environment["L5_FIXTURE_DIGEST"]
            assert receipt["log_sha256"] == hashlib.sha256(selector_log).hexdigest()
            assert receipt["result"] == "passed" and b"1 passed" in selector_log
            assert all(record["configuration_revision"] == 4 for record in readbacks)
            # Only setup was executed. The actual oracle and emitter must refuse
            # native completion; no installed/assessment evidence is invented.
            monkeypatch.syspath_prepend(str(scripts))
            specification = importlib.util.spec_from_file_location("original_security_emitter", scripts / "emit-security-qualification.py")
            emitter = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(emitter)
            now = datetime.now(UTC)
            assert emitter.verified_setup({"setup": readbacks}, fixture, now, now) == 2
            environment.update(
                PRIVATE_EVIDENCE_DIR=str(tmp_path),
                PRIVATE_FIXTURE=str(fixture_path),
                WHEEL_DIGEST="a" * 64,
                ARCHIVE_DIGEST="b" * 64,
                MANIFEST_VERSION="offline-manifest",
                RUNNER_TEMP=str(tmp_path),
            )
            context = {
                "source_revision": job.source_revision,
                "definition_revision": job.dispatch_revision,
                "run_id": claims["run_id"],
                "run_attempt": claims["run_attempt"],
            }
            partial = {
                **context,
                "workflow_path": emitter.WORKFLOW_PATH,
                "wheel_digest": environment["WHEEL_DIGEST"],
                "archive_digest": environment["ARCHIVE_DIGEST"],
                "manifest_version": environment["MANIFEST_VERSION"],
                "fixture_digest": environment["L5_FIXTURE_DIGEST"],
                "observed_at": now.isoformat(),
                "setup": readbacks,
            }
            with pytest.raises(FileNotFoundError, match="assessments"):
                read_selector_receipts(tmp_path, environment, now)
            with pytest.raises(FileNotFoundError, match="assessments"):
                emitter.validate_native(partial, context, environment, fixture, now=now)
            private_json(tmp_path / f"security-readback-{claims['run_id']}-{claims['run_attempt']}.json", partial)
            with pytest.raises(FileNotFoundError, match="assessments"):
                emitter.emit(environment)
            assert not (tmp_path / "l5-security-qualification/qualification.json").exists()
            assert not (tmp_path / "delivery-evidence/delivery-evidence.json").exists()
    finally:
        await release.close()
