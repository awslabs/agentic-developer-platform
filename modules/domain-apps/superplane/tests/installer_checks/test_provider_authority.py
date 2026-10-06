"""Conditional owner enrollment; fake transports never access a cloud."""

import copy
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from installation import provider_authority as owner


@pytest.fixture
def setup():
    schema = owner.contract()
    account = "123456789012"
    org = "12345678-1234-1234-1234-123456789012"
    request = "87654321-1234-1234-1234-123456789012"
    workspace = str(uuid.uuid5(schema.WORKSPACE_NAMESPACE, f"{org}/{request}"))
    provider = f"arn:aws:iam::{account}:role/adp-dev-spp-{schema.digest([org, workspace])[:32]}"
    operator = f"arn:aws:iam::{account}:role/installation-operator"
    role_id = "AROA" + "A" * 17
    binding = dict(
        domain="superplane",
        org_id=org,
        adp_org_id="tenant",
        producer_registry_id="producer",
        worker_registry_id="worker",
        worker_namespace="superplane-demo",
        worker_scaled_job="superplane-paid-worker",
        current_identity_enforced=False,
        domain_database_secret_id="domain-dsn",
        domain_database_schema="superplane",
    )
    state = SimpleNamespace(
        items={},
        writes=0,
        lose_reply=False,
        conflict=False,
        selected_account=account,
        selected_role_id=role_id,
        policy_version="v1",
        extra_policy=False,
        inline=False,
        registry="registry",
    )
    managed = schema.managed_policies(provider)

    def get_role(RoleName):
        return {
            "Role": {
                "Arn": f"arn:aws:iam::{account}:role/{RoleName}",
                "RoleId": role_id,
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": []},
            }
        }

    iam = SimpleNamespace(
        get_role=get_role,
        list_role_policies=lambda **kw: {
            "PolicyNames": ["extra"] if state.inline else []
        },
        list_attached_role_policies=lambda **kw: {
            "AttachedPolicies": [
                {"PolicyArn": arn}
                for arn in managed
                + (
                    ["arn:aws:iam::aws:policy/AdministratorAccess"]
                    if state.extra_policy
                    else []
                )
            ]
        },
        get_policy=lambda **kw: {"Policy": {"DefaultVersionId": state.policy_version}},
        get_policy_version=lambda **kw: {
            "PolicyVersion": {"Document": {"Version": "2012-10-17", "Statement": []}}
        },
    )
    boundary = provider.replace(":role/", ":policy/") + "-child-boundary"
    value = dict(
        version=1,
        credential_id=schema.handle(provider),
        owner=schema.OWNER,
        domain="superplane",
        installation_id="a" * 24,
        adp_org_id="tenant",
        org_id=org,
        workspace_id=workspace,
        request_id=request,
        subject="subject",
        user_id="canonical",
        membership_id="membership",
        account_id=account,
        region="us-east-1",
        service="aws",
        label="provider",
        role_arn=provider,
        role_id=role_id,
        policy_sha256=schema.policy_identity(iam, provider, managed)[1],
        managed_policy_arns=managed,
        child_boundary_arn=boundary,
        child_boundary_sha256=schema.boundary_identity(iam, boundary),
        secret_arn=f"arn:aws:secretsmanager:us-east-1:{account}:secret:provider-abcdef",
        secret_version="d" * 32,
        binding_sha256=schema.digest(binding),
        generation=1,
        status="active",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        validation_profile={
            "region": "us-east-1",
            "image_id": "ami-12345678",
            "instance_type": "t3.small",
            "subnet_id": "subnet-12345678",
            "security_group_ids": ["sg-12345678"],
        },
    )
    doc = dict(
        version=1,
        environment="dev",
        operator_role_arn=operator,
        operator_role_id=role_id,
        gateway_namespace="adp",
        registry_table="registry",
        authority=value,
        management_cluster="management",
    )
    route = dict(
        version=2, enabled=True, installation_id="a" * 24, namespace="superplane-demo"
    )
    config = {
        "ADP_DOMAIN_PROVIDER_ACCOUNT_ID": account,
        "ADP_DOMAIN_OPERATION_BINDINGS": json.dumps([binding]),
        "ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE": "adp-dev-superplane-provider-authorities",
        "ADP_DOMAIN_PROVIDER_EVIDENCE_TABLE": "adp-dev-superplane-provider-evidence",
    }
    for kind, suffix, scopes in (
        ("producer", "api-producer", ["domain:operation-producer"]),
        (
            "worker",
            "domain-worker",
            ["domain:operation-executor", "domain:operation-recovery"],
        ),
    ):
        state.items[kind] = {
            "owner": {"S": "webhook-terraform-domain-operations-v1"},
            "scope": {"S": "internal"},
            "status": {"S": "active"},
            "org_id": {"S": "tenant"},
            "domain_org_id": {"S": org},
            "role_arn": {
                "S": f"arn:aws:iam::{account}:role/adp-dev-superplane-{suffix}"
            },
            "credential_scopes": {"SS": scopes},
            "iam_role_id": {"S": role_id},
        }

    def get_item(**kw):
        assert kw["ConsistentRead"] is True
        key = next(iter(kw["Key"].values()))["S"]
        return {"Item": copy.deepcopy(state.items[key])} if key in state.items else {}

    def put_item(**kw):
        key = kw["Item"]["record_id"]["S"]
        old = state.items.get(key)
        if state.conflict or (
            kw["ConditionExpression"].startswith("attribute_not_exists")
            and old is not None
        ):
            raise RuntimeError("condition changed")
        if kw["ConditionExpression"] == "revision = :expected":
            assert old["revision"] == kw["ExpressionAttributeValues"][":expected"]
        state.items[key] = copy.deepcopy(kw["Item"])
        state.writes += 1
        if state.lose_reply:
            state.lose_reply = False
            raise RuntimeError("lost reply after committed write")

    services = {
        "iam": iam,
        "dynamodb": SimpleNamespace(get_item=get_item, put_item=put_item),
        "sts": SimpleNamespace(
            get_caller_identity=lambda: {
                "Account": state.selected_account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/installation-operator/session",
                "UserId": state.selected_role_id + ":session",
            }
        ),
        "secretsmanager": SimpleNamespace(
            describe_secret=lambda **kw: {
                "VersionIdsToStages": {"d" * 32: ["AWSCURRENT"]}
            }
        ),
        "s3": SimpleNamespace(
            get_object=lambda **kw: {"Body": io.BytesIO(json.dumps(route).encode())}
        ),
        "eks": SimpleNamespace(
            describe_cluster=lambda **kw: {
                "cluster": {
                    "arn": f"arn:aws:eks:us-east-1:{account}:cluster/management",
                    "status": "ACTIVE",
                }
            }
        ),
        "ssm": SimpleNamespace(
            get_parameter=lambda **kw: {"Parameter": {"Value": state.registry}}
        ),
    }
    session = SimpleNamespace(client=lambda name, **kw: services[name])
    return SimpleNamespace(
        doc=doc,
        state=state,
        session=session,
        config=config,
        route=route,
        iam=iam,
        run=lambda **kw: owner.enroll(
            doc, session, config_reader=lambda *args: config, **kw
        ),
    )


