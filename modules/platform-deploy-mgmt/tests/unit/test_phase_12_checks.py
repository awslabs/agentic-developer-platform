"""Unit tests for Phase 12 checks — the Superplane domain app.

Issue #5050 (U5), EPIC #4910.

Mocked boto3 throughout, following `test_phase_1_checks.py`. Two properties get more
attention than "PASS on a healthy account", because they are the ones that would make this
phase actively harmful:

  1. **Absence is a SKIP unless the operator asked for Superplane.** The domain app is
     optional. A phase that hard-failed on absence would report every gateway-only account
     as broken, which trains operators to ignore the phase.
  2. **Nothing writes, and nothing decrypts.** `customer_session` is read-only by contract.
     A check that reached for a write API, or read a parameter `WithDecryption=True`, would
     pass its own assertions while breaking that contract — so it is asserted directly on
     the recorded boto3 calls rather than assumed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from platform_deploy_mgmt.checks.boto_helpers import Context
from platform_deploy_mgmt.checks.phase_12 import (
    CHECKS,
    check_12_1_control_plane_role_exists,
    check_12_2_control_plane_trust_is_scoped,
    check_12_3_skypilot_role_exists,
    check_12_4_secret_access_is_resource_scoped,
    check_12_5_ecr_repositories_exist,
    check_12_6_ecr_tags_are_immutable,
    check_12_7_ssm_parameters_published,
    check_12_8_cors_allowlist_is_not_wildcard,
    check_12_9_secret_parameters_hold_names,
    check_12_10_repositories_are_tagged_for_teardown,
)
from platform_deploy_mgmt.checks.shape import Result, Severity

_ENV = "dev"
_PREFIX = f"adp-{_ENV}-superplane"
_PARAM_PREFIX = f"/adp/{_ENV}/superplane"
_OIDC_SUB = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub"
_OIDC_ARN = "arn:aws:iam::111111111111:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


class FakeClients:
    """One dispatcher standing in for every AWS client a check asks the session for.

    A single dispatcher rather than per-test wiring because several checks read more than
    one service (12.10 reads ECR twice through different APIs), and because it makes the
    read-only assertions below possible: every call any check makes is recorded in one
    place, so a write API is detectable without knowing which check would have made it.
    """

    def __init__(self):
        self.iam = MagicMock()
        self.iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        self.ecr = MagicMock()
        self.ssm = MagicMock()
        self.requested: list[str] = []

    def __call__(self, service_name: str, *args, **kwargs):
        self.requested.append(service_name)
        return {"iam": self.iam, "ecr": self.ecr, "ssm": self.ssm}[service_name]


@pytest.fixture
def clients() -> FakeClients:
    return FakeClients()


@pytest.fixture
def ctx(clients: FakeClients) -> Context:
    """A context whose customer session hands out the fakes above."""
    customer_session = MagicMock()
    customer_session.client.side_effect = clients
    return Context(
        customer_account_id="123456789012",
        region="us-east-1",
        environment=_ENV,
        customer_session=customer_session,
        platform_session=MagicMock(),
    )


@pytest.fixture(autouse=True)
def superplane_not_requested(monkeypatch):
    """Default every test to "the operator did not ask for Superplane".

    Absence then reads as SKIP unless a test opts in with `superplane_requested`, which is
    the same default an operator running a gateway-only deploy gets.
    """
    monkeypatch.delenv("SUPERPLANE_ENABLED", raising=False)


@pytest.fixture
def superplane_requested(monkeypatch):
    monkeypatch.setenv("SUPERPLANE_ENABLED", "true")


def _client_error(code: str, message: str = "Error") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "TestOperation")


def _scoped_trust(*subjects: str) -> dict:
    """A trust policy shaped like the one U3's irsa.tf produces."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {"Federated": _OIDC_ARN},
                "Condition": {
                    "StringEquals": {
                        _OIDC_SUB: list(subjects),
                        "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud": "sts.amazonaws.com",
                    }
                },
            }
        ],
    }


def _role(name: str, trust: dict | None = None) -> dict:
    return {
        "Role": {
            "RoleName": name,
            "RoleId": "AROAEXAMPLE",
            "Arn": f"arn:aws:iam::123456789012:role/{name}",
            "AssumeRolePolicyDocument": trust or _scoped_trust(f"system:serviceaccount:superplane:{name}"),
        }
    }


def _repository(name: str, *, immutable: bool = True, scan: bool = True) -> dict:
    return {
        "repositoryName": name,
        "repositoryArn": f"arn:aws:ecr:us-east-1:123456789012:repository/{name}",
        "imageTagMutability": "IMMUTABLE" if immutable else "MUTABLE",
        "imageScanningConfiguration": {"scanOnPush": scan},
    }


def _paginated_repositories(clients: FakeClients, repositories: list[dict]) -> None:
    paginator = MagicMock()
    paginator.paginate.return_value = [{"repositories": repositories}]
    clients.ecr.get_paginator.return_value = paginator


