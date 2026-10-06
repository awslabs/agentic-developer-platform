"""Native provider sessions verify real SDK caller identity and renew in memory."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
from botocore.stub import Stubber
import pytest

from harness_jobs.identity import (
    OperationRequest,
    OperationRefused,
    encode_payload,
    payload_digest,
)
from superplane_executor import provider_session

pytestmark = pytest.mark.asyncio

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/provider"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/provider/session"
USER_ID = "AROA" + "A" * 17 + ":session"


@pytest.fixture
def request_operation():
    request = OperationRequest(
        action="provision",
        idempotency_key="key",
        parameters={
            "credential_id": "credential",
            "credential_service": "aws",
            "credential_label": "label",
            "provider": "aws",
            "provider_account_id": ACCOUNT,
        },
    )
    return SimpleNamespace(
        request_payload=encode_payload(request),
        plan_digest=payload_digest(request),
        grant=SimpleNamespace(lease=SimpleNamespace(operation_id="operation")),
    )


@pytest.fixture
def aws_identity(monkeypatch):
    real_session = boto3.Session
    actual = {"Account": ACCOUNT, "Arn": ARN, "UserId": USER_ID}
    calls = []

    def session(**kwargs):
        if "botocore_session" in kwargs:
            return real_session(**kwargs)
        selected = real_session(**kwargs)
        client = selected.client("sts")
        stubber = Stubber(client)
        stubber.add_response("get_caller_identity", dict(actual))
        stubber.activate()
        calls.append(client)
        selected.client = lambda *args, **kw: client
        return selected

    monkeypatch.setattr(provider_session.boto3, "Session", session)
    return actual, calls


def response():
    return dict(
        version=1,
        operation_id="operation",
        credential_id="credential",
        role_arn=ROLE,
        account_id=ACCOUNT,
        region="us-east-1",
        assumed_role_arn=ARN,
        assumed_role_id=USER_ID,
        access_key_id="ASIA" + "A" * 16,
        secret_access_key="s" * 40,
        session_token="token",
        expiration=(datetime.now(UTC) + timedelta(seconds=890)).isoformat(),
        authority_expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        access_entry_arn=None,
    )


async def test_session_uses_sdk_identity_and_renews_via_paid_route(
    request_operation, aws_identity
):
    import asyncio

    calls = []

    async def post(path, body):
        assert path == "/internal/v1/controller-execution/provider-session"
        assert body == {
            "operation_id": "operation",
            "region": "us-east-1",
            "access_entry_arn": None,
        }
        calls.append(True)
        return response()

    session = await provider_session.session_for(
        SimpleNamespace(post=post), request_operation, "us-east-1"
    )
    credentials = session.get_credentials()
    credentials._expiry_time = datetime.now(UTC) - timedelta(seconds=1)
    refreshed = await asyncio.to_thread(credentials.get_frozen_credentials)
    assert refreshed.token == "token" and len(calls) == 2 and len(aws_identity[1]) == 2
    assert session._superplane_role_arn == ROLE


@pytest.mark.parametrize("field", ["Account", "Arn", "UserId"])
async def test_sdk_identity_drift_refuses(request_operation, aws_identity, field):
    aws_identity[0][field] = {
        "Account": "000000000000",
        "Arn": ARN.replace("provider", "other"),
        "UserId": "AROA" + "B" * 17 + ":session",
    }[field]

    async def post(*args):
        return response()

    with pytest.raises(OperationRefused):
        await provider_session.session_for(
            SimpleNamespace(post=post), request_operation, "us-east-1"
        )


async def test_scoped_session_cannot_replace_provider_role_id(
    request_operation, aws_identity
):
    import asyncio

    entry = "arn:aws:eks:us-east-1:123456789012:access-entry/workspace/role/id/installer/immutable"

    async def post(path, body):
        value = response()
        if body["access_entry_arn"] is not None:
            value["access_entry_arn"] = entry
            value["assumed_role_id"] = "AROA" + "B" * 17 + ":session"
            aws_identity[0]["UserId"] = value["assumed_role_id"]
        return value

    session = await provider_session.session_for(
        SimpleNamespace(post=post), request_operation, "us-east-1"
    )
    assert session._superplane_role_id == "AROA" + "A" * 17
    with pytest.raises(OperationRefused, match="scoped provider role changed"):
        await asyncio.to_thread(session._superplane_scoped_entry, entry)


async def test_actor_refresh_rechecks_shortened_current_authority(
    request_operation, aws_identity, monkeypatch
):
    import asyncio
    from workspace_provisioning.credentials import assume_session
    from workspace_provisioning.runtime_config import LifecycleRefused

    async def post(path, body):
        if path.endswith("provider-preflight"):
            return {
                "admits_work": True,
                "operation_id": "operation",
                "authority_expires_at": (
                    datetime.now(UTC) + timedelta(seconds=300)
                ).isoformat(),
            }
        return response()

    session = await provider_session.session_for(
        SimpleNamespace(post=post), request_operation, "us-east-1"
    )

    def no_assume(**kwargs):
        pytest.fail("shortened authority must refuse before actor STS")

    monkeypatch.setattr(
        session,
        "client",
        lambda *args, **kwargs: SimpleNamespace(assume_role=no_assume),
    )
    with pytest.raises(LifecycleRefused, match="shorter than the STS minimum"):
        await asyncio.to_thread(
            assume_session,
            session,
            role_arn="arn:aws:iam::123456789012:role/installer",
            region="us-east-1",
            verify=lambda: None,
        )
