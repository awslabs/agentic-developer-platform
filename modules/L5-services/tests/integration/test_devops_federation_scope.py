"""A native UI handoff cannot inherit broad launcher-role authority."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from l5_services.adapters.devops import federation
from test_devops_federation import NOW, OPERATOR, federation_fixture  # noqa: F401


@pytest.mark.asyncio
async def test_session_policy_confines_even_a_broad_launcher_to_one_space_and_expiry(federation_fixture):  # noqa: F811
    inputs, calls, requests = federation_fixture
    expires_at = NOW + timedelta(minutes=3)
    try:
        result = await federation.mint_browser_launch(**inputs, user_id="owner", expires_at=expires_at)
    finally:
        await inputs["federation_client"].aclose()
    assert len(calls) == len(requests) == 1
    policy = json.loads(calls[0]["Policy"])
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert set(statement["Action"]) == {
        "aidevops:CreateOneTimeLoginSession", "aidevops:GetAgentSpace", "aidevops:GetOperatorApp",
    }
    assert statement["Resource"] == "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one"
    assert "*" not in json.dumps(policy)
    assert statement["Condition"] == {
        "StringEquals": {"aws:RequestedRegion": "us-east-1"},
        "DateLessThan": {"aws:CurrentTime": "2026-01-01T00:03:00Z"},
    }
    assert calls[0]["DurationSeconds"] == 900  # STS minimum; the policy expires earlier.
    assert result["expires_at"] == "2026-01-01T00:03:00Z"
    inputs["operator_client"].get_agent_space.assert_called_once_with(agentSpaceId="space-one")
    inputs["operator_client"].get_operator_app.assert_called_once_with(agentSpaceId="space-one")


@pytest.mark.asyncio
@pytest.mark.parametrize("app", [
    {},
    {"operatorAppUrl": "https://space-one.aidevops.global.app.aws/", "idc": {"operatorAppRoleArn": OPERATOR}},
    {"operatorAppUrl": "https://space-one.aidevops.global.app.aws/", "iam": {"operatorAppRoleArn": "arn:aws:iam::123456789012:role/Other"}},
    *[{"operatorAppUrl": url, "iam": {"operatorAppRoleArn": OPERATOR}} for url in (
        "https://space-two.aidevops.global.app.aws/",
        "https://space-one.aidevops.global.app.aws.evil.example/",
        "https://space-one.aidevops.global.app.aws@evil.example/",
        "http://space-one.aidevops.global.app.aws/",
        "https://space-one.aidevops.global.app.aws:443/",
        "https://space-one.aidevops.global.app.aws/?redirect=elsewhere",
        "https://space-one.aidevops.global.app.aws/#elsewhere",
        "https://space-one.aidevops.global.app.aws/other-space",
    )],
])
async def test_changed_native_app_cannot_obtain_credentials_or_federation(federation_fixture, app):  # noqa: F811
    inputs, calls, requests = federation_fixture
    inputs["operator_client"].get_operator_app.return_value = app
    try:
        with pytest.raises(HTTPException) as denied:
            await federation.mint_browser_launch(**inputs, user_id="owner")
    finally:
        await inputs["federation_client"].aclose()
    assert denied.value.detail["code"] == "native_operator_app_unavailable"
    assert calls == requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign_space", "provider_error"])
async def test_provider_readback_failure_is_closed_and_redacted(federation_fixture, failure):  # noqa: F811
    inputs, calls, requests = federation_fixture
    if failure == "foreign_space":
        inputs["operator_client"].get_agent_space.return_value = {"agentSpace": {"agentSpaceId": "space-two"}}
    else:
        inputs["operator_client"].get_operator_app.side_effect = RuntimeError("private-provider-response-token")
    try:
        with pytest.raises(HTTPException) as denied:
            await federation.mint_browser_launch(**inputs, user_id="owner")
    finally:
        await inputs["federation_client"].aclose()
    assert "private-provider-response-token" not in str(denied.value.detail)
    assert denied.value.detail["code"] == "native_operator_app_unavailable"
    assert calls == requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [NOW, NOW - timedelta(seconds=1), NOW.replace(tzinfo=None), "not-a-time"])
async def test_invalid_or_expired_authority_never_reads_or_assumes(federation_fixture, expires_at):  # noqa: F811
    inputs, calls, requests = federation_fixture
    try:
        with pytest.raises(HTTPException) as denied:
            await federation.mint_browser_launch(**inputs, user_id="owner", expires_at=expires_at)
    finally:
        await inputs["federation_client"].aclose()
    assert denied.value.detail["code"] == "launch_configuration_unavailable"
    assert calls == requests == []
    inputs["operator_client"].get_agent_space.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime", [-1, 120, 3600])
async def test_response_expiry_is_bounded_by_credentials_and_fifteen_minutes(federation_fixture, lifetime):  # noqa: F811
    inputs, calls, requests = federation_fixture
    original = inputs["sts_client"].assume_role.side_effect

    def assume(**kwargs):
        response = original(**kwargs)
        response["Credentials"]["Expiration"] = NOW + timedelta(seconds=lifetime)
        return response

    inputs["sts_client"].assume_role = Mock(side_effect=assume)
    try:
        if lifetime < 0:
            with pytest.raises(HTTPException) as denied:
                await federation.mint_browser_launch(**inputs, user_id="owner", expires_at=NOW + timedelta(hours=1))
            assert denied.value.detail["code"] == "federation_unavailable"
            assert not requests
        else:
            result = await federation.mint_browser_launch(**inputs, user_id="owner", expires_at=NOW + timedelta(hours=1))
            expected = NOW + timedelta(seconds=min(lifetime, 900))
            assert result["expires_at"] == expected.isoformat().replace("+00:00", "Z")
    finally:
        await inputs["federation_client"].aclose()


@pytest.mark.asyncio
async def test_expiry_during_provider_readback_never_issues_launcher_credentials(federation_fixture, monkeypatch):  # noqa: F811
    inputs, calls, requests = federation_fixture
    clock = {"seconds": 0}
    monkeypatch.setattr(federation, "time", SimpleNamespace(monotonic=lambda: clock["seconds"]))
    app = inputs["operator_client"].get_operator_app.return_value

    def read(**_kwargs):
        clock["seconds"] = 61
        return app

    inputs["operator_client"].get_operator_app.side_effect = read
    try:
        with pytest.raises(HTTPException) as denied:
            await federation.mint_browser_launch(**inputs, user_id="owner", expires_at=NOW + timedelta(minutes=1))
    finally:
        await inputs["federation_client"].aclose()
    assert denied.value.detail["code"] == "federation_unavailable"
    assert calls == requests == []
