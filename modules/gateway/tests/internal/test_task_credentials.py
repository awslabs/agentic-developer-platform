"""Source-session scope and Kubernetes isolation before credential delivery."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.internal.task_credentials import issue_task_session, task_session_policy, verify_source_role_isolation

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/existing-worker"
CUSTOMER = "arn:aws:iam::222222222222:role/customer-deploy"


def missing_entry():
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "absent"}}, "DescribeAccessEntry")


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setenv("AGENT_TASK_SOURCE_ISOLATION_CONFIRMED", "true")
    monkeypatch.setenv("AGENT_TASK_SOURCE_ROLE_ARN", ROLE)
    monkeypatch.setenv("AGENT_TASK_SOURCE_EKS_CLUSTER", "platform-cluster")
    monkeypatch.setattr("src.internal.task_credentials.get_settings", lambda: SimpleNamespace(aws_region="us-east-1"))
    sts, eks = MagicMock(), MagicMock()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    eks.describe_cluster.return_value = {"cluster": {"accessConfig": {"authenticationMode": "API_AND_CONFIG_MAP"}}}
    eks.describe_access_entry.side_effect = missing_entry()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "test-source-key",
            "SecretAccessKey": "test-source-secret",
            "SessionToken": "test-source-token",
            "Expiration": datetime.now(UTC) + timedelta(minutes=30),
        }
    }
    monkeypatch.setattr("src.internal.task_credentials._aws_auth_config", lambda *_: {})
    monkeypatch.setattr("src.internal.task_credentials.boto3.client", lambda service, **_: {"sts": sts, "eks": eks}[service])
    return sts, eks


def test_existing_source_principal_is_retained_without_platform_permissions(source):
    sts, _ = source
    result = issue_task_session(invocation_id="run-1", not_after=datetime.now(UTC) + timedelta(hours=1))
    call = sts.assume_role.call_args.kwargs
    assert call["RoleArn"] == ROLE
    assert 900 <= call["DurationSeconds"] < 3600
    assert json.loads(call["Policy"]) == task_session_policy("aws", ACCOUNT)
    assert result["Version"] == 1 and result["AccessKeyId"] == "test-source-key"
    # These are source permissions only. Destination AssumeRole is still made by
    # the customer's own SDK, which controls ExternalId/tags/target session options.
    assert set(call) == {"RoleArn", "RoleSessionName", "DurationSeconds", "Policy"}


def test_named_target_scope_has_explicit_denial_outside_the_approved_roles():
    document = task_session_policy("aws", ACCOUNT, [CUSTOMER])
    assert document["Statement"][0]["Resource"] == [CUSTOMER]
    assert document["Statement"][-1] == {
        "Effect": "Deny",
        "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
        "NotResource": [CUSTOMER],
    }
    with pytest.raises(ValueError):
        task_session_policy("aws", ACCOUNT, [])
    with pytest.raises(ValueError):
        task_session_policy("aws", ACCOUNT, [ROLE])


@pytest.mark.parametrize("problem", ["eks-entry", "role-mapping", "account-mapping", "malformed", "unavailable", "wrong-account", "short-grant"])
def test_unsafe_or_unknown_source_identity_never_issues(source, monkeypatch, problem):
    sts, eks = source
    expiry = datetime.now(UTC) + timedelta(hours=1)
    if problem == "eks-entry":
        eks.describe_access_entry.side_effect = None
        eks.describe_access_entry.return_value = {"accessEntry": {"principalArn": ROLE}}
    elif problem == "role-mapping":
        monkeypatch.setattr(
            "src.internal.task_credentials._aws_auth_config", lambda *_: {"mapRoles": json.dumps([{"rolearn": ROLE, "groups": ["system:masters"]}])}
        )
    elif problem == "account-mapping":
        monkeypatch.setattr("src.internal.task_credentials._aws_auth_config", lambda *_: {"mapAccounts": json.dumps([ACCOUNT])})
    elif problem == "malformed":
        monkeypatch.setattr("src.internal.task_credentials._aws_auth_config", lambda *_: {"mapRoles": "{}"})
    elif problem == "unavailable":
        eks.describe_access_entry.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeAccessEntry")
    elif problem == "wrong-account":
        sts.get_caller_identity.return_value = {"Account": "999999999999"}
    else:
        expiry = datetime.now(UTC) + timedelta(minutes=10)
    with pytest.raises((ValueError, ClientError)):
        issue_task_session(invocation_id="run", not_after=expiry)
    sts.assume_role.assert_not_called()


def test_source_role_eks_access_is_rechecked_before_every_refresh(source):
    sts, eks = source
    expiry = datetime.now(UTC) + timedelta(hours=1)
    issue_task_session(invocation_id="run", not_after=expiry)
    eks.describe_access_entry.side_effect = None
    with pytest.raises(ValueError, match="EKS access"):
        issue_task_session(invocation_id="run", not_after=expiry)
    sts.assume_role.assert_called_once()


@pytest.mark.parametrize("mode", ["API", "API_AND_CONFIG_MAP", "CONFIG_MAP"])
def test_isolation_checks_both_supported_kubernetes_authentication_sources(source, mode):
    _, eks = source
    eks.describe_cluster.return_value["cluster"]["accessConfig"]["authenticationMode"] = mode
    with patch("src.internal.task_credentials._aws_auth_config", return_value={"mapRoles": json.dumps([{"rolearn": CUSTOMER}])}) as mapping:
        verify_source_role_isolation(eks, cluster="platform-cluster", role_arn=ROLE, account=ACCOUNT)
        assert mapping.call_count == (0 if mode == "API" else 1)
        assert eks.describe_access_entry.call_count == (0 if mode == "CONFIG_MAP" else 1)


def test_provider_lifetime_cannot_exceed_the_grant(source):
    sts, _ = source
    sts.assume_role.return_value["Credentials"]["Expiration"] = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(ValueError, match="exceeds"):
        issue_task_session(invocation_id="run", not_after=datetime.now(UTC) + timedelta(hours=1))


@pytest.mark.parametrize("mapping", [{"mapRoles": "[{}]"}, {"mapUsers": "[{}]"}, {"mapAccounts": "[{}]"}, {"mapAccounts": "[true]"}])
def test_malformed_mapping_entries_refuse_source_delivery(source, monkeypatch, mapping):
    sts, _ = source
    monkeypatch.setattr("src.internal.task_credentials._aws_auth_config", lambda *_: mapping)
    with pytest.raises(ValueError):
        issue_task_session(invocation_id="run", not_after=datetime.now(UTC) + timedelta(hours=1))
    sts.assume_role.assert_not_called()


def test_role_paths_cannot_hide_legacy_kubernetes_mapping(source, monkeypatch):
    sts, _ = source
    monkeypatch.setenv("AGENT_TASK_SOURCE_ROLE_ARN", ROLE.replace(":role/", ":role/path/"))
    monkeypatch.setattr("src.internal.task_credentials._aws_auth_config", lambda *_: {"mapRoles": json.dumps([{"rolearn": ROLE}])})
    with pytest.raises(ValueError, match="EKS access"):
        issue_task_session(invocation_id="run", not_after=datetime.now(UTC) + timedelta(hours=1))
    sts.assume_role.assert_not_called()


def test_no_source_session_until_platform_isolation_rollout_is_confirmed(source, monkeypatch):
    sts, eks = source
    monkeypatch.delenv("AGENT_TASK_SOURCE_ISOLATION_CONFIRMED")
    with pytest.raises(ValueError, match="not configured"):
        issue_task_session(invocation_id="run", not_after=datetime.now(UTC) + timedelta(hours=1))
    sts.assume_role.assert_not_called()
    eks.describe_cluster.assert_not_called()