def _healthy_parameters() -> dict[str, str]:
    return {
        "control-plane-role-arn": f"arn:aws:iam::123456789012:role/{_PREFIX}-control-plane",
        "skypilot-role-arn": f"arn:aws:iam::123456789012:role/{_PREFIX}-skypilot-api",
        "namespace": "superplane",
        "skypilot-namespace": "skypilot",
        "aws-region": "us-east-1",
        "cors-allowed-origins": json.dumps(["https://superplane.example.com"]),
        "database-secret-name": "adp/dev/superplane/database",
        "jwt-secret-name": "adp/dev/superplane/jwt",
    }


def _paginated_parameters(clients: FakeClients, values: dict[str, str]) -> None:
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Parameters": [{"Name": f"{_PARAM_PREFIX}/{leaf}", "Value": value} for leaf, value in values.items()]}
    ]
    clients.ssm.get_paginator.return_value = paginator


# ---------------------------------------------------------------------------
# 12.1 / 12.3 — the roles exist
# ---------------------------------------------------------------------------


class TestRolesExist:
    """12.1 and 12.3: both IRSA roles U3 creates are present under the env-scoped prefix."""

    def test_control_plane_role_pass(self, ctx, clients):
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane")

        result = check_12_1_control_plane_role_exists(ctx)

        assert result.result == Result.PASS
        clients.iam.get_role.assert_called_once_with(RoleName=f"{_PREFIX}-control-plane")

    def test_role_name_is_environment_scoped(self, clients):
        """A staging run must not report on dev's role.

        The prefix carries the environment, so this is what stops a verification of one
        environment from silently passing on another's resources.
        """
        session = MagicMock()
        session.client.side_effect = clients
        staging = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="staging",
            customer_session=session,
            platform_session=MagicMock(),
        )
        clients.iam.get_role.return_value = _role("adp-staging-superplane-control-plane")

        check_12_1_control_plane_role_exists(staging)

        clients.iam.get_role.assert_called_once_with(RoleName="adp-staging-superplane-control-plane")

    def test_skypilot_role_pass(self, ctx, clients):
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-skypilot-api")

        result = check_12_3_skypilot_role_exists(ctx)

        assert result.result == Result.PASS
        assert "separate from" in result.detail

    def test_api_error_that_is_not_absence_fails_regardless_of_the_gate(self, ctx, clients):
        """AccessDenied is a broken verification, not an absent resource.

        Reporting it as SKIP would hide a credential problem behind "not deployed", which is
        the most misleading outcome this phase could produce.
        """
        clients.iam.get_role.side_effect = _client_error("AccessDenied", "nope")

        result = check_12_1_control_plane_role_exists(ctx)

        assert result.result == Result.FAIL
        assert "AccessDenied" in result.detail


