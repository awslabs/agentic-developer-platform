"""Registered credential selection and independent AWS target readback."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.orchestration.deployment_target import DeploymentTargetResolver
from src.orchestration.execution_policy import Action
from src.orchestration.review_cycle import CycleBlockedError


@pytest.fixture
def target(monkeypatch):
    credential = SimpleNamespace(id="connection", label="dev", credential_type="aws_role", service="aws", secret_arn="test-secret")
    resolve = AsyncMock(return_value=credential)
    monkeypatch.setattr("src.orchestration.deployment_target.resolve_user_credential", resolve)
    role = "arn:aws:iam::123456789012:role/deploy"
    secrets = SimpleNamespace(get_secret=Mock(return_value=json.dumps({"role_arn": role, "external_id": "external"})))
    temporary = SimpleNamespace(
        access_key_id="scoped-access",
        secret_access_key="scoped-secret",
        session_token="scoped-token",
        region="us-east-1",
        expiration=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    assume = Mock(return_value=temporary)
    sts = SimpleNamespace(get_caller_identity=Mock(return_value={"Account": "123456789012"}))
    eks = SimpleNamespace(
        describe_cluster=Mock(return_value={"cluster": {"name": "cluster", "arn": "arn:aws:eks:us-east-1:123456789012:cluster/cluster"}})
    )
    scoped = SimpleNamespace(client=lambda name, **kwargs: {"sts": sts, "eks": eks}[name])
    session_factory = Mock(return_value=scoped)
    return SimpleNamespace(
        resolver=DeploymentTargetResolver(secrets=secrets, assume=assume, session_factory=session_factory),
        entry=SimpleNamespace(connection_id="connection", resource_kind="eks-namespace", resource_id="cluster/namespace"),
        policy=SimpleNamespace(
            environment_connection_ids=["connection"],
            org_id="tenant",
            user_credentials=SimpleNamespace(actions=[Action.DEPLOY], vault_credential_ids=["connection"], aws_role_arns=[role]),
        ),
        resolve=resolve,
        credential=credential,
        assume=assume,
        session_factory=session_factory,
        sts=sts,
        eks=eks,
        temporary=temporary,
    )


async def resolve(ctx):
    return await ctx.resolver.resolve(object(), entry=ctx.entry, policy=ctx.policy, principal_user_id="human", execution_id="execution")


async def test_target_uses_vault_acl_and_tagged_scoped_credentials(target):
    result = await resolve(target)
    assert result.physical.account_id == "123456789012" and result.physical.resource_id == "cluster/namespace"
    assert target.resolve.await_args.kwargs == {"org_id": "tenant", "user_id": "human", "credential_id": "connection"}
    assert target.assume.call_args.kwargs["user_id"] == "human" and target.assume.call_args.kwargs["task_id"] == "execution"
    assert target.session_factory.call_args.kwargs["aws_access_key_id"] == "scoped-access"


@pytest.mark.parametrize("failure", ["not_allowed", "vault_acl", "wrong_type", "expired", "wrong_account", "wrong_cluster"])
async def test_unverifiable_connection_never_falls_back(target, failure):
    if failure == "not_allowed":
        target.policy.environment_connection_ids = []
    elif failure == "vault_acl":
        target.resolve.side_effect = CycleBlockedError("credential_not_authorized")
    elif failure == "wrong_type":
        target.credential.credential_type = "api_key"
    elif failure == "expired":
        target.temporary.expiration = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif failure == "wrong_account":
        target.sts.get_caller_identity.return_value = {"Account": "999999999999"}
    else:
        target.eks.describe_cluster.return_value["cluster"]["name"] = "other-cluster"
    with pytest.raises(CycleBlockedError):
        await resolve(target)
    assert target.session_factory.call_count <= 1


async def test_full_policy_denial_precedes_role_assumption_and_runtime_reads(target):
    authorize = Mock(side_effect=CycleBlockedError("deployment_runtime_authority_denied"))
    inspect = Mock()
    with pytest.raises(CycleBlockedError, match="authority_denied"):
        await target.resolver.resolve(
            object(),
            entry=target.entry,
            policy=target.policy,
            principal_user_id="human",
            execution_id="execution",
            authorize_scope=authorize,
            inspect_target=inspect,
        )
    authorize.assert_called_once_with("connection", "arn:aws:iam::123456789012:role/deploy")
    target.assume.assert_not_called()
    target.sts.get_caller_identity.assert_not_called()
    inspect.assert_not_called()
