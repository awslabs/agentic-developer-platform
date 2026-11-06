from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException

from l5_services.adapters.devops.federation import mint_browser_launch
from l5_services.adapters.devops.spaces import SpaceSelection


SELECTION = SpaceSelection("tenant", "product", "project", "binding", "123456789012", "us-east-1")
LAUNCHER = "arn:aws:iam::123456789012:role/DevopsLauncher"
OPERATOR = "arn:aws:iam::123456789012:role/DevopsOperator"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def federation_fixture():
    calls = []

    def assume_role(**arguments):
        calls.append(arguments)
        return {"AssumedRoleUser": {"Arn": (
            "arn:aws:sts::123456789012:assumed-role/DevopsLauncher/" + arguments["RoleSessionName"]
        )}, "Credentials": {"AccessKeyId": "offline-id", "SecretAccessKey": "offline-key",
                            "SessionToken": "offline-session", "Expiration": NOW + timedelta(minutes=15)}}

    sts = Mock(assume_role=Mock(side_effect=assume_role))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"SigninToken": "offline-ticket"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    operator = Mock(
        get_agent_space=Mock(return_value={"agentSpace": {"agentSpaceId": "space-one"}}),
        get_operator_app=Mock(return_value={"operatorAppUrl": "https://space-one.aidevops.global.app.aws/",
                                           "iam": {"operatorAppRoleArn": OPERATOR, "createdAt": NOW}}),
    )
    inputs = dict(selection=SELECTION, native_space_id="space-one", launcher_role_arn=LAUNCHER,
                  operator_role_arn=OPERATOR, issuer="https://adp.example.invalid",
                  sts_client=sts, federation_client=client, operator_client=operator, now=NOW)
    return inputs, calls, requests


@pytest.mark.asyncio
async def test_two_members_retain_distinct_stable_native_attribution(federation_fixture):
    inputs, calls, requests = federation_fixture
    try:
        first = await mint_browser_launch(**inputs, user_id="alice")
        second = await mint_browser_launch(**inputs, user_id="bob")
        repeated = await mint_browser_launch(**inputs, user_id="alice")
    finally:
        await inputs["federation_client"].aclose()

    assert first == repeated
    assert first["binding_id"] == second["binding_id"] == "binding"
    assert first["expires_at"] == "2026-01-01T00:15:00Z"
    assert calls[0]["SourceIdentity"] == calls[2]["SourceIdentity"]
    assert calls[0]["SourceIdentity"] != calls[1]["SourceIdentity"]
    assert all(call["RoleArn"] == LAUNCHER and call["DurationSeconds"] == 900 for call in calls)
    assert all(call["RoleSessionName"] == call["SourceIdentity"] for call in calls)
    assert all(request.url.host == "signin.aws.amazon.com" for request in requests)
    assert all(b"offline-session" in request.content for request in requests)
    query = parse_qs(urlsplit(first["launch_url"]).query)
    assert query["Issuer"] == ["https://adp.example.invalid"]
    assert "/agent-spaces/space-one/launch" in query["Destination"][0]
    assert "offline-session" not in first["launch_url"]
    assert "offline-key" not in first["launch_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda inputs: inputs.update(operator_role_arn=LAUNCHER),
    lambda inputs: inputs.update(operator_role_arn="arn:aws:iam::999999999999:role/Foreign"),
    lambda inputs: inputs.update(launcher_role_arn="arn:aws:iam::999999999999:role/Foreign"),
    lambda inputs: inputs.update(native_space_id="space-one/foreign"),
    lambda inputs: inputs.update(issuer="https://adp.example.invalid/redirect"),
    lambda inputs: inputs.update(issuer="https://other@adp.example.invalid"),
    lambda inputs: inputs.update(selection=SpaceSelection("tenant", "product", "project", "binding", "bad", "us-east-1")),
    lambda inputs: inputs.update(user_id=""),
])
async def test_invalid_server_scope_never_obtains_provider_credentials(federation_fixture, change):
    inputs, calls, requests = federation_fixture
    change(inputs)
    inputs.setdefault("user_id", "alice")
    try:
        with pytest.raises(HTTPException) as denied:
            await mint_browser_launch(**inputs)
        assert denied.value.status_code == 503
        assert calls == requests == []
    finally:
        await inputs["federation_client"].aclose()


@pytest.mark.asyncio
async def test_provider_redirect_is_not_followed_or_exposed(federation_fixture):
    inputs, calls, requests = federation_fixture
    await inputs["federation_client"].aclose()
    inputs["federation_client"] = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://example.invalid/foreign"}),
    ))
    try:
        with pytest.raises(HTTPException) as denied:
            await mint_browser_launch(**inputs, user_id="alice")
        assert denied.value.detail == {"code": "federation_unavailable"}
        assert len(calls) == 1
    finally:
        await inputs["federation_client"].aclose()


@pytest.mark.asyncio
async def test_wrong_assumed_role_is_rejected_before_federation(federation_fixture):
    inputs, calls, requests = federation_fixture
    inputs["sts_client"].assume_role = Mock(return_value={
        "AssumedRoleUser": {"Arn": "arn:aws:sts::123456789012:assumed-role/Foreign/alice"},
        "Credentials": {"AccessKeyId": "offline-id", "SecretAccessKey": "offline-key", "SessionToken": "offline-session"},
    })
    try:
        with pytest.raises(HTTPException) as denied:
            await mint_browser_launch(**inputs, user_id="alice")
        assert denied.value.detail == {"code": "federation_unavailable"}
        assert not requests
    finally:
        await inputs["federation_client"].aclose()


@pytest.mark.asyncio
async def test_large_provider_response_is_not_returned(federation_fixture):
    inputs, calls, requests = federation_fixture
    await inputs["federation_client"].aclose()
    inputs["federation_client"] = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="x" * 5000),
    ))
    try:
        with pytest.raises(HTTPException) as denied:
            await mint_browser_launch(**inputs, user_id="alice")
        assert denied.value.detail == {"code": "federation_unavailable"}
    finally:
        await inputs["federation_client"].aclose()