class TestAbsenceRespectsTheDeployGate:
    """The property that keeps this phase usable on accounts without Superplane."""

    def test_missing_role_skips_when_superplane_not_requested(self, ctx, clients):
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")

        result = check_12_1_control_plane_role_exists(ctx)

        assert result.result == Result.SKIP
        assert result.evidence["superplane_expected"] is False

    def test_missing_role_fails_when_superplane_requested(self, ctx, clients, superplane_requested):
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")

        result = check_12_1_control_plane_role_exists(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.HARD
        assert result.evidence["superplane_expected"] is True

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_gate_accepts_the_shapes_deploy_all_accepts(self, ctx, clients, monkeypatch, value):
        monkeypatch.setenv("SUPERPLANE_ENABLED", value)
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")

        assert check_12_1_control_plane_role_exists(ctx).result == Result.FAIL

    @pytest.mark.parametrize("value", ["false", "", "0", "no"])
    def test_gate_rejects_non_affirmative_values(self, ctx, clients, monkeypatch, value):
        monkeypatch.setenv("SUPERPLANE_ENABLED", value)
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")

        assert check_12_1_control_plane_role_exists(ctx).result == Result.SKIP

    def test_every_absence_path_agrees_with_the_gate(self, ctx, clients, superplane_requested):
        """All ten checks, one empty account: every result is a HARD-or-SOFT FAIL, none PASS.

        A single check that reported PASS on an empty account would make the phase report a
        deployment that is not there — which is worse than a false failure, because nobody
        investigates a pass.
        """
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")
        clients.iam.list_role_policies.side_effect = _client_error("NoSuchEntity")
        _paginated_repositories(clients, [])
        _paginated_parameters(clients, {})

        results = [fn(ctx) for _, _, fn, _, _ in CHECKS]

        assert all(r.result == Result.FAIL for r in results), [
            (r.id, r.result.value) for r in results if r.result != Result.FAIL
        ]

    def test_empty_account_is_all_skips_when_superplane_was_not_requested(self, ctx, clients):
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")
        clients.iam.list_role_policies.side_effect = _client_error("NoSuchEntity")
        _paginated_repositories(clients, [])
        _paginated_parameters(clients, {})

        results = [fn(ctx) for _, _, fn, _, _ in CHECKS]

        assert all(r.result == Result.SKIP for r in results), [
            (r.id, r.result.value) for r in results if r.result != Result.SKIP
        ]


# ---------------------------------------------------------------------------
# 12.2 — the trust policy is scoped
# ---------------------------------------------------------------------------


class TestTrustPolicyIsScoped:
    """12.2: the role is assumable by named service accounts, not by any pod."""

    def test_pass_on_two_named_service_accounts(self, ctx, clients):
        clients.iam.get_role.return_value = _role(
            f"{_PREFIX}-control-plane",
            _scoped_trust(
                "system:serviceaccount:superplane:superplane-api",
                "system:serviceaccount:superplane:superplane-controller",
            ),
        )

        result = check_12_2_control_plane_trust_is_scoped(ctx)

        assert result.result == Result.PASS
        assert len(result.evidence["subjects"]) == 2

    def test_fail_when_federated_without_a_sub_condition(self, ctx, clients):
        """The failure this check exists for.

        A statement that federates to the cluster's OIDC provider with only an `aud`
        condition is assumable by EVERY pod in the cluster, including core ADP pods. The
        role exists, its name is right, and the platform-isolation property is gone.
        """
        wide_open = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Principal": {"Federated": _OIDC_ARN},
                    "Condition": {
                        "StringEquals": {"oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud": "sts.amazonaws.com"}
                    },
                }
            ],
        }
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane", wide_open)

        result = check_12_2_control_plane_trust_is_scoped(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.HARD
        assert result.evidence["unscoped_statements"] == [0]

    def test_fail_when_no_condition_block_at_all(self, ctx, clients):
        clients.iam.get_role.return_value = _role(
            f"{_PREFIX}-control-plane",
            {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Principal": {"Federated": _OIDC_ARN},
                    }
                ]
            },
        )

        result = check_12_2_control_plane_trust_is_scoped(ctx)

        assert result.result == Result.FAIL

    def test_fail_when_a_subject_is_not_a_service_account(self, ctx, clients):
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane", _scoped_trust("*"))

        result = check_12_2_control_plane_trust_is_scoped(ctx)

        assert result.result == Result.FAIL
        assert result.evidence["unscoped_statements"] == [0]

    def test_a_single_statement_dict_is_handled(self, ctx, clients):
        """IAM may present `Statement` as an object rather than a list.

        Iterating that object would walk its KEYS and find no subject, so a correctly
        scoped role would be reported as unscoped.
        """
        policy = _scoped_trust("system:serviceaccount:superplane:superplane-api")
        policy["Statement"] = policy["Statement"][0]
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane", policy)

        assert check_12_2_control_plane_trust_is_scoped(ctx).result == Result.PASS

    def test_a_json_string_trust_document_is_parsed(self, ctx, clients):
        """Some botocore paths return the document as a string rather than a dict."""
        policy = _scoped_trust("system:serviceaccount:superplane:superplane-api")
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane", policy)
        clients.iam.get_role.return_value["Role"]["AssumeRolePolicyDocument"] = json.dumps(policy)

        assert check_12_2_control_plane_trust_is_scoped(ctx).result == Result.PASS

    def test_a_single_string_sub_value_is_handled(self, ctx, clients):
        """`:sub` may be a bare string when there is one subject.

        Iterating a string yields characters, so this would otherwise report a scoped role
        as trusting dozens of non-service-account subjects.
        """
        policy = _scoped_trust("system:serviceaccount:superplane:superplane-api")
        policy["Statement"][0]["Condition"]["StringEquals"][_OIDC_SUB] = (
            "system:serviceaccount:superplane:superplane-api"
        )
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane", policy)

        result = check_12_2_control_plane_trust_is_scoped(ctx)

        assert result.result == Result.PASS
        assert result.evidence["subjects"] == ["system:serviceaccount:superplane:superplane-api"]


# ---------------------------------------------------------------------------
# 12.4 — secret access is resource-scoped
# ---------------------------------------------------------------------------