def test_check_only_never_enrolls_or_claims_human_authority(setup):
    answer = setup.run()
    assert (
        answer["state"] == "absent"
        and not answer["human_admitted"]
        and not answer["worker_ready"]
    )
    assert setup.state.writes == 0


def test_enroll_exact_noop_and_revoke_are_conditional(setup):
    assert setup.run(check_only=False)["state"] == "configured"
    assert (
        setup.run(check_only=False)["state"] == "configured" and setup.state.writes == 1
    )
    assert setup.run(check_only=False, revoke=True)["state"] == "revoked"
    assert (
        setup.run(check_only=False, revoke=True)["state"] == "revoked"
        and setup.state.writes == 2
    )
    with pytest.raises(ValueError):
        setup.run(check_only=False)


def test_lost_reply_reconciles_original_identity_without_second_write(setup):
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(check_only=False)
    assert (
        setup.run(check_only=False)["state"] == "configured" and setup.state.writes == 1
    )


@pytest.mark.parametrize(
    "kind",
    [
        "account",
        "operator",
        "installation",
        "registry",
        "binding",
        "policy-version",
        "extra-policy",
        "inline",
        "beneficiary",
    ],
)
def test_drift_and_owner_conflicts_refuse_without_write(setup, kind):
    if kind == "account":
        setup.state.selected_account = "000000000000"
    elif kind == "operator":
        setup.state.selected_role_id = "AROA" + "B" * 17
    elif kind == "installation":
        setup.route["installation_id"] = "b" * 24
    elif kind == "registry":
        setup.state.registry = "foreign-registry"
    elif kind == "binding":
        setup.config["ADP_DOMAIN_OPERATION_BINDINGS"] = "[]"
    elif kind == "policy-version":
        setup.state.policy_version = "v2"
    elif kind == "extra-policy":
        setup.state.extra_policy = True
    elif kind == "inline":
        setup.state.inline = True
    else:
        setup.run(check_only=False)
        setup.doc["authority"]["user_id"] = "other-beneficiary"
        setup.state.writes = 0
    with pytest.raises(ValueError):
        setup.run(check_only=False)
    assert setup.state.writes == 0


def test_revoke_does_not_depend_on_broken_installation_or_provider(setup):
    setup.run(check_only=False)
    setup.config.clear()
    setup.state.policy_version = "v2"
    assert setup.run(check_only=False, revoke=True)["state"] == "revoked"


def test_concurrent_registration_cannot_overwrite(setup):
    setup.state.conflict = True
    with pytest.raises(RuntimeError):
        setup.run(check_only=False)
    assert setup.state.writes == 0


def test_owner_kubernetes_context_must_match_selected_eks_identity(monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        assert argv[-1] == "jsonpath={.clusters}"
        return SimpleNamespace(stdout=json.dumps([{"cluster": {"server": "https://foreign.invalid", "certificate-authority-data": "foreign"}}]))

    monkeypatch.setattr(owner.subprocess, "run", command)
    with pytest.raises(ValueError):
        owner.installed_config("adp", {"endpoint": "https://selected.invalid", "certificateAuthority": {"data": "selected"}})
    assert len(calls) == 1  # No request to the unverified cluster, no user/token projection.
