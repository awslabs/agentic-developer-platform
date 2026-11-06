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


async def test_read_only_recovery_policy_restricts_the_registered_role_session(target):
    restriction = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "eks:DescribeCluster",
                    "Resource": "arn:aws:eks:us-east-1:123456789012:cluster/cluster",
                }
            ],
        }
    )
    await target.resolver.resolve(
        object(),
        entry=target.entry,
        policy=target.policy,
        principal_user_id="human",
        execution_id="execution",
        session_policy=restriction,
    )
    assert target.assume.call_args.kwargs["session_policy"] == restriction
    assert target.assume.call_args.kwargs["external_id"] == "external"
    assert target.assume.call_args.kwargs["session_duration_seconds"] == 900


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


async def test_ec2_template_target_uses_selected_role_and_exact_provider_identity(target):
    template_id = "lt-0123456789abcdef0"
    target.entry.resource_kind = "ec2-launch-template"
    target.entry.resource_id = template_id
    ec2 = SimpleNamespace(
        describe_launch_templates=Mock(return_value={"LaunchTemplates": [{"LaunchTemplateId": template_id, "LatestVersionNumber": 4}]})
    )
    scoped = target.session_factory.return_value
    scoped.client = lambda name, **kwargs: {"sts": target.sts, "ec2": ec2}[name]
    inspect = Mock(return_value={"observed_version": 4})
    resolved = await target.resolver.resolve(
        object(),
        entry=target.entry,
        policy=target.policy,
        principal_user_id="human",
        execution_id="execution",
        inspect_target=inspect,
    )
    assert resolved.physical.resource_kind == "ec2-launch-template"
    assert resolved.physical.resource_id == template_id
    assert resolved.observation == {"observed_version": 4}
    inspect.assert_called_once_with(scoped, {"LaunchTemplateId": template_id, "LatestVersionNumber": 4}, None)
    ec2.describe_launch_templates.assert_called_once_with(LaunchTemplateIds=[template_id])
    target.eks.describe_cluster.assert_not_called()


@pytest.mark.parametrize("identity", ["different_id", "two_templates", "wrong_account"])
async def test_ec2_template_target_rejects_unverifiable_provider_readback(target, identity):
    template_id = "lt-0123456789abcdef0"
    target.entry.resource_kind = "ec2-launch-template"
    target.entry.resource_id = template_id
    ec2 = SimpleNamespace(describe_launch_templates=Mock(return_value={"LaunchTemplates": [{"LaunchTemplateId": template_id}]}))
    if identity == "different_id":
        ec2.describe_launch_templates.return_value["LaunchTemplates"][0]["LaunchTemplateId"] = "lt-11111111111111111"
    elif identity == "two_templates":
        ec2.describe_launch_templates.return_value["LaunchTemplates"].append({"LaunchTemplateId": template_id})
    else:
        target.sts.get_caller_identity.return_value = {"Account": "999999999999"}
    target.session_factory.return_value.client = lambda name, **kwargs: {"sts": target.sts, "ec2": ec2}[name]
    with pytest.raises(CycleBlockedError, match="deployment_target_identity_mismatch"):
        await resolve(target)
    target.eks.describe_cluster.assert_not_called()


async def test_ec2_template_target_rejects_unscoped_resource_name_before_role_assumption(target):
    target.entry.resource_kind = "ec2-launch-template"
    target.entry.resource_id = "*"
    with pytest.raises(CycleBlockedError, match="deployment_resource_boundary_unverifiable"):
        await resolve(target)
    target.assume.assert_not_called()


@pytest.mark.parametrize(
    "kind,resource,service,response",
    [
        (
            "agentcore-gateway",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example",
            "bedrock-agentcore-control",
            {"gatewayId": "example", "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example"},
        ),
        (
            "lambda-function",
            "arn:aws:lambda:us-east-1:123456789012:function:example-interceptor",
            "lambda",
            {"Configuration": {"FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:example-interceptor"}},
        ),
    ],
)
async def test_scoped_role_resolves_exact_tool_resource(target, kind, resource, service, response):
    target.entry.resource_kind, target.entry.resource_id = kind, resource
    provider = SimpleNamespace(**{"get_gateway" if service == "bedrock-agentcore-control" else "get_function": Mock(return_value=response)})
    target.session_factory.return_value.client = Mock(
        side_effect=lambda name, **kwargs: {
            "sts": target.sts,
            service: provider,
        }[name]
    )
    inspect = Mock(return_value=["observed"])
    result = await target.resolver.resolve(
        object(),
        entry=target.entry,
        policy=target.policy,
        principal_user_id="human",
        execution_id="execution",
        inspect_target=inspect,
    )
    assert (result.physical.resource_kind, result.physical.resource_id, result.observation) == (kind, resource, ["observed"])
    inspect.assert_called_once_with(target.session_factory.return_value, response, None)
    if kind == "agentcore-gateway":
        provider.get_gateway.assert_called_once_with(gatewayIdentifier="example")
    else:
        provider.get_function.assert_called_once_with(FunctionName=resource)


async def test_scoped_role_rejects_tool_resource_in_wrong_account(target):
    target.entry.resource_kind = "lambda-function"
    target.entry.resource_id = "arn:aws:lambda:us-east-1:999999999999:function:example-interceptor"
    with pytest.raises(CycleBlockedError, match="deployment_target_identity_mismatch"):
        await resolve(target)
    target.eks.describe_cluster.assert_not_called()


async def test_scoped_role_resolves_facade_deployment_with_cluster_and_namespace(target):
    target.entry.resource_kind = "eks-deployment"
    target.entry.resource_id = "cluster/namespace/tools-facade"
    inspect = Mock(return_value=["verified"])
    result = await target.resolver.resolve(
        object(),
        entry=target.entry,
        policy=target.policy,
        principal_user_id="human",
        execution_id="execution",
        inspect_target=inspect,
    )
    assert result.physical.resource_kind == "eks-deployment"
    assert result.physical.resource_id == target.entry.resource_id
    assert result.observation == ["verified"]
    inspect.assert_called_once_with(target.session_factory.return_value, target.eks.describe_cluster.return_value["cluster"], "namespace")


@pytest.mark.parametrize(
    "resource,adapter,allowed",
    [
        ("cluster/namespace/sample-api", "qualification-api-runtime-verification", True),
        ("cluster/namespace/sample-api", "gateway-health-verification", False),
        ("cluster/namespace/another/nested", "qualification-api-runtime-verification", False),
        ("cluster/namespace/UPPER", "qualification-api-runtime-verification", False),
    ],
)
async def test_qualification_api_target_requires_exact_approved_workload_boundary(target, resource, adapter, allowed):
    target.entry.resource_kind = "eks-deployment"
    target.entry.resource_id = resource
    target.entry.verification_adapter = adapter
    readback = Mock(return_value=["observed-pod"])
    if allowed:
        result = await target.resolver.resolve(
            object(), entry=target.entry, policy=target.policy, principal_user_id="human", execution_id="execution", inspect_target=readback
        )
        assert result.physical.resource_id == resource and result.observation == ["observed-pod"]
        readback.assert_called_once()
    else:
        with pytest.raises(CycleBlockedError, match="deployment_resource_boundary_unverifiable"):
            await target.resolver.resolve(
                object(), entry=target.entry, policy=target.policy, principal_user_id="human", execution_id="execution", inspect_target=readback
            )
        target.assume.assert_not_called()
        readback.assert_not_called()
