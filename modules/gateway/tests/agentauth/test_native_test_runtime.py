"""Actual app/auth/router integration with isolated PostgreSQL, Moto and Lua.

Keys, JWKS, STS, pricing, provider transports and usage storage are offline test
substitutes. These checks do not enroll a real fixture or attest a live runtime.
"""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import jwt
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cryptography.hazmat.primitives.asymmetric import rsa

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.native_test_configuration import CONFIGURATION_KIND, COVERAGE_CONTRACT, NativeTestDeployment, deployment_key
from src.agentauth.native_test_enrollment import CAPABILITY_HEADER, OIDC_AUDIENCE, OPERATION_HEADER
from src.agentauth.native_test_runtime import compose_native_test_runtime
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from src.auth.cognito_jwt import CognitoJWTValidator
from src.shared.schemas.budget import EnforcementResult
from src.shared.schemas.common import RateLimitCheckResult
from tests.agentauth import test_native_test_enrollment as enrollment_tests
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission = enrollment_tests.admission
sessions = enrollment_tests.sessions
native = enrollment_tests.native
PAID_ROUTES = (
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/bedrock/invoke",
    "/bedrock/invoke-with-response-stream",
    "/model/offline-model/invoke",
    "/model/offline-model/invoke-with-response-stream",
    "/openai/v1/responses",
)


def write_configuration(f, **changes):
    configuration = f.configuration.model_copy(update=changes)
    f.native.fixture.client.put_item(
        TableName=f.native.store.table,
        Item=f.native.store.item(deployment_key(configuration.deployment_id), CONFIGURATION_KIND, configuration.model_dump()),
    )
    return configuration


