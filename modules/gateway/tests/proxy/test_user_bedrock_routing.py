"""Personal and hosted Claude/OpenAI calls use the same person's hierarchy."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import delete

from src.proxy.bedrock_enforcement import RoutingDecision, resolve_routing_decision
from src.proxy.bedrock_principal import BedrockRoutingIdentityError
from src.proxy.bedrock_routing import BedrockTarget, bedrock_routing_resolver
from src.proxy.bedrock_routing_errors import BedrockAccountUnavailableError
from src.proxy.bedrock_signing import bedrock_destination_signer
from src.proxy.mantle_service import MantlePassthroughService
from src.proxy.service import ProxyService, _current_agent_run_id
from src.shared.models.bedrock_routing import BedrockAccountMapping
from tests.proxy import test_bedrock_enforcement as routing_fixtures
from tests.proxy.conftest import MockBedrockClient
from tests.proxy.test_bedrock_enforcement import (
    CANONICAL_USER_ID,
    MAPPED_ACCOUNT,
    MODEL_ID,
    ORG_ID,
    PLATFORM_ACCOUNT,
    TEAM_ID,
    _context,
    _credentials,
    _destination,
    _patch_routing_session,
    _seed_enforced_org_mapping,
)

session_factory = routing_fixtures.session_factory
routing_environment = routing_fixtures.routing_environment


@pytest.fixture(autouse=True)
def reset_routing():
    bedrock_routing_resolver._mappings_exist_cache = None
    token = _current_agent_run_id.set(None)
    yield
    _current_agent_run_id.reset(token)
    bedrock_routing_resolver._mappings_exist_cache = None


def worker():
    return _context(
        user_id="scaledjob-worker",
        org_id="__platform__",
        team_id="__agents__",
        account_type="service",
        auth_source="iam",
        scope="internal",
        attributed_org_id="run-workspace",
        # Deliberately wrong: this attribution field must never choose credentials.
        attributed_user_id="forged-person",
    )


def run_row(**updates):
    return {
        "tenant_id": "run-workspace",
        "user_id": "another-executor",
        "root_human_id": CANONICAL_USER_ID,
        "is_human_rooted": True,
        "status": "in_progress",
        **updates,
    }


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("scopes,expected", [([], "platform"), (["org"], "org"), (["org", "team"], "team"), (["org", "team", "user"], "user")])
async def test_most_local_person_rule_governs_personal_and_cloud(session_factory, routing_environment, cloud, scopes, expected):
    await _seed_enforced_org_mapping(session_factory)
    async with session_factory() as db:
        if "org" not in scopes:
            await db.execute(delete(BedrockAccountMapping))
        for scope in scopes:
            if scope == "org":
                continue
            destination = _destination(f"destination-{scope}", "222222222222" if scope == "team" else "333333333333")
            db.add(destination)
            db.add(
                BedrockAccountMapping(
                    scope_type=scope,
                    scope_id_org=ORG_ID if scope == "team" else None,
                    scope_id_team=TEAM_ID if scope == "team" else None,
                    scope_id_user=CANONICAL_USER_ID if scope == "user" else None,
                    destination_id=destination.id,
                    authored_by_user_id="admin",
                )
            )
        await db.commit()
    # A repo/workspace context must not replace the person's registered hierarchy.
    context = worker() if cloud else _context(org_id="run-workspace", team_id="other-team")
    before = context.model_dump()
    signer = AsyncMock(return_value=_credentials())
    registry = AsyncMock(return_value=run_row())
    with (
        _patch_routing_session(session_factory),
        patch.object(bedrock_destination_signer, "get_credentials", signer),
        patch("src.proxy.bedrock_principal.read_routing_run", registry),
    ):
        result = await resolve_routing_decision(context, agent_run_id="run-capability")
    assert result.target.rung == expected
    assert result.target.account_id == {"platform": PLATFORM_ACCOUNT, "org": MAPPED_ACCOUNT, "team": "222222222222", "user": "333333333333"}[expected]
    assert context.model_dump() == before
    assert registry.await_count == int(cloud)
    if expected != "platform":
        assert signer.call_args.kwargs["user_id"] == CANONICAL_USER_ID
    else:
        signer.assert_not_called()


@pytest.mark.parametrize(
    "row,reason",
    [
        (None, "unknown_run"),
        (run_row(tenant_id="other-tenant"), "tenant_mismatch"),
        (run_row(status="complete"), "terminal_run"),
        (run_row(status=""), "missing_run_status"),
        (run_row(root_human_id=""), "missing_run_owner"),
        (run_row(root_human_id="missing-user"), "unknown_run_owner"),
        (run_row(is_human_rooted=None), "missing_run_owner"),
    ],
)
async def test_unverified_cloud_identity_never_selects_credentials(session_factory, routing_environment, row, reason):
    await _seed_enforced_org_mapping(session_factory)
    signer = AsyncMock()
    with (
        _patch_routing_session(session_factory),
        patch.object(bedrock_destination_signer, "get_credentials", signer),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=row)),
    ):
        with pytest.raises(BedrockRoutingIdentityError) as exc:
            await resolve_routing_decision(worker(), agent_run_id="asserted-run")
    assert exc.value.details["reason"] == reason
    signer.assert_not_called()


async def test_missing_run_and_registry_outage_fail_closed(session_factory):
    with _patch_routing_session(session_factory):
        with pytest.raises(BedrockRoutingIdentityError, match="verify") as exc:
            await resolve_routing_decision(worker())
        assert exc.value.details["reason"] == "missing_run_id"
        with patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(side_effect=RuntimeError("offline"))):
            with pytest.raises(BedrockRoutingIdentityError) as exc:
                await resolve_routing_decision(worker(), agent_run_id="run")
        assert exc.value.status_code == 503


async def test_service_root_and_unprivileged_agent_cannot_borrow_person_rules(session_factory, routing_environment):
    await _seed_enforced_org_mapping(session_factory)
    with (
        _patch_routing_session(session_factory),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=run_row(is_human_rooted=False))),
    ):
        result = await resolve_routing_decision(worker(), agent_run_id="service-run")
        assert result.target.is_platform
        context = worker().model_copy(update={"scope": "shared"})
        result = await resolve_routing_decision(context, agent_run_id="someone-elses-run")
        assert result.target.is_platform


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("profile,expected_profile", [("us", "eu"), ("global", "global")])
async def test_openai_signs_with_resolved_credentials_and_keeps_decision(
    session_factory, routing_environment, cloud, stream, profile, expected_profile
):
    await _seed_enforced_org_mapping(session_factory)
    seen = []

    async def transport(request):
        seen.append(request)
        response = {"usage": {"input_tokens": 10, "output_tokens": 2}}
        if stream:
            return httpx.Response(200, content=("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode())
        return httpx.Response(200, json=response)

    with (
        _patch_routing_session(session_factory),
        patch.object(bedrock_destination_signer, "get_credentials", AsyncMock(return_value=_credentials(region="eu-west-1"))),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=run_row())),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            platform_auth = MagicMock()
            service = MantlePassthroughService(
                platform_auth, "https://bedrock-runtime.us-east-1.amazonaws.com", inference_profile_prefix=profile, http_client=client
            )
            service._log_usage = AsyncMock()
            result = await service.create_response(
                b'{"model":"openai.gpt-6-astra"}', worker() if cloud else _context(), stream=stream, model="openai.gpt-6-astra", agent_run_id="run"
            )
            if stream:
                assert b"response.completed" in b"".join([chunk async for chunk in result])
    assert len(seen) == 1
    assert seen[0].url.host == "bedrock-runtime.eu-west-1.amazonaws.com"
    assert "Credential=ASIAROUTED/" in seen[0].headers["authorization"]
    assert "/eu-west-1/bedrock/aws4_request" in seen[0].headers["authorization"]
    assert json.loads(seen[0].content)["model"] == f"{expected_profile}.openai.gpt-6-astra"
    platform_auth.sign.assert_not_called()
    assert service._log_usage.call_args.kwargs["routing_decision"].target.account_id == MAPPED_ACCOUNT
    capture = service._log_usage.call_args.args[2]
    assert capture.routing.endpoint_region == "eu-west-1"


@pytest.mark.parametrize("stream", [False, True])
async def test_claude_cloud_passes_owner_credentials_to_bedrock_client(session_factory, routing_environment, stream):
    await _seed_enforced_org_mapping(session_factory)
    credentials = _credentials()
    pool = MagicMock(get_client=AsyncMock(return_value=MockBedrockClient()))
    service = ProxyService(pool)
    service._log_usage = AsyncMock()
    with (
        _patch_routing_session(session_factory),
        patch.object(bedrock_destination_signer, "get_credentials", AsyncMock(return_value=credentials)),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=run_row())),
    ):
        result = await service.invoke_model(MODEL_ID, {"messages": [], "max_tokens": 8}, worker(), stream=stream, agent_run_id="verified-run")
        if stream:
            assert [chunk async for chunk in result]
    pool.get_client.assert_awaited_once_with(credentials)
    assert service._log_usage.call_args.kwargs["routing_decision"].target.account_id == MAPPED_ACCOUNT


async def test_concurrent_openai_requests_do_not_share_signers_or_regions():
    gate = asyncio.Event()
    requests = []

    async def transport(request):
        requests.append(request)
        if len(requests) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})

    async def decide(context, **kwargs):
        region = "us-east-1" if context.user_id == "alice" else "eu-west-1"
        return RoutingDecision(BedrockTarget(account_id=context.user_id, rung="user", region=region), _credentials(context.user_id, region=region))

    with patch("src.proxy.mantle_service.resolve_routing_decision", decide):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            service = MantlePassthroughService(MagicMock(), "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
            service._log_usage = AsyncMock()
            await asyncio.gather(
                *(
                    service.create_response(b'{"model":"openai.gpt-6-astra"}', _context(user_id=name), stream=False, model="openai.gpt-6-astra")
                    for name in ["alice", "bob"]
                )
            )
    for request in requests:
        name = "alice" if request.url.host == "bedrock-runtime.us-east-1.amazonaws.com" else "bob"
        assert f"Credential={name}/" in request.headers["authorization"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cloud", [False, True])
async def test_openai_unavailable_destination_never_uses_platform(session_factory, routing_environment, stream, cloud):
    await _seed_enforced_org_mapping(session_factory)
    failure = BedrockAccountUnavailableError(reason="assume_role_failed", account_id=MAPPED_ACCOUNT, scope="org")
    platform_auth = MagicMock()
    transport = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = MantlePassthroughService(platform_auth, "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
        with (
            _patch_routing_session(session_factory),
            patch.object(bedrock_destination_signer, "get_credentials", AsyncMock(side_effect=failure)),
            patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=run_row())),
        ):
            with pytest.raises(BedrockAccountUnavailableError):
                await service.create_response(b"{}", worker() if cloud else _context(), stream=stream, model="openai.gpt-6-astra", agent_run_id="run")
    platform_auth.sign.assert_not_called()
    transport.assert_not_called()


async def test_routing_run_read_is_current_and_strongly_consistent():
    from src.budget.run_binding import RunBindingResolver

    table = MagicMock()
    table.query.return_value = {"Items": [run_row(status="complete")]}
    resolver = RunBindingResolver("runs", "us-east-1", table=table)
    resolver._cached_row = AsyncMock(return_value=run_row(status="in_progress"))
    assert (await resolver.read_current("run"))["status"] == "complete"
    assert table.query.call_args.kwargs["ConsistentRead"] is True
    resolver._cached_row.assert_not_called()


async def test_account_selector_uses_same_user_despite_workspace_switch(session_factory, routing_environment):
    from src.admin.bedrock_routing.self_routes import _caller_id

    await _seed_enforced_org_mapping(session_factory)
    async with session_factory() as db:
        user_id = await _caller_id(db, _context(org_id="another-workspace", team_id="another-team"))
    assert user_id == CANONICAL_USER_ID