class TestSecretAccessIsScoped:
    """12.4: neither role can enumerate the account's secrets."""

    _SCOPED = {
        "Statement": [
            {
                "Sid": "ReadOwnSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/superplane/database-*"],
            }
        ]
    }

    def test_pass_when_both_roles_are_scoped(self, ctx, clients):
        clients.iam.list_role_policies.return_value = {"PolicyNames": ["p"]}
        clients.iam.get_role_policy.return_value = {"PolicyDocument": self._SCOPED}

        result = check_12_4_secret_access_is_resource_scoped(ctx)

        assert result.result == Result.PASS

    def test_fail_on_wildcard_secret_resource(self, ctx, clients):
        """The gateway's database and GitHub App credentials live in the same account.

        A `Resource: "*"` on a secretsmanager action is what turns a Superplane pod
        compromise into an account-wide credential read.
        """
        clients.iam.list_role_policies.return_value = {"PolicyNames": ["broad"]}
        clients.iam.get_role_policy.return_value = {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Sid": "TooBroad",
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": "*",
                    }
                ]
            }
        }

        result = check_12_4_secret_access_is_resource_scoped(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.HARD
        assert "TooBroad" in str(result.evidence["offending_statements"])

    def test_fail_on_action_wildcard_reaching_secrets(self, ctx, clients):
        """`Action: "*"` with `Resource: "*"` includes every secretsmanager call.

        Matching only on the `secretsmanager:` prefix would miss it, and this is the shape a
        hurried debugging policy actually takes.
        """
        clients.iam.list_role_policies.return_value = {"PolicyNames": ["admin"]}
        clients.iam.get_role_policy.return_value = {
            "PolicyDocument": {"Statement": [{"Sid": "All", "Effect": "Allow", "Action": "*", "Resource": "*"}]}
        }

        assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.FAIL

    def test_ecr_auth_wildcard_is_not_a_finding(self, ctx, clients):
        """`ecr:GetAuthorizationToken` cannot be resource-scoped by AWS.

        U3 documents that. Flagging it would make the check cry wolf on every correct
        deployment, and a check operators learn to ignore protects nothing.
        """
        clients.iam.list_role_policies.return_value = {"PolicyNames": ["p"]}
        clients.iam.get_role_policy.return_value = {
            "PolicyDocument": {
                "Statement": [
                    {"Sid": "EcrAuth", "Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"}
                ]
            }
        }

        assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.PASS

    def test_deny_statements_are_not_findings(self, ctx, clients):
        clients.iam.list_role_policies.return_value = {"PolicyNames": ["p"]}
        clients.iam.get_role_policy.return_value = {
            "PolicyDocument": {
                "Statement": [
                    {"Sid": "Guard", "Effect": "Deny", "Action": "secretsmanager:*", "Resource": "*"},
                ]
            }
        }

        assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.PASS

    def test_one_missing_role_still_checks_the_other(self, ctx, clients):
        """A partial deployment must not skip the policy check on the role that exists."""

        def list_policies(RoleName: str):
            if RoleName.endswith("-skypilot-api"):
                raise _client_error("NoSuchEntity")
            return {"PolicyNames": ["broad"]}

        clients.iam.list_role_policies.side_effect = list_policies
        clients.iam.get_role_policy.return_value = {
            "PolicyDocument": {
                "Statement": [{"Sid": "Bad", "Effect": "Allow", "Action": "secretsmanager:*", "Resource": "*"}]
            }
        }

        result = check_12_4_secret_access_is_resource_scoped(ctx)

        assert result.result == Result.FAIL
        assert "Bad" in str(result.evidence["offending_statements"])


# ---------------------------------------------------------------------------
# 12.5 / 12.6 / 12.10 — the ECR repositories
# ---------------------------------------------------------------------------


class TestEcrRepositories:
    """12.5, 12.6 and 12.10: the domain app's repositories and their configuration."""

    def test_pass_when_repositories_present(self, ctx, clients):
        _paginated_repositories(
            clients,
            [_repository("adp-superplane-api"), _repository("adp-superplane-controller")],
        )

        result = check_12_5_ecr_repositories_exist(ctx)

        assert result.result == Result.PASS
        assert result.evidence["repositories"] == ["adp-superplane-api", "adp-superplane-controller"]

    def test_other_repositories_in_the_account_are_ignored(self, ctx, clients):
        """Discovery is prefix-scoped, so the gateway's repositories are not mistaken for ours.

        This is the same ownership boundary `ecr.tf`'s foreign-repository precondition
        enforces at plan time, applied to what is actually in the account.
        """
        _paginated_repositories(
            clients,
            [_repository("adp-gateway"), _repository("adp-agent-runtime"), _repository("adp-superplane-api")],
        )

        result = check_12_5_ecr_repositories_exist(ctx)

        assert result.evidence["repositories"] == ["adp-superplane-api"]

    def test_only_foreign_repositories_reads_as_absence(self, ctx, clients, superplane_requested):
        _paginated_repositories(clients, [_repository("adp-gateway")])

        assert check_12_5_ecr_repositories_exist(ctx).result == Result.FAIL

    def test_pagination_is_followed(self, ctx, clients):
        """An account with enough repositories to paginate must not be read partially.

        Reading only the first page would report a missing repository on a healthy account —
        a false failure that is very hard to reproduce locally.
        """
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"repositories": [_repository("adp-superplane-api")]},
            {"repositories": [_repository("adp-superplane-controller")]},
            {"repositories": [_repository("adp-superplane-platform-monitor")]},
        ]
        clients.ecr.get_paginator.return_value = paginator

        result = check_12_5_ecr_repositories_exist(ctx)

        assert len(result.evidence["repositories"]) == 3

    def test_immutable_and_scanning_pass(self, ctx, clients):
        _paginated_repositories(clients, [_repository("adp-superplane-api")])

        assert check_12_6_ecr_tags_are_immutable(ctx).result == Result.PASS

    def test_mutable_tags_are_a_hard_failure(self, ctx, clients):
        """Mutable tags defeat digest pinning without breaking anything visible."""
        _paginated_repositories(
            clients,
            [_repository("adp-superplane-api", immutable=False), _repository("adp-superplane-controller")],
        )

        result = check_12_6_ecr_tags_are_immutable(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.HARD
        assert result.evidence["mutable"] == ["adp-superplane-api"]

    def test_missing_scan_on_push_is_soft(self, ctx, clients):
        """Weaker than mutable tags: it loses vulnerability signal, not image identity."""
        _paginated_repositories(clients, [_repository("adp-superplane-api", scan=False)])

        result = check_12_6_ecr_tags_are_immutable(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.SOFT

    def test_teardown_tag_present_passes(self, ctx, clients):
        _paginated_repositories(clients, [_repository("adp-superplane-api")])
        clients.ecr.list_tags_for_resource.return_value = {
            "tags": [{"Key": "DomainApp", "Value": "superplane"}, {"Key": "Project", "Value": "adp"}]
        }

        assert check_12_10_repositories_are_tagged_for_teardown(ctx).result == Result.PASS

    def test_untagged_repository_is_reported_as_a_teardown_orphan(self, ctx, clients):
        """The cyber-module failure, generalised: domain-owned but unidentifiable.

        A resource teardown cannot recognise as domain-owned is the resource that survives
        teardown, and only the account shows it — a plan cannot.
        """
        _paginated_repositories(clients, [_repository("adp-superplane-api")])
        clients.ecr.list_tags_for_resource.return_value = {"tags": [{"Key": "Project", "Value": "adp"}]}

        result = check_12_10_repositories_are_tagged_for_teardown(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.SOFT
        assert result.evidence["untagged"] == ["adp-superplane-api"]


# ---------------------------------------------------------------------------
# 12.7 / 12.8 / 12.9 — the published configuration
# ---------------------------------------------------------------------------


class TestPublishedConfiguration:
    """12.7, 12.8 and 12.9: what the rollout lane and the pods read."""

    def test_pass_when_every_required_parameter_is_published(self, ctx, clients):
        _paginated_parameters(clients, _healthy_parameters())

        assert check_12_7_ssm_parameters_published(ctx).result == Result.PASS

    def test_missing_skypilot_namespace_is_a_failure(self, ctx, clients):
        """A separate input from `namespace`, with a separate default.

        A rollout that derived one from the other renders service accounts that cannot
        assume their roles — which surfaces as opaque AWS 403s inside the pod rather than as
        a rollout failure, so it must be caught here.
        """
        parameters = _healthy_parameters()
        del parameters["skypilot-namespace"]
        _paginated_parameters(clients, parameters)

        result = check_12_7_ssm_parameters_published(ctx)

        assert result.result == Result.FAIL
        assert result.evidence["missing"] == ["skypilot-namespace"]

    def test_partial_configuration_is_a_failure_not_a_skip(self, ctx, clients):
        """Some parameters present means the module WAS deployed.

        So a gap is a real defect regardless of `SUPERPLANE_ENABLED` — the gate only
        excuses a module that is entirely absent.
        """
        _paginated_parameters(clients, {"namespace": "superplane"})

        result = check_12_7_ssm_parameters_published(ctx)

        assert result.result == Result.FAIL
        assert "superplane_expected" not in (result.evidence or {})

    def test_parameters_are_read_without_decryption(self, ctx, clients):
        """The read-only contract, at its sharpest point.

        Verification must not be able to decrypt a SecureString even if someone later
        writes one under this prefix.
        """
        _paginated_parameters(clients, _healthy_parameters())

        check_12_7_ssm_parameters_published(ctx)

        _, kwargs = clients.ssm.get_paginator.return_value.paginate.call_args
        assert kwargs["WithDecryption"] is False
        assert kwargs["Path"] == _PARAM_PREFIX
        assert kwargs["Recursive"] is True

    def test_explicit_cors_allowlist_passes(self, ctx, clients):
        _paginated_parameters(clients, _healthy_parameters())

        result = check_12_8_cors_allowlist_is_not_wildcard(ctx)

        assert result.result == Result.PASS
        assert result.evidence["origins"] == ["https://superplane.example.com"]

    def test_wildcard_cors_allowlist_is_a_hard_failure(self, ctx, clients):
        """Upstream's default. With credentials it makes any origin a session-bearing caller."""
        parameters = _healthy_parameters()
        parameters["cors-allowed-origins"] = json.dumps(["*"])
        _paginated_parameters(clients, parameters)

        result = check_12_8_cors_allowlist_is_not_wildcard(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.HARD

    def test_wildcard_among_valid_origins_is_still_a_failure(self, ctx, clients):
        """One '*' in the list makes the rest of the list decorative."""
        parameters = _healthy_parameters()
        parameters["cors-allowed-origins"] = json.dumps(["https://superplane.example.com", "*"])
        _paginated_parameters(clients, parameters)

        assert check_12_8_cors_allowlist_is_not_wildcard(ctx).result == Result.FAIL

    def test_bare_wildcard_string_is_caught(self, ctx, clients):
        """Not every writer JSON-encodes the value.

        A bare `*` must not slip through as unparseable-and-therefore-fine.
        """
        parameters = _healthy_parameters()
        parameters["cors-allowed-origins"] = "*"
        _paginated_parameters(clients, parameters)

        assert check_12_8_cors_allowlist_is_not_wildcard(ctx).result == Result.FAIL

    def test_empty_allowlist_is_soft(self, ctx, clients):
        """Fails closed rather than open — a misconfiguration, not a security hole."""
        parameters = _healthy_parameters()
        parameters["cors-allowed-origins"] = json.dumps([])
        _paginated_parameters(clients, parameters)

        result = check_12_8_cors_allowlist_is_not_wildcard(ctx)

        assert result.result == Result.FAIL
        assert result.severity == Severity.SOFT

    def test_secret_name_parameters_pass(self, ctx, clients):
        _paginated_parameters(clients, _healthy_parameters())

        assert check_12_9_secret_parameters_hold_names(ctx).result == Result.PASS

    @pytest.mark.parametrize(
        "value",
        [
            "postgres://user:hunter2@db.internal:5432/superplane",
            '{"username": "admin", "password": "x"}',
            "password=hunter2",
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
    )
    def test_material_shaped_values_are_reported(self, ctx, clients, value):
        """SSM is not a secret store: the value lands in Terraform state in plaintext."""
        parameters = _healthy_parameters()
        parameters["database-secret-name"] = value
        _paginated_parameters(clients, parameters)

        result = check_12_9_secret_parameters_hold_names(ctx)

        assert result.result == Result.FAIL
        assert result.evidence["suspect_parameters"] == ["database-secret-name"]

    def test_the_suspect_value_is_never_copied_into_the_evidence(self, ctx, clients):
        """Evidence is uploaded to S3.

        Reporting a leaked credential by republishing it into a second store would make the
        finding worse than the defect it reports.
        """
        secret = "postgres://user:hunter2@db.internal:5432/superplane"
        parameters = _healthy_parameters()
        parameters["database-secret-name"] = secret
        _paginated_parameters(clients, parameters)

        result = check_12_9_secret_parameters_hold_names(ctx)

        assert secret not in json.dumps(result.evidence)
        assert secret not in result.detail

    def test_a_secret_name_containing_slashes_is_not_flagged(self, ctx, clients):
        """`adp/dev/superplane/database` is the normal shape and must not read as a URL."""
        _paginated_parameters(clients, _healthy_parameters())

        assert check_12_9_secret_parameters_hold_names(ctx).result == Result.PASS


# ---------------------------------------------------------------------------
# Read-only contract and registry shape
# ---------------------------------------------------------------------------


class TestEveryCheckIsReadOnly:
    """`customer_session` is read-only by contract; asserted on the recorded calls.

    Documented in `boto_helpers` as "critical for security". A check that reached for a
    write API would still pass its own assertions, so the contract is asserted here rather
    than trusted.
    """

    def _run_all(self, ctx, clients) -> None:
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane")
        clients.iam.list_role_policies.return_value = {"PolicyNames": ["p"]}
        clients.iam.get_role_policy.return_value = {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Sid": "ReadOwnSecrets",
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/x-*"],
                    }
                ]
            }
        }
        _paginated_repositories(clients, [_repository("adp-superplane-api")])
        clients.ecr.list_tags_for_resource.return_value = {"tags": [{"Key": "DomainApp", "Value": "superplane"}]}
        _paginated_parameters(clients, _healthy_parameters())
        for _, _, fn, _, _ in CHECKS:
            fn(ctx)

    def test_no_check_calls_a_mutating_api(self, ctx, clients):
        forbidden = (
            "put_",
            "create_",
            "delete_",
            "update_",
            "attach_",
            "detach_",
            "tag_",
            "untag_",
            "set_",
            "batch_delete",
        )
        self._run_all(ctx, clients)

        called: list[str] = []
        for client in (clients.iam, clients.ecr, clients.ssm):
            called.extend(name for name, _, _ in client.mock_calls)
        offenders = [name for name in called if name.startswith(forbidden)]

        assert offenders == [], f"A phase-12 check called a mutating API on the customer session: {offenders}"

    def test_no_check_reads_secret_values(self, ctx, clients):
        """Reading a secret's material is not needed to verify that its NAME is published.

        Doing it anyway would put credential material into a process whose output is
        uploaded to S3.
        """
        self._run_all(ctx, clients)

        assert "secretsmanager" not in clients.requested

    def test_no_check_touches_the_platform_session(self, ctx, clients):
        """Only the runner writes evidence.

        A check that reached for `platform_session` would be writing to the platform account
        from inside a customer-account read.
        """
        self._run_all(ctx, clients)

        assert ctx.platform_session.client.call_args_list == []
        assert ctx.platform_session.resource.call_args_list == []


class TestChecksRegistry:
    """The tuple shape the runner iterates, and the ids it reports."""

    def test_ids_are_unique(self):
        ids = [c[0] for c in CHECKS]
        assert len(ids) == len(set(ids))

    def test_every_id_is_in_phase_12(self):
        """A mislabelled id would file evidence under the wrong phase."""
        assert all(c[0].startswith("12.") for c in CHECKS)

    def test_ids_are_sequential(self):
        assert [c[0] for c in CHECKS] == [f"12.{n}" for n in range(1, len(CHECKS) + 1)]

    def test_every_entry_has_the_runner_tuple_shape(self):
        """The runner unpacks five fields; a short tuple fails the whole phase at runtime."""
        from platform_deploy_mgmt.checks.shape import CostClass

        for entry in CHECKS:
            check_id, name, fn, severity, cost_class = entry
            assert isinstance(check_id, str) and check_id
            assert isinstance(name, str) and name
            assert callable(fn)
            assert isinstance(severity, Severity)
            assert isinstance(cost_class, CostClass)

    def test_registered_severity_matches_what_each_check_returns_on_a_healthy_account(self, ctx, clients):
        """The registry's severity is what the runner reports if a check RAISES.

        If they disagree, an exception in a HARD check could be reported as a SOFT warning
        and the phase would exit 0 on a broken deployment.
        """
        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane")
        clients.iam.list_role_policies.return_value = {"PolicyNames": []}
        _paginated_repositories(clients, [_repository("adp-superplane-api")])
        clients.ecr.list_tags_for_resource.return_value = {"tags": [{"Key": "DomainApp", "Value": "superplane"}]}
        _paginated_parameters(clients, _healthy_parameters())

        for check_id, _, fn, severity, _ in CHECKS:
            result = fn(ctx)
            assert result.severity == severity, (
                f"{check_id}: registry says {severity}, check returned {result.severity}"
            )
            assert result.id == check_id

    def test_names_match_the_registry(self, ctx, clients):
        """The registry name is printed before the check runs; a mismatch misleads a reader."""
        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")
        clients.iam.list_role_policies.side_effect = _client_error("NoSuchEntity")
        _paginated_repositories(clients, [])
        _paginated_parameters(clients, {})

        for check_id, name, fn, _, _ in CHECKS:
            assert fn(ctx).name == name, f"{check_id} returns a different name than the registry declares"


class TestPhaseRegistration:
    """The `PHASE_REGISTRY` entry — U5's only shared edit in this module."""

    def test_phase_12_resolves_to_this_module(self):
        from platform_deploy_mgmt.checks.runner import PHASE_REGISTRY

        module_name, display_name = PHASE_REGISTRY[12]
        assert module_name == "platform_deploy_mgmt.checks.phase_12"
        assert "Superplane" in display_name

    def test_phase_1_still_resolves(self):
        """Regression guard on the shared edit.

        `PHASE_REGISTRY` is edited by several units. This asserts the pre-existing entry
        survived, which is the failure a careless merge produces.
        """
        from platform_deploy_mgmt.checks.runner import PHASE_REGISTRY

        assert PHASE_REGISTRY[1] == ("platform_deploy_mgmt.checks.phase_1", "Bootstrap state backend")

    def test_every_registered_module_is_importable_and_exposes_checks(self):
        """The runner does `import_module(...).CHECKS`.

        A typo in the registry is a phase that fails at runtime rather than at import, so it
        is checked here for every entry, not just phase 12.
        """
        import importlib

        from platform_deploy_mgmt.checks.runner import PHASE_REGISTRY

        for phase, (module_name, _) in PHASE_REGISTRY.items():
            module = importlib.import_module(module_name)
            assert hasattr(module, "CHECKS"), f"Phase {phase} module {module_name} has no CHECKS"
            assert module.CHECKS, f"Phase {phase} registers an empty CHECKS list"

    def test_phase_number_matches_the_deploy_step_it_verifies(self):
        """Phase 12 verifies `deploy-all.sh`'s "Step 12/12: Superplane".

        The correspondence is the whole reason the number is 12 and not 2, and it is only
        useful if it stays true — an operator reading a deploy log maps one to the other.
        """
        from pathlib import Path

        # tests/unit[0] tests[1] platform-deploy-mgmt[2] modules[3] root[4]
        repo_root = Path(__file__).resolve().parents[4]
        deploy_all = (repo_root / "platform" / "scripts" / "deploy-all.sh").read_text(encoding="utf-8")

        assert "Step 12/12: Deploy superplane domain app" in deploy_all

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_the_runner_can_execute_phase_12_end_to_end(self, _summary, _ddb, _s3, ctx, clients):
        """Registry entry plus CHECKS actually run through `run_phase`.

        Unit-testing the checks and the registry separately would not catch a tuple shape
        the runner cannot unpack.
        """
        from platform_deploy_mgmt.checks.runner import run_phase

        clients.iam.get_role.return_value = _role(f"{_PREFIX}-control-plane")
        clients.iam.list_role_policies.return_value = {"PolicyNames": []}
        _paginated_repositories(clients, [_repository("adp-superplane-api")])
        clients.ecr.list_tags_for_resource.return_value = {"tags": [{"Key": "DomainApp", "Value": "superplane"}]}
        _paginated_parameters(clients, _healthy_parameters())

        assert run_phase(12, ctx) == 0

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_the_runner_exits_nonzero_when_a_hard_phase_12_check_fails(
        self, _summary, _ddb, _s3, ctx, clients, superplane_requested
    ):
        """The gate's other side: an operator who asked for Superplane gets a failing phase."""
        from platform_deploy_mgmt.checks.runner import run_phase

        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")
        clients.iam.list_role_policies.side_effect = _client_error("NoSuchEntity")
        _paginated_repositories(clients, [])
        _paginated_parameters(clients, {})

        assert run_phase(12, ctx) == 1

    @patch("platform_deploy_mgmt.checks.runner._upload_evidence")
    @patch("platform_deploy_mgmt.checks.runner._update_deployment_status")
    @patch("platform_deploy_mgmt.checks.runner._write_step_summary")
    def test_an_absent_optional_module_does_not_fail_the_phase(self, _summary, _ddb, _s3, ctx, clients):
        """A gateway-only account passes phase 12 with skips.

        This is the outcome that decides whether the phase is safe to include in
        `phase: all`.
        """
        from platform_deploy_mgmt.checks.runner import run_phase

        clients.iam.get_role.side_effect = _client_error("NoSuchEntity")
        clients.iam.list_role_policies.side_effect = _client_error("NoSuchEntity")
        _paginated_repositories(clients, [])
        _paginated_parameters(clients, {})

        assert run_phase(12, ctx) == 0


@pytest.mark.parametrize(
    "operator,subject",
    [
        ("StringLike", "system:serviceaccount:*:*"),
        ("StringNotEquals", "system:serviceaccount:superplane:api"),
        ("StringEqualsIfExists", "system:serviceaccount:superplane:api"),
        ("StringEquals", "system:serviceaccount:superplane:*"),
    ],
)
def test_broad_or_inverted_subject_never_passes(ctx, clients, operator, subject):
    trust = _scoped_trust(subject)
    trust["Statement"][0]["Condition"] = {operator: {_OIDC_SUB: subject}}
    clients.iam.get_role.return_value = _role("control-plane", trust)
    assert check_12_2_control_plane_trust_is_scoped(ctx).result == Result.FAIL


def test_an_additional_non_oidc_allow_does_not_pass(ctx, clients):
    trust = _scoped_trust("system:serviceaccount:superplane:api")
    trust["Statement"].append({"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"})
    clients.iam.get_role.return_value = _role("control-plane", trust)
    assert check_12_2_control_plane_trust_is_scoped(ctx).result == Result.FAIL


@pytest.mark.parametrize(
    "statement",
    [
        {"Action": "secretsmanager:GetSecretValue", "Resource": "arn:aws:secretsmanager:*:*:secret:*"},
        {"Action": "SECRETSMANAGER:GetSecretValue", "Resource": "*"},
        {"Action": "secret*:*", "Resource": "*"},
        {"NotAction": "s3:*", "Resource": "*"},
        {"Action": "secretsmanager:GetSecretValue", "NotResource": "some-secret"},
    ],
)
def test_broad_secret_grant_forms_fail(ctx, clients, statement):
    clients.iam.list_role_policies.return_value = {"PolicyNames": ["drift"]}
    clients.iam.get_role_policy.return_value = {"PolicyDocument": {"Statement": [{"Effect": "Allow", **statement}]}}
    assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.FAIL


def test_managed_policy_second_page_is_inspected(ctx, clients):
    clients.iam.list_role_policies.return_value = {"PolicyNames": []}
    arn = "arn:aws:iam::aws:policy/SecretsManagerReadWrite"

    def attached(**kwargs):
        if "Marker" not in kwargs:
            return {"AttachedPolicies": [], "IsTruncated": True, "Marker": "next"}
        return {"AttachedPolicies": [{"PolicyArn": arn}]}

    clients.iam.list_attached_role_policies.side_effect = attached
    clients.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v2"}}
    clients.iam.get_policy_version.return_value = {
        "PolicyVersion": {
            "Document": {"Statement": [{"Effect": "Allow", "Action": "secretsmanager:*", "Resource": "*"}]}
        }
    }
    assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.FAIL
    clients.iam.get_policy_version.assert_called_with(PolicyArn=arn, VersionId="v2")


def test_policy_read_failure_does_not_pass(ctx, clients):
    clients.iam.list_role_policies.return_value = {"PolicyNames": []}
    clients.iam.list_attached_role_policies.side_effect = _client_error("AccessDenied")
    assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.FAIL


def test_missing_policy_page_cursor_does_not_pass(ctx, clients):
    clients.iam.list_role_policies.return_value = {"PolicyNames": [], "IsTruncated": True}
    assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.FAIL


def test_partial_role_inventory_does_not_pass_even_with_scoped_policy(ctx, clients):
    def inline(RoleName):
        if RoleName.endswith("skypilot-api"):
            raise _client_error("NoSuchEntity")
        return {"PolicyNames": ["scoped"]}

    clients.iam.list_role_policies.side_effect = inline
    clients.iam.get_role_policy.return_value = {
        "PolicyDocument": {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/superplane/database-*",
                }
            ]
        }
    }
    assert check_12_4_secret_access_is_resource_scoped(ctx).result == Result.FAIL


@pytest.mark.parametrize(
    "value",
    [
        "null",
        "{}",
        '"https://example.com"',
        '["https://*.example.com"]',
        "[123]",
        '["https://user:secret@example.com"]',
    ],
)
def test_malformed_cors_never_claims_explicit_origins(ctx, clients, value):
    parameters = _healthy_parameters()
    parameters["cors-allowed-origins"] = value
    _paginated_parameters(clients, parameters)
    result = check_12_8_cors_allowlist_is_not_wildcard(ctx)
    assert result.result == Result.FAIL
    assert "secret@example" not in str(result)


def test_empty_required_configuration_fails(ctx, clients):
    parameters = _healthy_parameters()
    parameters["namespace"] = " "
    _paginated_parameters(clients, parameters)
    assert check_12_7_ssm_parameters_published(ctx).result == Result.FAIL


def test_one_missing_secret_name_is_not_success(ctx, clients):
    parameters = _healthy_parameters()
    del parameters["jwt-secret-name"]
    _paginated_parameters(clients, parameters)
    assert check_12_9_secret_parameters_hold_names(ctx).result == Result.FAIL