@pytest.fixture
async def runtime(native, monkeypatch):
    from src import app as app_module
    from src.admin.middleware import RequestLoggingMiddleware
    from src.agentauth import arc_model, native_test_runtime, work_routes
    from src.auth import middleware
    from src.budget import enforcement_middleware
    from src.proxy import routes, service
    from src.shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "cognito_user_pool_id", "us-east-1_offline")
    monkeypatch.setattr(settings, "cognito_client_id", "offline-client")
    monkeypatch.setattr(settings, "enforce_org_assignment", True)
    monkeypatch.setattr(settings, "approval_fail_open", False)
    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.approval_middleware.get_settings", lambda: settings)
    monkeypatch.setenv("RATELIMIT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: native.sessions)
    monkeypatch.setattr(RequestLoggingMiddleware, "_log_request", AsyncMock())
    monkeypatch.setattr(app_module, "UNIT_MODULES", ["src.proxy.routes"])

    # Keep the real signature/issuer/client/expiry validation and real current
    # membership checks. Only public-key retrieval is replaced offline.
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk["kid"] = "offline-key"
    validator = CognitoJWTValidator(user_pool_id="us-east-1_offline", client_id="offline-client", agent_clients_table="")
    monkeypatch.setattr(validator.jwk_client, "fetch_data", lambda: {"keys": [jwk]})
    monkeypatch.setattr(middleware, "_cognito_validator", validator)

    def login(subject="jwt-subject", **changes):
        now = int(datetime.now(UTC).timestamp())
        return jwt.encode(
            {
                "iss": validator.issuer,
                "client_id": "offline-client",
                "sub": subject,
                "username": subject,
                "token_use": "access",
                "iat": now,
                "exp": now + 3600,
                "custom:org_id": "tenant-one",
                "custom:team_id": "team-one",
                "custom:department_id": "department-one",
                "custom:account_type": "human",
                **changes,
            },
            signing_key,
            algorithm="RS256",
            headers={"kid": "offline-key"},
        )

    authority_calls = []

    def proof_transport(request):
        authority_calls.append(request)
        if request.method == "GET":
            assert str(request.url) == arc_model.ISSUER + "/.well-known/jwks"
            return httpx.Response(200, json={"keys": [jwk]})
        assert str(request.url) == "https://sts.us-east-1.amazonaws.com/"
        assert request.content.decode() == work_routes.STS_BODY
        assert "x-adp-work-invocation" in request.headers["authorization"]
        return httpx.Response(
            200,
            content=(
                "<GetCallerIdentityResponse><GetCallerIdentityResult>"
                "<Arn>arn:aws:sts::123456789012:assumed-role/offline-runner/offline-job</Arn>"
                "</GetCallerIdentityResult></GetCallerIdentityResponse>"
            ),
        )

    offline_httpx = SimpleNamespace(**vars(httpx))
    offline_httpx.AsyncClient = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(proof_transport), **kwargs)
    monkeypatch.setattr(arc_model, "httpx", offline_httpx)
    monkeypatch.setattr(work_routes, "httpx", offline_httpx)

    def compose(gateway):
        return compose_native_test_runtime(
            gateway,
            store=native.store,
            session_factory=native.sessions,
            policy_store=native.issuer.policy_store,
            budget=native.fixture.budget,
            deployment_id="offline-gateway",
            clock=lambda: native.fixture.clock[0],
        )

    monkeypatch.setattr(native_test_runtime, "build_native_test_runtime", compose)
    app = app_module.create_app()
    rate = app.state.ratelimit_service
    for name in ("check_rate_limit", "consume_rate_limit"):
        monkeypatch.setattr(rate, name, AsyncMock(return_value=RateLimitCheckResult(allowed=True, limit=100, remaining=99)))
    monkeypatch.setattr(rate, "release_concurrent", AsyncMock())
    ordinary_budget = SimpleNamespace(
        _get_session=lambda **kwargs: native.sessions(),
        prepare_enforcement_context=AsyncMock(return_value=None),
        check_budget_hierarchy=AsyncMock(return_value=EnforcementResult(allowed=True)),
        reconcile_reservation=AsyncMock(),
        estimate_cost_from_payload_size=MagicMock(return_value=Decimal("0.01")),
    )
    monkeypatch.setattr(enforcement_middleware, "budget_enforcement_service", ordinary_budget)
    app.middleware_stack = app.build_middleware_stack()
    composition = app.state.native_test_runtime
    native.issuer, native.service = composition.enrollment, composition.admission
    configuration = NativeTestDeployment(
        deployment_id="offline-gateway",
        tenant_id="tenant-one",
        revision=str(uuid4()),
        state="active",
        scope="model_only",
        cap_usd="20",
        coverage_contract=COVERAGE_CONTRACT,
        coverage_digest=composition.coverage(),
        expires_at=int(native.fixture.clock[0].timestamp()) + 600,
        jobs=[native.request.job],
        allowed_models=["offline-model", "openai.gpt-5.6-luna"],
        allowed_tools=[enrollment_tests.TOOL],
        allowed_routes=["/v1/messages", "/openai/v1/responses"],
    )
    f = SimpleNamespace(
        native=native,
        settings=settings,
        app=app,
        composition=composition,
        configuration=configuration,
        login=login,
        signing_key=signing_key,
        authority_calls=authority_calls,
        rate=rate,
        ordinary_budget=ordinary_budget,
        provider_calls=[],
        usage=[],
    )
    write_configuration(f)

    # Real ProxyService.messages/translation/dispatch; only the SDK transport,
    # model catalogue, route decision and usage persistence are substituted.
    async def invoke(**kwargs):
        before = kwargs.pop("_native_test_before_submit", None)
        if before:
            before()
        f.provider_calls.append(kwargs)
        response = {
            "id": "offline-message",
            "content": [{"type": "text", "text": "offline"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model": "offline-model",
            "role": "assistant",
            "type": "message",
            "stop_reason": "end_turn",
        }
        return {"body": BytesIO(json.dumps(response).encode())}

    async def invoke_stream(**kwargs):
        await invoke(**kwargs)
        return {
            "body": [
                {"chunk": {"bytes": json.dumps(frame).encode()}}
                for frame in [
                    {"type": "message_start", "message": {"id": "offline-message", "usage": {"input_tokens": 1}}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
            ]
        }

    sdk = SimpleNamespace(
        meta=SimpleNamespace(
            config=SimpleNamespace(retries={"total_max_attempts": 1}),
            region_name="us-east-1",
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        )
    )
    sdk_client = SimpleNamespace(_invoke_client=sdk, _streaming_client=sdk, invoke_model=invoke, invoke_model_with_response_stream=invoke_stream)
    pool = SimpleNamespace(get_client=AsyncMock(return_value=sdk_client))
    resolver = SimpleNamespace(resolve_model=lambda model: model, check_model_access=MagicMock())
    proxy = service.ProxyService(pool, model_resolver=resolver)

    async def usage(**kwargs):
        # Deliberately leave the admitted hold in place. Provider observation is
        # not a durable cost receipt; the budget test must not erase uncertainty.
        f.usage.append(kwargs)
        return False

    monkeypatch.setattr(proxy, "_log_usage", usage)
    monkeypatch.setattr(service, "resolve_routing_decision", AsyncMock(return_value=service.RoutingDecision()))
    monkeypatch.setattr(routes, "get_chat_logging_service", lambda: MagicMock())
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    app.dependency_overrides[routes.get_model_resolver] = lambda: resolver
    f.proxy, f.pool, f.resolver = proxy, pool, resolver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline", headers={"Content-Type": "application/json"}
    ) as client:
        f.client = client
        yield f


async def enroll_http(f, *, oidc_changes=None, proof_changes=None):
    response = await f.client.post(
        "/v1/native-tests/approvals",
        headers={"Authorization": "Bearer " + f.login("owner-subject")},
        json={"subject_id": "jwt-subject", "approval": f.native.request.model_dump()},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    approval = f.native.store.approval("tenant-one", f.native.request.run_id)
    now = int(datetime.now(UTC).timestamp())
    oidc = jwt.encode(
        {
            **approval.job.model_dump(exclude={"runner_role", "job_workflow_ref"}),
            "iss": "https://token.actions.githubusercontent.com",
            "aud": OIDC_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            **(oidc_changes or {}),
        },
        f.signing_key,
        algorithm="RS256",
        headers={"kid": "offline-key"},
    )
    invocation = envelope_digest(
        {
            "tenant_id": "tenant-one",
            "run_id": approval.run_id,
            "approval_revision": approval.revision,
            "github_oidc_token": oidc,
            "deployment_id": "offline-gateway",
        }
    )
    request = AWSRequest(
        method="POST",
        url="https://sts.us-east-1.amazonaws.com/",
        data="Action=GetCallerIdentity&Version=2011-06-15",
        headers={"x-adp-work-invocation": invocation, "Content-Type": "application/x-www-form-urlencoded"},
    )
    SigV4Auth(Credentials("testing", "testing", "testing"), "sts", "us-east-1").add_auth(request)
    proof = {k.lower(): v for k, v in request.headers.items()}
    proof.update(proof_changes or {})
    exchange_body = {
        "run_id": approval.run_id,
        "github_oidc_token": oidc,
        "producer_proof": base64.b64encode(json.dumps(proof).encode()).decode(),
    }
    response = await f.client.post("/v1/native-tests/capabilities/exchange", headers={"Authorization": "Bearer " + f.login()}, json=exchange_body)
    f.approval, f.exchange_body = approval, exchange_body
    if response.status_code == 200:
        f.token = response.json()["capability"]
    return response


def model_headers(f, *, login=None, operation=None):
    return {
        "Authorization": "Bearer " + (login or f.login()),
        CAPABILITY_HEADER: f.token,
        OPERATION_HEADER: operation or str(uuid4()),
    }


@pytest.mark.parametrize("path", PAID_ROUTES)
@pytest.mark.parametrize("auth_header", ["Authorization", "X-Api-Key"])
async def test_designation_cannot_omit_capability_on_any_paid_route(runtime, path, auth_header):
    f = runtime
    token = f.login()
    response = await f.client.post(path, headers={auth_header: ("Bearer " if auth_header == "Authorization" else "") + token}, json={})
    assert response.status_code == 403, response.text
    assert response.json()["error"] in {"mandatory_native_test_capability_required", "native_test_model_route_unsupported"}
    assert not f.provider_calls
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()
    f.rate.check_rate_limit.assert_not_awaited()


async def test_http_approval_exchange_one_shot_revocation(runtime):
    f = runtime
    response = await enroll_http(f)
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "model_only" and response.json()["all_in_admission"] == "unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert len(f.authority_calls) == 2
    capability = f.native.store.capability("tenant-one", f.approval.credential_id)
    assert f.token not in json.dumps(capability)
    replay = await f.client.post("/v1/native-tests/capabilities/exchange", headers={"Authorization": "Bearer " + f.login()}, json=f.exchange_body)
    assert replay.status_code == 403 and f.token not in replay.text
    revoked = await f.client.post(
        "/v1/native-tests/capabilities/revoke",
        headers={"Authorization": "Bearer " + f.login("owner-subject")},
        json={"run_id": f.approval.run_id},
    )
    assert revoked.status_code == 200 and revoked.headers["cache-control"] == "no-store"
    denied = await f.client.post("/v1/messages", headers=model_headers(f), content=enrollment_tests.BODY)
    assert denied.status_code == 403 and not f.provider_calls


@pytest.mark.parametrize("changes", [{"sha": "c" * 40}, {"aud": "other"}, {"run_attempt": "2"}])
async def test_exchange_verifies_signed_exact_job(runtime, changes):
    response = await enroll_http(runtime, oidc_changes=changes)
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert (
        runtime.native.store.document(
            runtime.native.store.key("tenant-one", "CAPABILITY", runtime.approval.credential_id), "native_test_capability_v1"
        )
        is None
    )


async def test_exchange_verifies_producer_envelope(runtime):
    response = await enroll_http(runtime, proof_changes={"x-adp-work-invocation": "wrong"})
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert (
        runtime.native.store.document(
            runtime.native.store.key("tenant-one", "CAPABILITY", runtime.approval.credential_id), "native_test_capability_v1"
        )
        is None
    )


@pytest.mark.parametrize("change", ["signature", "issuer", "client", "expired", "absent"])
async def test_actual_auth_rejects_unverified_login(runtime, change):
    f = runtime
    changes = {"issuer": {"iss": "https://unapproved.test"}, "client": {"client_id": "unapproved"}, "expired": {"exp": 1}}
    token = f.login(**changes.get(change, {}))
    if change == "signature":
        a, b, c = token.split(".")
        token = ".".join((a, b, ("A" if c[0] != "A" else "B") + c[1:]))
    headers = {} if change == "absent" else {"Authorization": "Bearer " + token}
    response = await f.client.post("/v1/messages", headers=headers, content=enrollment_tests.BODY)
    assert response.status_code == 401 and not f.provider_calls
    response = await f.client.post("/v1/native-tests/capabilities/exchange", headers=headers, json={})
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("stream", [False, True])
async def test_real_messages_dispatch_original_hold_cap_and_replay(runtime, stream):
    f = runtime
    assert (await enroll_http(f)).status_code == 200
    body = json.dumps({**json.loads(enrollment_tests.BODY), "stream": stream}).encode()
    headers = model_headers(f)
    response = await f.client.post("/v1/messages", headers=headers, content=body)
    # The offline usage store does not confirm settlement. Nonstream fails;
    # streaming carries its failure event after the observed provider frames.
    assert response.status_code == (200 if stream else 502), response.text
    assert len(f.provider_calls) == 1
    f.pool.get_client.assert_awaited_once_with(None, single_attempt=True)
    operation = f.native.store.read_operation("tenant-one", f.approval.run_id, headers[OPERATION_HEADER])
    assert operation["status"] == "dispatch_started"
    assert f.usage[-1]["context"]._policy_request_id == operation["reservation_id"]
    assert f.usage[-1]["context"]._budget_admission_targets
    assert (await f.native.fixture.reservations.snapshot(f.native.service._target(f.approval.authority()))).total_usd == 12
    replay = await f.client.post("/v1/messages", headers=headers, content=body)
    assert replay.status_code == 409
    denied = await f.client.post("/v1/messages", headers=model_headers(f), content=body)
    assert denied.status_code == 403 and len(f.provider_calls) == 1
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()
    assert f.rate.check_rate_limit.await_count == 1


@pytest.mark.parametrize("change", ["missing_registry", "read_failure", "revoked", "expired", "coverage", "missing_enrollment"])
async def test_runtime_or_protected_configuration_failure_refuses_designated(runtime, monkeypatch, change):
    f = runtime
    assert (await enroll_http(f)).status_code == 200
    if change == "missing_registry":
        f.composition.store = None
    elif change == "read_failure":
        monkeypatch.setattr(f.native.store, "designation", MagicMock(side_effect=RuntimeError("private database error")))
    elif change == "missing_enrollment":
        f.composition.enrollment = None
    else:
        write_configuration(f, **{"revoked": {"state": "revoked"}, "expired": {"expires_at": 1}, "coverage": {"coverage_digest": "f" * 64}}[change])
    response = await f.client.post("/v1/messages", headers=model_headers(f), content=enrollment_tests.BODY)
    assert response.status_code == 403 and "private database" not in response.text and not f.provider_calls
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()


async def test_ordinary_negative_lookup_preserves_model_path_without_enrollment(runtime):
    f = runtime
    f.composition.enrollment = None
    response = await f.client.post("/v1/messages", headers={"Authorization": "Bearer " + f.login("owner-subject")}, content=enrollment_tests.BODY)
    assert response.status_code == 502  # Same explicit offline settlement refusal.
    assert len(f.provider_calls) == 1
    f.pool.get_client.assert_awaited_once_with(None)
    f.ordinary_budget.check_budget_hierarchy.assert_awaited_once()
    f.rate.check_rate_limit.assert_awaited_once()
    assert f.usage[-1]["context"]._native_test_handoff is None


async def test_verified_subject_survives_service_alias_even_with_approval_disabled(runtime, monkeypatch):
    f = runtime
    monkeypatch.setattr(f.settings, "enforce_org_assignment", False)
    login = f.login(username="", **{"custom:account_type": "service"})
    response = await f.client.post("/v1/messages", headers={"Authorization": "Bearer " + login}, content=enrollment_tests.BODY)
    assert response.status_code == 403 and response.json()["error"] == "native_test_designation_refused"
    assert not f.provider_calls
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()


@pytest.mark.parametrize("kind", ["duplicate", "secret_validation", "oversized", "wrong_owner"])
async def test_control_request_failures_do_not_echo_sensitive_proofs(runtime, kind):
    f = runtime
    marker = "private-proof-must-not-appear"
    payload = {
        "duplicate": '{"run_id":"a","run_id":"b","producer_proof":"' + marker + '"}',
        "secret_validation": json.dumps({"run_id": "bad", "producer_proof": marker, "github_oidc_token": marker}),
        "oversized": marker * 4000,
        "wrong_owner": json.dumps({"subject_id": "jwt-subject", "approval": f.native.request.model_dump()}),
    }[kind]
    path = "/v1/native-tests/approvals" if kind == "wrong_owner" else "/v1/native-tests/capabilities/exchange"
    response = await f.client.post(path, headers={"Authorization": "Bearer " + f.login()}, content=payload)
    assert response.status_code in {400, 403, 413}
    assert marker not in response.text and response.headers["cache-control"] == "no-store"


async def test_new_provider_alias_is_guarded_and_invalidates_coverage(runtime):
    f = runtime
    hits = []

    @f.app.post("/new-paid-alias", tags=["proxy"])
    async def alias():
        hits.append(True)
        return {}

    with pytest.raises(AdmissionRefusedError, match="coverage"):
        f.composition.coverage()
    response = await f.client.post("/new-paid-alias", headers={"Authorization": "Bearer " + f.login()}, json={})
    assert response.status_code == 401 and not hits


@pytest.mark.parametrize("change", ["missing", "reordered", "duplicate"])
async def test_protected_registration_rejects_middleware_coverage_drift(runtime, change):
    from src.auth.middleware import TokenContextMiddleware

    entries = runtime.app.user_middleware
    entry = next(item for item in entries if item.cls is TokenContextMiddleware)
    if change != "duplicate":
        entries.remove(entry)
    if change != "missing":
        entries.append(entry)
    with pytest.raises(AdmissionRefusedError, match="coverage"):
        runtime.composition.require_enrollment()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("known_usage", [False, True])
async def test_real_responses_route_preserves_original_reservation_and_settlement(runtime, monkeypatch, stream, known_usage):
    from src.orchestration import provider_quotes
    from src.proxy import mantle_service, routes

    f = runtime
    model = "openai.gpt-5.6-luna"
    f.native.request = f.native.request.model_copy(update={"allowed_models": [model]})
    assert (await enroll_http(f)).status_code == 200
    original_quote = provider_quotes.quote_request

    async def quote(*args, **kwargs):
        return replace(await original_quote(*args, **kwargs), billing_model_id=model)

    monkeypatch.setattr(provider_quotes, "quote_request", quote)
    monkeypatch.setattr(routes, "_mantle_model_configured", lambda name: name == model)
    operation_id = str(uuid4())
    response_body = {"id": "offline-response", "output": []}
    if known_usage:
        response_body["usage"] = {"input_tokens": 2, "output_tokens": 1}
    wire = json.dumps(response_body).encode()
    if stream:
        wire = b'data: {"type":"response.completed","response":' + wire + b"}\n\n"

    def upstream(request):
        assert json.loads(request.content)["model"] == "us." + model
        assert f.native.store.read_operation("tenant-one", f.approval.run_id, operation_id)["status"] == "dispatch_started"
        f.provider_calls.append(request)
        return httpx.Response(200, content=wire)

    async def routing(*args, **kwargs):
        assert f.native.store.read_operation("tenant-one", f.approval.run_id, operation_id)["status"] == "admitted"
        return mantle_service.RoutingDecision()

    monkeypatch.setattr(mantle_service, "resolve_routing_decision", routing)
    monkeypatch.setattr(mantle_service, "get_session_factory", lambda: lambda: AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(mantle_service, "UsageService", lambda db: SimpleNamespace(log_request=log))
    reconcile = AsyncMock(side_effect=f.native.fixture.budget.reconcile_reservation)
    monkeypatch.setattr(mantle_service, "reconcile_budget_reservation", reconcile)
    auth = MagicMock()
    auth.sign.return_value = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
        service = mantle_service.MantlePassthroughService(
            auth, "https://bedrock-runtime.us-east-1.amazonaws.com", inference_profile_prefix="us", http_client=transport
        )
        service._chat_logger = MagicMock()
        f.app.dependency_overrides[routes.get_mantle_service] = lambda: service
        response = await f.client.post(
            "/openai/v1/responses",
            headers=model_headers(f, operation=operation_id),
            json={"model": model, "input": "offline", "max_output_tokens": 16, "stream": stream},
        )
    assert response.status_code == (200 if known_usage or stream else 502), response.text
    assert len(f.provider_calls) == 1
    args = log.await_args.kwargs
    operation = f.native.store.read_operation("tenant-one", f.approval.run_id, operation_id)
    assert args["request_id"] == operation["reservation_id"] == args["context"]._policy_request_id
    assert args["context"]._budget_admission_targets
    assert args["reservation_usage_known"] is known_usage
    assert reconcile.await_args.kwargs["context"] is args["context"]
    total = (await f.native.fixture.reservations.snapshot(f.native.service._target(f.approval.authority()))).total_usd
    assert total == args["cost_usd"] if known_usage else total == 12
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()


async def test_configuration_change_between_read_and_dispatch_cas_refuses_provider(runtime, monkeypatch):
    f = runtime
    assert (await enroll_http(f)).status_code == 200
    original = f.native.store.client.transact_write_items
    mutations = []

    def change_before_cas(**kwargs):
        if any('"dispatch_started"' in json.dumps(item) for item in kwargs["TransactItems"]):
            mutations.append(True)
            write_configuration(f, state="revoked")
        return original(**kwargs)

    monkeypatch.setattr(f.native.store.client, "transact_write_items", change_before_cas)
    headers = model_headers(f)
    response = await f.client.post("/v1/messages", headers=headers, content=enrollment_tests.BODY)
    assert response.status_code >= 400 and mutations and not f.provider_calls
    operation = f.native.store.read_operation("tenant-one", f.approval.run_id, headers[OPERATION_HEADER])
    assert operation["status"] == "admitted"
    assert (await f.native.fixture.reservations.snapshot(f.native.service._target(f.approval.authority()))).total_usd == 12
    revoked = await f.client.post(
        "/v1/native-tests/capabilities/revoke",
        headers={"Authorization": "Bearer " + f.login("owner-subject")},
        json={"run_id": f.approval.run_id},
    )
    assert revoked.status_code == 200  # Revocation remains possible after deployment is revoked.


async def test_designation_disappearance_between_runtime_reads_never_becomes_ordinary(runtime, monkeypatch):
    f = runtime
    monkeypatch.setattr(f.native.store, "designation", MagicMock(side_effect=[f.native.designation, None]))
    response = await f.client.post("/v1/messages", headers={"Authorization": "Bearer " + f.login()}, content=enrollment_tests.BODY)
    assert response.status_code == 403 and not f.provider_calls
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()


async def test_rate_limit_still_denies_native_handoff_and_retains_original_hold(runtime):
    f = runtime
    assert (await enroll_http(f)).status_code == 200
    f.rate.check_rate_limit.return_value = RateLimitCheckResult(allowed=False, limit=1, remaining=0)
    response = await f.client.post("/v1/messages", headers=model_headers(f), content=enrollment_tests.BODY)
    assert response.status_code == 429 and not f.provider_calls
    assert (await f.native.fixture.reservations.snapshot(f.native.service._target(f.approval.authority()))).total_usd == 12


@pytest.mark.parametrize("change", ["duplicate", "wrong", "cross_tenant", "service", "native_header_on_ordinary"])
async def test_header_and_auth_scope_changes_cannot_shed_designation(runtime, monkeypatch, change):
    f = runtime
    assert (await enroll_http(f)).status_code == 200
    headers = model_headers(f)
    if change == "duplicate":
        headers = [*headers.items(), (CAPABILITY_HEADER, f.token)]
    elif change == "wrong":
        headers[CAPABILITY_HEADER] += "x"
    elif change in {"cross_tenant", "service"}:
        # Approval's break-glass switch must not disable mandatory native scope.
        monkeypatch.setattr(f.settings, "enforce_org_assignment", False)
        claims = {"custom:org_id": "other"} if change == "cross_tenant" else {"custom:account_type": "service"}
        headers["Authorization"] = "Bearer " + f.login(**claims)
    else:
        headers["Authorization"] = "Bearer " + f.login("owner-subject")
    response = await f.client.post("/v1/messages", headers=headers, content=enrollment_tests.BODY)
    assert response.status_code == 403 and not f.provider_calls
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()


@pytest.mark.parametrize("change", ["missing", "revoked", "expired", "scope", "models", "tools", "job"])
async def test_approval_requires_protected_current_deployment_scope(runtime, change):
    f = runtime
    if change == "missing":
        f.native.fixture.client.delete_item(TableName=f.native.store.table, Key=deployment_key("offline-gateway"))
    else:
        changes = {
            "revoked": {"state": "revoked"},
            "expired": {"expires_at": 1},
            "scope": {"scope": "all_in"},
            "models": {"allowed_models": ["other"]},
            "tools": {"allowed_tools": ["other"]},
            "job": {"jobs": [f.native.request.job.model_copy(update={"sha": "c" * 40})]},
        }
        write_configuration(f, **changes[change])
    response = await f.client.post(
        "/v1/native-tests/approvals",
        headers={"Authorization": "Bearer " + f.login("owner-subject")},
        json={"subject_id": "jwt-subject", "approval": f.native.request.model_dump()},
    )
    assert response.status_code == 403 and not f.provider_calls
    assert f.native.store.document(f.native.store.key("tenant-one", "APPROVAL", f.native.request.run_id), "native_test_approval_v1") is None


@pytest.mark.parametrize("configuration", ["none", "malformed", "registry_only", "full"])
async def test_environment_factory_never_manufactures_a_negative_registry(native, monkeypatch, configuration):
    from fastapi import FastAPI

    from src.agentauth.native_test_runtime import build_native_test_runtime

    for name in ("ADP_NATIVE_TEST_AUTHORITY_TABLE", "ADP_NATIVE_TEST_DEPLOYMENT_ID", "AGENT_AUTHORITY_TABLE", "BG_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    if configuration == "malformed":
        # An operator bound a value that is not a table name (an ARN). They meant
        # to adopt the boundary, so this is "configured" and must fail closed,
        # never "not deployed here".
        monkeypatch.setenv("ADP_NATIVE_TEST_AUTHORITY_TABLE", "arn:aws:dynamodb:us-east-1:123456789012:table/" + native.store.table)
    elif configuration != "none":
        monkeypatch.setenv("ADP_NATIVE_TEST_AUTHORITY_TABLE", native.store.table)
    if configuration == "full":
        monkeypatch.setenv("ADP_NATIVE_TEST_DEPLOYMENT_ID", "offline-gateway")
        monkeypatch.setenv("AGENT_AUTHORITY_TABLE", native.store.table)
        monkeypatch.setenv("BG_REDIS_URL", "redis://offline.invalid:6379")
    runtime = build_native_test_runtime(FastAPI())
    if configuration == "none":
        assert runtime.store is None and runtime.enrollment is None and not runtime.configured
    elif configuration == "malformed":
        assert runtime.store is None and runtime.enrollment is None and runtime.configured
    else:
        assert runtime.configured
        assert runtime.store.designation("jwt-subject") == native.designation
        assert (runtime.enrollment is not None) is (configuration == "full")
    with pytest.raises(AdmissionRefusedError):
        runtime.require_enrollment()  # No real registered middleware/source yet.


@pytest.mark.parametrize("path", PAID_ROUTES)
async def test_provenance_verified_iam_registry_subject_cannot_shed_designation(runtime, monkeypatch, path):
    from src.auth import agent_registry

    f = runtime
    monkeypatch.setattr(f.settings, "trust_apigw_headers", True)
    monkeypatch.setattr(f.settings, "apigw_provenance_secret", "offline-edge-proof")
    designation = f.native.designation.model_copy(update={"subject_id": "iam-agent:offline-agent"})
    f.native.fixture.client.put_item(
        TableName=f.native.store.table,
        Item=f.native.store.item(f.native.store.fixture_key(designation.subject_id), "native_test_fixture_v1", designation.model_dump()),
    )
    lookup = MagicMock(
        return_value={
            "agent_id": "offline-agent",
            "agent_name": "offline",
            "org_id": "tenant-one",
            "team_id": "team-one",
            "scope": "internal",
            "role_arn": "arn:aws:iam::123456789012:role/offline-runner",
        }
    )
    monkeypatch.setattr(agent_registry, "get_agent_registry_service", lambda: SimpleNamespace(get_agent_by_role_arn=lookup))
    headers = {
        "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/offline-runner/offline-job",
        "X-Adp-Edge-Provenance": "offline-edge-proof",
        "X-Agent-OrgId": "other-billing-tenant",
    }
    response = await f.client.post(path, headers=headers, json={})
    assert response.status_code == 403 and response.json()["error"] == "native_test_designation_refused"
    lookup.assert_called_once_with("arn:aws:iam::123456789012:role/offline-runner")
    assert not f.provider_calls
    f.ordinary_budget.check_budget_hierarchy.assert_not_awaited()
    f.rate.check_rate_limit.assert_not_awaited()
    headers["X-Adp-Edge-Provenance"] = "untrusted"
    response = await f.client.post(path, headers=headers, json={})
    assert response.status_code == 401 and lookup.call_count == 1


@pytest.mark.parametrize("path", PAID_ROUTES)
async def test_unconfigured_registry_passes_ordinary_traffic_and_configured_but_unbuilt_refuses(monkeypatch, path):
    """No registry bound to the deployment means nobody was ever designated.

    Regression: #8203 mounted this boundary unconditionally while every
    environment that had not yet run the registry Terraform had no
    ``ADP_NATIVE_TEST_AUTHORITY_TABLE``. ``store is None`` was treated as an
    unreadable registry and every paid model route on the gateway refused with
    403 ``native_test_registry_unavailable``.
    """
    from fastapi import FastAPI
    from starlette.responses import JSONResponse

    from src.agentauth.native_test_runtime import NativeTestRuntime, NativeTestRuntimeMiddleware

    downstream_calls = []

    async def downstream(scope, receive, send):
        downstream_calls.append(scope["path"])
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    def call(runtime, headers=()):
        gateway = FastAPI()
        monkeypatch.setattr("src.agentauth.native_test_runtime.build_native_test_runtime", lambda app: runtime)
        middleware = NativeTestRuntimeMiddleware(downstream, gateway=gateway)
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": list(headers),
            "state": {"token_context": SimpleNamespace(user_id="jwt-subject", _authenticated_subject_id="jwt-subject")},
        }
        sent = []

        async def send(message):
            sent.append(message)

        return middleware, scope, sent, send

    # Feature not deployed here: ordinary traffic reaches the provider route.
    not_deployed = NativeTestRuntime(None, None, configured=False)
    middleware, scope, sent, send = call(not_deployed)
    await middleware(scope, receive, send)
    assert sent[0]["status"] == 200 and downstream_calls == [path]

    # ...but a caller presenting native test headers is still refused.
    middleware, scope, sent, send = call(not_deployed, headers=[(CAPABILITY_HEADER.encode(), b"forged")])
    await middleware(scope, receive, send)
    assert sent[0]["status"] == 403 and b"native_test_designation_refused" in sent[1]["body"] and downstream_calls == [path]

    # A registry IS bound but could not be built: fail closed, as before.
    bound_but_unbuilt = NativeTestRuntime(None, None)
    middleware, scope, sent, send = call(bound_but_unbuilt)
    await middleware(scope, receive, send)
    assert sent[0]["status"] == 403 and b"native_test_registry_unavailable" in sent[1]["body"] and downstream_calls == [path]
