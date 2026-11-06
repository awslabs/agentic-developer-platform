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
        return SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "cluster": {
                            "server": "https://foreign.invalid",
                            "certificate-authority-data": "foreign",
                        }
                    }
                ]
            )
        )

    monkeypatch.setattr(owner.subprocess, "run", command)
    with pytest.raises(ValueError):
        owner.installed_config(
            "adp",
            {
                "endpoint": "https://selected.invalid",
                "certificateAuthority": {"data": "selected"},
            },
        )
    assert (
        len(calls) == 1
    )  # No request to the unverified cluster, no user/token projection.


def binding_rotation(setup, *, expired=False):
    if expired:
        setup.doc["authority"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    binding = json.loads(setup.config["ADP_DOMAIN_OPERATION_BINDINGS"])[0]
    binding["worker_image_digests"] = ["sha256:" + "e" * 64]
    setup.config["ADP_DOMAIN_OPERATION_BINDINGS"] = json.dumps([binding])
    setup.doc["authority"]["binding_sha256"] = owner.contract().digest(binding)
    setup.doc["authority"]["generation"] += 1
    return previous


@pytest.mark.parametrize("expired", [False, True])
def test_binding_rotation_is_reviewed_conditional_and_idempotent(setup, expired):
    previous = binding_rotation(setup, expired=expired)
    assert setup.run(previous_document=previous)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    assert (
        setup.run(previous_document=previous, check_only=False)["state"] == "configured"
    )
    assert setup.state.writes == 2
    stored = json.loads(
        setup.state.items[previous["authority"]["credential_id"]]["document"]["S"]
    )
    assert stored == setup.doc["authority"]
    assert stored["expires_at"] == previous["authority"]["expires_at"]
    if expired:
        assert datetime.fromisoformat(stored["expires_at"]) < datetime.now(timezone.utc)
    assert (
        setup.run(previous_document=previous, check_only=False)["state"] == "configured"
    )
    assert setup.state.writes == 2
    answer = setup.run(previous_document=previous)
    assert not answer["human_admitted"] and not answer["worker_ready"]
    assert setup.run(revoke=True, check_only=False)["state"] == "revoked"
    stored = json.loads(
        setup.state.items[previous["authority"]["credential_id"]]["document"]["S"]
    )
    assert stored["generation"] == 3
    with pytest.raises(ValueError):
        setup.run(previous_document=previous, check_only=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "another-human"),
        ("membership_id", "another-membership"),
        ("label", "another-scope"),
        ("policy_sha256", "f" * 64),
        ("secret_version", "f" * 32),
        ("child_boundary_sha256", "f" * 64),
        ("expires_at", "2099-01-01T00:00:00+00:00"),
        ("generation", 3),
    ],
)
@pytest.mark.parametrize("expired", [False, True])
def test_binding_rotation_cannot_change_authority_or_extend_expiry(
    setup, field, value, expired
):
    previous = binding_rotation(setup, expired=expired)
    setup.doc["authority"][field] = value
    with pytest.raises(ValueError):
        setup.run(previous_document=previous, check_only=False)
    assert setup.state.writes == 1


@pytest.mark.parametrize(
    "problem",
    [
        "absent",
        "revoked",
        "wrong-binding",
        "policy-drift",
        "operator-drift",
        "extra-policy",
        "registry-drift",
        "no-explicit-rotation",
        "same-binding",
    ],
)
@pytest.mark.parametrize("expired", [False, True])
def test_binding_rotation_refuses_invalid_predecessor_or_live_state(
    setup, problem, expired
):
    previous = binding_rotation(setup, expired=expired)
    key = previous["authority"]["credential_id"]
    if problem == "absent":
        del setup.state.items[key]
    elif problem == "revoked":
        revoked = {**previous["authority"], "status": "revoked", "generation": 2}
        setup.state.items[key]["document"]["S"] = owner.contract().canonical(revoked)
        setup.state.items[key]["revision"]["S"] = owner.contract().digest(revoked)
    elif problem == "wrong-binding":
        setup.doc["authority"]["binding_sha256"] = "f" * 64
    elif problem == "policy-drift":
        setup.state.policy_version = "v2"
    elif problem == "operator-drift":
        setup.state.selected_role_id = "AROA" + "B" * 17
    elif problem == "extra-policy":
        setup.state.extra_policy = True
    elif problem == "registry-drift":
        setup.state.registry = "another-registry"
    elif problem == "same-binding":
        setup.doc["authority"]["binding_sha256"] = previous["authority"][
            "binding_sha256"
        ]
    with pytest.raises(ValueError):
        setup.run(
            previous_document=None if problem == "no-explicit-rotation" else previous,
            check_only=False,
        )
    assert setup.state.writes == 1


@pytest.mark.parametrize("expired", [False, True])
def test_binding_rotation_lost_reply_reconciles_exact_committed_generation(
    setup, expired
):
    previous = binding_rotation(setup, expired=expired)
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(previous_document=previous, check_only=False)
    assert setup.state.writes == 2
    assert (
        setup.run(previous_document=previous, check_only=False)["state"] == "configured"
    )
    assert setup.state.writes == 2


@pytest.mark.parametrize("expired", [False, True])
def test_binding_rotation_concurrent_change_does_not_write(setup, expired):
    previous = binding_rotation(setup, expired=expired)
    setup.state.conflict = True
    with pytest.raises(RuntimeError):
        setup.run(previous_document=previous, check_only=False)
    assert setup.state.writes == 1


def tag_trust_rotation(setup):
    schema = owner.contract()
    authority = setup.doc["authority"]
    statement = {
        "Effect": "Allow",
        "Principal": {"AWS": f"arn:aws:iam::{authority['account_id']}:role/gateway"},
        "Action": ["sts:AssumeRole", "sts:TagSession"],
        "Condition": {
            "StringEquals": {
                "sts:ExternalId": "fixture-only-external-id",
                "aws:RequestTag/adp:user_id": authority["user_id"],
                "aws:RequestTag/adp:agent_id": [
                    "superplane-operation",
                    "superplane-provider-validation",
                ],
                "aws:RequestTag/adp:persona": [
                    "superplane-operation",
                    "superplane-provider-validation",
                ],
            },
            "StringLike": {"aws:RequestTag/adp:task_id": "?*"},
            "ForAllValues:StringEquals": {
                "aws:TagKeys": [
                    "adp:user_id",
                    "adp:agent_id",
                    "adp:task_id",
                    "adp:persona",
                ]
            },
        },
    }
    snapshot = {
        "trust": {"Version": "2012-10-17", "Statement": [statement]},
        "managed": {
            arn: {
                "version": "v1",
                "document": {"Version": "2012-10-17", "Statement": []},
            }
            for arn in authority["managed_policy_arns"]
        },
    }
    setup.state.trust = snapshot["trust"]
    original_get_role = setup.iam.get_role

    def get_role(**kwargs):
        result = original_get_role(**kwargs)
        result["Role"]["AssumeRolePolicyDocument"] = setup.state.trust
        return result

    setup.iam.get_role = get_role
    authority["policy_sha256"] = schema.digest(snapshot)
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    repaired = owner.repaired_session_tag_policy(snapshot, authority, schema)
    setup.state.trust = repaired["trust"]
    authority["policy_sha256"] = schema.digest(repaired)
    authority["generation"] += 1
    return previous, snapshot


def test_tag_trust_repair_preserves_assume_external_id_and_all_tags(setup):
    previous, snapshot = tag_trust_rotation(setup)
    assume, tagging = setup.state.trust["Statement"]
    assert assume["Condition"] == snapshot["trust"]["Statement"][0]["Condition"]
    assert assume["Principal"] == tagging["Principal"]
    assert assume["Action"] == "sts:AssumeRole"
    assert tagging["Action"] == "sts:TagSession"
    condition = copy.deepcopy(assume["Condition"])
    del condition["StringEquals"]["sts:ExternalId"]
    assert tagging["Condition"] == condition
    args = dict(previous_document=previous, previous_tag_policy=snapshot)
    assert setup.run(**args)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2


@pytest.mark.parametrize(
    "problem",
    [
        "snapshot-tamper",
        "new-grants",
        "new-trust-principal",
        "no-external-id",
        "tag-value",
        "extra-statement",
        "role-id",
        "policy-version",
        "binding",
        "secret",
        "expiry-extension",
        "beneficiary",
        "generation-skip",
        "absent",
        "revoked",
        "concurrent",
        "expired",
        "no-previous",
        "ordinary-binding-mode",
    ],
)
def test_tag_trust_repair_cannot_change_scope_or_restore_authority(setup, problem):
    if problem == "expired":
        setup.doc["authority"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    previous, snapshot = tag_trust_rotation(setup)
    authority = setup.doc["authority"]
    if problem == "snapshot-tamper":
        snapshot["trust"]["Statement"][0]["Principal"]["AWS"] += "other"
    elif problem == "new-grants":
        setup.state.extra_policy = True
    elif problem == "new-trust-principal":
        setup.state.trust["Statement"][1]["Principal"]["AWS"] += "other"
    elif problem == "no-external-id":
        del setup.state.trust["Statement"][0]["Condition"]["StringEquals"][
            "sts:ExternalId"
        ]
    elif problem == "tag-value":
        setup.state.trust["Statement"][1]["Condition"]["StringEquals"][
            "aws:RequestTag/adp:user_id"
        ] = "other"
    elif problem == "extra-statement":
        setup.state.trust["Statement"].append(
            {"Effect": "Allow", "Action": "*", "Principal": "*"}
        )
    elif problem == "role-id":
        authority["role_id"] = "AROA" + "B" * 17
    elif problem == "policy-version":
        setup.state.policy_version = "v2"
    elif problem == "binding":
        authority["binding_sha256"] = "f" * 64
    elif problem == "secret":
        authority["secret_version"] = "f" * 32
    elif problem == "expiry-extension":
        authority["expires_at"] = "2099-01-01T00:00:00+00:00"
    elif problem == "beneficiary":
        authority["user_id"] = "another"
    elif problem == "generation-skip":
        authority["generation"] += 1
    elif problem == "absent":
        del setup.state.items[authority["credential_id"]]
    elif problem == "revoked":
        setup.state.items[authority["credential_id"]]["document"]["S"] = "{}"
    elif problem == "concurrent":
        setup.state.conflict = True
    elif problem == "no-previous":
        previous = None
    elif problem == "ordinary-binding-mode":
        snapshot = None
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(
            previous_document=previous, previous_tag_policy=snapshot, check_only=False
        )
    assert setup.state.writes == 1


def test_tag_trust_repair_lost_write_response_and_revocation(setup):
    previous, snapshot = tag_trust_rotation(setup)
    args = dict(
        previous_document=previous, previous_tag_policy=snapshot, check_only=False
    )
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args)
    assert setup.run(**args)["state"] == "configured"
    assert setup.state.writes == 2
    assert setup.run(revoke=True, check_only=False)["state"] == "revoked"
    with pytest.raises(ValueError):
        setup.run(**args)
    assert setup.state.writes == 3


def renewal(setup, *, expired=False):
    if expired:
        setup.doc["authority"]["expires_at"] = "2026-01-01T00:00:00+00:00"
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    setup.doc["authority"]["generation"] += 1
    setup.doc["authority"]["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=12)
    ).isoformat()
    return previous


def test_expired_binding_reconciliation_composes_with_separate_owner_renewal(setup):
    previous = binding_rotation(setup, expired=True)
    setup.run(previous_document=previous, check_only=False)
    rotated = copy.deepcopy(setup.doc)
    assert rotated["authority"]["expires_at"] == previous["authority"]["expires_at"]
    setup.doc["authority"]["generation"] += 1
    setup.doc["authority"]["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat()
    with pytest.raises(ValueError):
        setup.run(previous_document=rotated, check_only=False)
    assert setup.state.writes == 2
    assert (
        setup.run(previous_document=rotated, renew=True)["state"] == "renewal-reviewed"
    )
    assert setup.state.writes == 2
    answer = setup.run(previous_document=rotated, renew=True, check_only=False)
    assert setup.state.writes == 3
    assert answer["state"] == "configured"
    assert not answer["human_admitted"] and not answer["worker_ready"]


@pytest.mark.parametrize("expired", [False, True])
def test_explicit_owner_renewal_is_reviewed_conditional_and_idempotent(setup, expired):
    previous = renewal(setup, expired=expired)
    assert (
        setup.run(previous_document=previous, renew=True)["state"] == "renewal-reviewed"
    )
    assert setup.state.writes == 1
    assert (
        setup.run(previous_document=previous, renew=True, check_only=False)["state"]
        == "configured"
    )
    assert setup.state.writes == 2
    assert (
        setup.run(previous_document=previous, renew=True, check_only=False)["state"]
        == "configured"
    )
    assert setup.state.writes == 2
    assert setup.run(revoke=True, check_only=False)["state"] == "revoked"
    with pytest.raises(ValueError):
        setup.run(previous_document=previous, renew=True, check_only=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "another-human"),
        ("membership_id", "another-member"),
        ("request_id", "another-request"),
        ("binding_sha256", "f" * 64),
        ("policy_sha256", "f" * 64),
        ("secret_version", "f" * 32),
        ("generation", 3),
        ("status", "revoked"),
        ("expires_at", "2099-01-01T00:00:00+00:00"),
        ("expires_at", "2000-01-01T00:00:00+00:00"),
    ],
)
def test_owner_renewal_cannot_widen_scope_or_duration(setup, field, value):
    previous = renewal(setup)
    setup.doc["authority"][field] = value
    with pytest.raises(ValueError):
        setup.run(previous_document=previous, renew=True, check_only=False)
    assert setup.state.writes == 1


def test_owner_renewal_rechecks_live_permissions(setup):
    previous = renewal(setup)
    setup.state.extra_policy = True
    with pytest.raises(ValueError):
        setup.run(previous_document=previous, renew=True, check_only=False)
    assert setup.state.writes == 1


def test_owner_renewal_requires_explicit_mode_and_exact_predecessor(setup):
    previous = renewal(setup)
    with pytest.raises(ValueError):
        setup.run(previous_document=previous, check_only=False)
    with pytest.raises(ValueError):
        setup.run(renew=True, check_only=False)
    setup.state.conflict = True
    with pytest.raises(Exception):
        setup.run(previous_document=previous, renew=True, check_only=False)


def test_regional_provider_enrolls_in_management_region(setup):
    document, session = copy.deepcopy(setup.doc), setup.session

    def config_reader(*args):
        return setup.config

    document["management_region"] = "us-east-1"
    document["authority"]["region"] = "us-west-2"
    document["authority"]["validation_profile"]["region"] = "us-west-2"
    document["authority"]["secret_arn"] = document["authority"]["secret_arn"].replace(
        "us-east-1", "us-west-2"
    )
    original = session.client
    calls = []

    def client(service, *, region_name):
        calls.append((service, region_name))
        assert region_name == (
            "us-east-1" if service in {"dynamodb", "s3", "eks", "ssm"} else "us-west-2"
        )
        return original(service, region_name=region_name)

    session.client = client
    result = owner.enroll(
        document, session, config_reader=config_reader, check_only=False
    )
    assert result["state"] == "configured"
    assert ("dynamodb", "us-east-1") in calls and (
        "secretsmanager",
        "us-west-2",
    ) in calls


def beneficiary_tag_rotation(setup):
    tag_trust_rotation(setup)
    schema = owner.contract()
    authority = setup.doc["authority"]
    snapshot = {
        "trust": copy.deepcopy(setup.state.trust),
        "managed": {
            arn: {
                "version": "v1",
                "document": {"Version": "2012-10-17", "Statement": []},
            }
            for arn in authority["managed_policy_arns"]
        },
    }
    for statement in snapshot["trust"]["Statement"]:
        statement["Condition"]["StringEquals"]["aws:RequestTag/adp:user_id"] = (
            authority["subject"]
        )
    setup.state.items.pop(authority["credential_id"])
    setup.state.writes = 0
    setup.state.trust = snapshot["trust"]
    authority.update(generation=1, policy_sha256=schema.digest(snapshot))
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    repaired = owner.repaired_beneficiary_tag_policy(snapshot, authority, schema)
    setup.state.trust = repaired["trust"]
    authority.update(generation=2, policy_sha256=schema.digest(repaired))
    return previous, snapshot


def test_beneficiary_tag_repair_preserves_enrollment_and_is_idempotent(setup):
    previous, snapshot = beneficiary_tag_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_beneficiary_tag=True,
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2
    for statement in setup.state.trust["Statement"]:
        assert (
            statement["Condition"]["StringEquals"]["aws:RequestTag/adp:user_id"]
            == previous["authority"]["user_id"]
        )


@pytest.mark.parametrize(
    "problem",
    [
        "snapshot",
        "principal",
        "grant",
        "external-id",
        "different-user",
        "expiry",
        "revoked",
        "missing-prior",
        "ordinary-mode",
        "role-id",
    ],
)
def test_beneficiary_repair_refuses_other_changes(setup, problem):
    previous, snapshot = beneficiary_tag_rotation(setup)
    authority = setup.doc["authority"]
    repair = True
    if problem == "snapshot":
        snapshot["trust"]["Statement"][0]["Effect"] = "Deny"
    elif problem == "principal":
        setup.state.trust["Statement"][0]["Principal"]["AWS"] += "other"
    elif problem == "grant":
        setup.state.extra_policy = True
    elif problem == "external-id":
        del setup.state.trust["Statement"][0]["Condition"]["StringEquals"][
            "sts:ExternalId"
        ]
    elif problem == "different-user":
        authority["user_id"] = "other"
    elif problem == "expiry":
        authority["expires_at"] = "2099-01-01T00:00:00+00:00"
    elif problem == "revoked":
        setup.state.items[authority["credential_id"]]["document"]["S"] = "{}"
    elif problem == "missing-prior":
        previous = None
    elif problem == "ordinary-mode":
        repair = False
    elif problem == "role-id":
        authority["role_id"] = "AROA" + "B" * 17
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(
            previous_document=previous,
            previous_tag_policy=snapshot,
            repair_beneficiary_tag=repair,
            check_only=False,
        )
    assert setup.state.writes == 1


def network_read_rotation(setup):
    schema = owner.contract()
    authority = setup.doc["authority"]
    arn = authority["role_arn"].replace(":role/", ":policy/") + "-state-validation"
    snapshot = {
        "trust": {"Version": "2012-10-17", "Statement": []},
        "managed": {
            p: {"version": "v1", "document": {"Version": "2012-10-17", "Statement": []}}
            for p in authority["managed_policy_arns"]
        },
    }
    snapshot["managed"][arn]["document"]["Statement"] = [
        {
            "Effect": "Allow",
            "Action": ["ec2:DescribeInstances", "ec2:DescribeVpcAttribute"],
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestedRegion": authority["region"]}},
        },
        {"Effect": "Deny", "Action": ["iam:CreatePolicyVersion"], "Resource": "*"},
    ]
    setup.state.network_policy = copy.deepcopy(snapshot)
    setup.iam.get_policy = lambda PolicyArn: {
        "Policy": {
            "DefaultVersionId": setup.state.network_policy["managed"].get(
                PolicyArn, {"version": "v1"}
            )["version"]
        }
    }
    setup.iam.get_policy_version = lambda PolicyArn, VersionId: {
        "PolicyVersion": {
            "Document": setup.state.network_policy["managed"].get(
                PolicyArn, {"document": {"Version": "2012-10-17", "Statement": []}}
            )["document"]
        }
    }
    base_get = setup.iam.get_role

    def get_role(**kw):
        r = base_get(**kw)
        r["Role"]["AssumeRolePolicyDocument"] = setup.state.network_policy["trust"]
        return r

    setup.iam.get_role = get_role
    authority["policy_sha256"] = schema.digest(snapshot)
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    # Construct the new AWS readback independently of the repair implementation.
    live = copy.deepcopy(snapshot)
    live["managed"][arn]["version"] = "v3"
    live["managed"][arn]["document"]["Statement"][0]["Action"] = [
        "ec2:DescribeInstances",
        "ec2:DescribeAddressesAttribute",
        "ec2:DescribePrefixLists",
        "ec2:DescribeVpcAttribute",
    ]
    setup.state.network_policy = live
    authority["policy_sha256"] = schema.digest(live)
    authority["generation"] += 1
    return previous, snapshot, arn


def test_network_reads_repair_is_exact_conditional_and_lost_reply_safe(setup):
    previous, snapshot, arn = network_read_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_network_reads_version="v3",
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args, check_only=False)
    assert setup.state.writes == 2
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2
    assert (
        owner.repaired_network_reads_policy(
            snapshot, previous["authority"], owner.contract(), "v3"
        )
        == setup.state.network_policy
    )
    assert snapshot["managed"][arn]["version"] == "v1"
    setup.run(check_only=False, revoke=True)
    with pytest.raises(ValueError):
        setup.run(**args, check_only=False)


@pytest.mark.parametrize(
    "problem",
    [
        "trust",
        "other-shard",
        "extra-action",
        "resource",
        "region",
        "condition",
        "snapshot",
        "wrong-version",
        "older-version",
        "no-version",
        "no-previous",
        "no-snapshot",
        "binding",
        "beneficiary",
        "expiry",
        "generation",
        "absent",
        "revoked",
        "concurrent",
        "renew",
        "other-repair",
    ],
)
def test_network_read_repair_refuses_any_unreviewed_change(setup, problem):
    previous, snapshot, arn = network_read_rotation(setup)
    live = setup.state.network_policy
    statement = live["managed"][arn]["document"]["Statement"][0]
    authority = setup.doc["authority"]
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_network_reads_version="v3",
        check_only=False,
    )
    if problem == "trust":
        live["trust"]["Statement"] = [
            {"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}
        ]
    elif problem == "other-shard":
        other = next(p for p in live["managed"] if p != arn)
        live["managed"][other]["version"] = "v2"
    elif problem == "extra-action":
        statement["Action"].append("ec2:DeleteVpc")
    elif problem == "resource":
        statement["Resource"] = ["*"]
    elif problem == "region":
        statement["Condition"]["StringEquals"]["aws:RequestedRegion"] = "us-west-2"
    elif problem == "condition":
        del statement["Condition"]
    elif problem == "snapshot":
        snapshot["managed"][arn]["document"]["Statement"][0]["Action"].append("ec2:*")
    elif problem in {"wrong-version", "older-version", "no-version"}:
        args["repair_network_reads_version"] = {
            "wrong-version": "v4",
            "older-version": "v1",
            "no-version": None,
        }[problem]
    elif problem == "no-previous":
        args["previous_document"] = None
    elif problem == "no-snapshot":
        args["previous_tag_policy"] = None
    elif problem == "binding":
        authority["binding_sha256"] = "f" * 64
    elif problem == "beneficiary":
        authority["user_id"] = "another"
    elif problem == "expiry":
        authority["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=3)
        ).isoformat()
    elif problem == "generation":
        authority["generation"] += 1
    elif problem == "absent":
        del setup.state.items[authority["credential_id"]]
    elif problem == "revoked":
        setup.state.items[authority["credential_id"]]["document"]["S"] = "{}"
    elif problem == "concurrent":
        setup.state.conflict = True
    elif problem == "renew":
        args["renew"] = True
    elif problem == "other-repair":
        args["repair_beneficiary_tag"] = True
    # Even if a caller names the real changed live digest, unrelated deltas refuse.
    authority["policy_sha256"] = owner.contract().digest(live)
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(**args)
    assert setup.state.writes == 1


def nodegroup_read_rotation(setup):
    previous, snapshot, _ = network_read_rotation(setup)
    authority = setup.doc["authority"]
    arn = authority["role_arn"].replace(":role/", ":policy/") + "-identity"
    live = copy.deepcopy(snapshot)
    live["managed"][arn]["version"] = "v2"
    live["managed"][arn]["document"]["Statement"].append(
        {
            "Effect": "Allow",
            "Action": ["iam:GetRole"],
            "Resource": f"arn:aws:iam::{authority['account_id']}:role/aws-service-role/eks-nodegroup.amazonaws.com/AWSServiceRoleForAmazonEKSNodegroup",
        }
    )
    setup.state.network_policy = live
    authority["policy_sha256"] = owner.contract().digest(live)
    return previous, snapshot, arn


def test_nodegroup_role_read_repair_is_conditional_and_lost_reply_safe(setup):
    previous, snapshot, arn = nodegroup_read_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_nodegroup_role_read_version="v2",
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args, check_only=False)
    assert setup.state.writes == 2
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2
    assert snapshot["managed"][arn]["version"] == "v1"
    setup.run(check_only=False, revoke=True)
    with pytest.raises(ValueError):
        setup.run(**args, check_only=False)


@pytest.mark.parametrize(
    "problem",
    [
        "extra-action",
        "wildcard-resource",
        "other-account",
        "other-role",
        "trust",
        "other-shard",
        "snapshot",
        "wrong-version",
        "old-version",
        "no-version",
        "no-previous",
        "no-snapshot",
        "binding",
        "beneficiary",
        "expiry",
        "generation",
        "absent",
        "revoked",
        "concurrent",
        "renew",
        "other-repair",
    ],
)
def test_nodegroup_role_read_repair_rejects_unreviewed_changes(setup, problem):
    previous, snapshot, arn = nodegroup_read_rotation(setup)
    authority = setup.doc["authority"]
    live = setup.state.network_policy
    statement = live["managed"][arn]["document"]["Statement"][-1]
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_nodegroup_role_read_version="v2",
        check_only=False,
    )
    if problem == "extra-action":
        statement["Action"].append("iam:CreateServiceLinkedRole")
    elif problem == "wildcard-resource":
        statement["Resource"] = "*"
    elif problem == "other-account":
        statement["Resource"] = statement["Resource"].replace(
            authority["account_id"], "999988887777"
        )
    elif problem == "other-role":
        statement["Resource"] = authority["role_arn"]
    elif problem == "trust":
        live["trust"]["Statement"].append(
            {"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}
        )
    elif problem == "other-shard":
        live["managed"][next(p for p in live["managed"] if p != arn)]["version"] = "v2"
    elif problem == "snapshot":
        snapshot["managed"][arn]["version"] = "v9"
    elif problem in {"wrong-version", "old-version", "no-version"}:
        args["repair_nodegroup_role_read_version"] = {
            "wrong-version": "v3",
            "old-version": "v1",
            "no-version": None,
        }[problem]
    elif problem == "no-previous":
        args["previous_document"] = None
    elif problem == "no-snapshot":
        args["previous_tag_policy"] = None
    elif problem == "binding":
        authority["binding_sha256"] = "f" * 64
    elif problem == "beneficiary":
        authority["user_id"] = "different"
    elif problem == "expiry":
        authority["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=3)
        ).isoformat()
    elif problem == "generation":
        authority["generation"] += 1
    elif problem == "absent":
        del setup.state.items[authority["credential_id"]]
    elif problem == "revoked":
        setup.state.items[authority["credential_id"]]["document"]["S"] = "{}"
    elif problem == "concurrent":
        setup.state.items[authority["credential_id"]]["revision"]["S"] = "f" * 64
    elif problem == "renew":
        args["renew"] = True
    elif problem == "other-repair":
        args["repair_network_reads_version"] = "v2"
    authority["policy_sha256"] = owner.contract().digest(live)
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(**args)
    assert setup.state.writes == 1


def inventory_read_rotation(setup):
    previous, snapshot, _ = network_read_rotation(setup)
    authority = setup.doc["authority"]
    arn = authority["role_arn"].replace(":role/", ":policy/") + "-state-validation"
    live = copy.deepcopy(snapshot)
    live["managed"][arn]["version"] = "v2"
    live["managed"][arn]["document"]["Statement"][0]["Action"].append(
        "tag:GetResources"
    )
    setup.state.network_policy = live
    authority["policy_sha256"] = owner.contract().digest(live)
    return previous, snapshot, arn


def test_inventory_read_repair_is_conditional_and_lost_reply_safe(setup):
    previous, snapshot, arn = inventory_read_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_inventory_read_version="v2",
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args, check_only=False)
    assert setup.state.writes == 2
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2
    assert snapshot["managed"][arn]["version"] == "v1"
    setup.run(check_only=False, revoke=True)
    with pytest.raises(ValueError):
        setup.run(**args, check_only=False)


@pytest.mark.parametrize(
    "problem",
    [
        "extra-action",
        "resource",
        "region",
        "missing-region",
        "trust",
        "other-shard",
        "snapshot",
        "wrong-version",
        "old-version",
        "no-version",
        "no-previous",
        "no-snapshot",
        "binding",
        "beneficiary",
        "expiry",
        "generation",
        "absent",
        "revoked",
        "concurrent",
        "renew",
        "other-repair",
    ],
)
def test_inventory_read_repair_rejects_unreviewed_changes(setup, problem):
    previous, snapshot, arn = inventory_read_rotation(setup)
    authority = setup.doc["authority"]
    live = setup.state.network_policy
    statement = live["managed"][arn]["document"]["Statement"][0]
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_inventory_read_version="v2",
        check_only=False,
    )
    if problem == "extra-action":
        statement["Action"].append("tag:TagResources")
    elif problem == "resource":
        statement["Resource"] = authority["role_arn"]
    elif problem == "region":
        statement["Condition"]["StringEquals"]["aws:RequestedRegion"] = "us-east-2"
    elif problem == "missing-region":
        del statement["Condition"]
    elif problem == "trust":
        live["trust"]["Statement"].append(
            {"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}
        )
    elif problem == "other-shard":
        live["managed"][next(p for p in live["managed"] if p != arn)]["version"] = "v2"
    elif problem == "snapshot":
        snapshot["managed"][arn]["version"] = "v9"
    elif problem in {"wrong-version", "old-version", "no-version"}:
        args["repair_inventory_read_version"] = {
            "wrong-version": "v3",
            "old-version": "v1",
            "no-version": None,
        }[problem]
    elif problem == "no-previous":
        args["previous_document"] = None
    elif problem == "no-snapshot":
        args["previous_tag_policy"] = None
    elif problem == "binding":
        authority["binding_sha256"] = "f" * 64
    elif problem == "beneficiary":
        authority["user_id"] = "different"
    elif problem == "expiry":
        authority["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=3)
        ).isoformat()
    elif problem == "generation":
        authority["generation"] += 1
    elif problem == "absent":
        del setup.state.items[authority["credential_id"]]
    elif problem == "revoked":
        setup.state.items[authority["credential_id"]]["document"]["S"] = "{}"
    elif problem == "concurrent":
        setup.state.items[authority["credential_id"]]["revision"]["S"] = "f" * 64
    elif problem == "renew":
        args["renew"] = True
    elif problem == "other-repair":
        args["repair_network_reads_version"] = "v2"
    authority["policy_sha256"] = owner.contract().digest(live)
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(**args)
    assert setup.state.writes == 1


def state_history_rotation(setup):
    _, snapshot, arn = network_read_rotation(setup)
    schema = owner.contract()
    authority = setup.doc["authority"]
    prefix = (
        f"dev/modules/superplane-workspaces/v2/"
        f"{authority['org_id']}/{authority['workspace_id']}"
    )
    snapshot["managed"][arn]["document"]["Statement"].append(
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": f"arn:aws:s3:::adp-terraform-state-{authority['account_id']}",
            "Condition": {
                "StringLike": {
                    "s3:prefix": [
                        prefix + "/terraform.tfstate",
                        prefix + "/workspaces/*",
                    ]
                }
            },
        }
    )
    setup.state.network_policy = copy.deepcopy(snapshot)
    authority.update(generation=1, policy_sha256=schema.digest(snapshot))
    setup.state.items.pop(authority["credential_id"])
    setup.state.writes = 0
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    live = copy.deepcopy(snapshot)
    live["managed"][arn]["version"] = "v2"
    live["managed"][arn]["document"]["Statement"][-1]["Action"].append(
        "s3:ListBucketVersions"
    )
    setup.state.network_policy = live
    authority.update(generation=2, policy_sha256=schema.digest(live))
    return previous, snapshot, arn


def test_state_history_read_rotation_is_conditional_and_lost_reply_safe(setup):
    previous, snapshot, arn = state_history_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_state_history_read_version="v2",
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    assert setup.state.writes == 1
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args, check_only=False)
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2
    assert snapshot["managed"][arn]["version"] == "v1"
    setup.run(check_only=False, revoke=True)
    with pytest.raises(ValueError):
        setup.run(**args, check_only=False)


@pytest.mark.parametrize(
    "fault",
    [
        "write",
        "bucket",
        "prefix",
        "condition",
        "trust",
        "other-shard",
        "snapshot",
        "old-version",
        "binding",
        "expiry",
        "generation",
        "concurrent",
        "renew",
        "other-repair",
        "no-previous",
        "no-snapshot",
    ],
)
def test_state_history_read_rotation_refuses_unreviewed_changes(setup, fault):
    previous, snapshot, arn = state_history_rotation(setup)
    authority = setup.doc["authority"]
    live = setup.state.network_policy
    statement = live["managed"][arn]["document"]["Statement"][-1]
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_state_history_read_version="v2",
        check_only=False,
    )
    if fault == "write":
        statement["Action"].append("s3:DeleteObject")
    elif fault == "bucket":
        statement["Resource"] = "*"
    elif fault == "prefix":
        statement["Condition"]["StringLike"]["s3:prefix"] = ["*"]
    elif fault == "condition":
        del statement["Condition"]
    elif fault == "trust":
        live["trust"]["Statement"].append({"Effect": "Allow", "Principal": "*"})
    elif fault == "other-shard":
        live["managed"][next(p for p in live["managed"] if p != arn)]["version"] = "v2"
    elif fault == "snapshot":
        snapshot["managed"][arn]["version"] = "v9"
    elif fault == "old-version":
        args["repair_state_history_read_version"] = "v1"
    elif fault == "binding":
        authority["binding_sha256"] = "f" * 64
    elif fault == "expiry":
        authority["expires_at"] = "2099-01-01T00:00:00+00:00"
    elif fault == "generation":
        authority["generation"] += 1
    elif fault == "concurrent":
        setup.state.items[authority["credential_id"]]["revision"]["S"] = "f" * 64
    elif fault == "renew":
        args["renew"] = True
    elif fault == "other-repair":
        args["repair_inventory_read_version"] = "v2"
    elif fault == "no-previous":
        args["previous_document"] = None
    elif fault == "no-snapshot":
        args["previous_tag_policy"] = None
    authority["policy_sha256"] = owner.contract().digest(live)
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(**args)
    assert setup.state.writes == 1


def access_inventory_rotation(setup):
    _, snapshot, _ = network_read_rotation(setup)
    schema = owner.contract()
    authority = setup.doc["authority"]
    arn = authority["role_arn"].replace(":role/", ":policy/") + "-lifecycle"
    cluster = authority["role_arn"].rsplit("/", 1)[1].replace("-spp-", "-spw-")
    prefix = f"arn:aws:eks:{authority['region']}:{authority['account_id']}:access-entry/{cluster}/"
    condition = {
        "StringEquals": {"aws:PrincipalTag/adp:agent_id": "superplane-operation"}
    }
    statement = {
        "Effect": "Allow",
        "Action": [
            "eks:DescribeAccessEntry",
            "eks:ListAssociatedAccessPolicies",
            "eks:DeleteAccessEntry",
            "eks:DisassociateAccessPolicy",
        ],
        "Resource": [
            prefix + f"role/{authority['account_id']}/{actor}/*"
            for actor in ("installer", "registrar", "supervisor")
        ],
        "Condition": condition,
    }
    snapshot["managed"][arn]["document"]["Statement"] = [statement]
    setup.state.network_policy = copy.deepcopy(snapshot)
    authority.update(generation=1, policy_sha256=schema.digest(snapshot))
    setup.state.items.pop(authority["credential_id"])
    setup.state.writes = 0
    setup.run(check_only=False)
    previous = copy.deepcopy(setup.doc)
    live = copy.deepcopy(snapshot)
    live["managed"][arn]["version"] = "v2"
    live["managed"][arn]["document"]["Statement"][0]["Action"].remove(
        "eks:DescribeAccessEntry"
    )
    live["managed"][arn]["document"]["Statement"].append(
        {
            "Effect": "Allow",
            "Action": ["eks:DescribeAccessEntry"],
            "Resource": prefix + "*",
            "Condition": condition,
        }
    )
    setup.state.network_policy = live
    authority.update(generation=2, policy_sha256=schema.digest(live))
    return previous, snapshot, arn


def test_access_inventory_rotation_preserves_scope_and_reconciles_lost_response(setup):
    previous, snapshot, arn = access_inventory_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_access_inventory_version="v2",
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args, check_only=False)
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 2
    assert snapshot["managed"][arn]["version"] == "v1"


@pytest.mark.parametrize(
    "fault",
    [
        "write",
        "other-cluster",
        "node-mutation",
        "tag",
        "trust",
        "other-shard",
        "old-version",
        "renew",
        "other-repair",
        "concurrent",
        "snapshot",
    ],
)
def test_access_inventory_rotation_refuses_additional_changes(setup, fault):
    previous, snapshot, arn = access_inventory_rotation(setup)
    live = setup.state.network_policy
    statements = live["managed"][arn]["document"]["Statement"]
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_access_inventory_version="v2",
        check_only=False,
    )
    if fault == "write":
        statements[-1]["Action"].append("eks:DeleteAccessEntry")
    elif fault == "other-cluster":
        statements[-1]["Resource"] = "*"
    elif fault == "node-mutation":
        statements[0]["Resource"] = [statements[-1]["Resource"]]
    elif fault == "tag":
        del statements[-1]["Condition"]
    elif fault == "trust":
        live["trust"]["Statement"].append(
            {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": "*"}
        )
    elif fault == "other-shard":
        live["managed"][next(k for k in live["managed"] if k != arn)]["version"] = "v2"
    elif fault == "old-version":
        args["repair_access_inventory_version"] = "v1"
    elif fault == "renew":
        args["renew"] = True
    elif fault == "other-repair":
        args["repair_inventory_read_version"] = "v2"
    elif fault == "concurrent":
        setup.state.items[setup.doc["authority"]["credential_id"]]["revision"]["S"] = (
            "f" * 64
        )
    elif fault == "snapshot":
        snapshot["managed"][arn]["version"] = "v9"
    setup.doc["authority"]["policy_sha256"] = owner.contract().digest(live)
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(**args)
    assert setup.state.writes == 1


def access_entry_tag_rotation(setup):
    previous, snapshot, arn = access_inventory_rotation(setup)
    setup.run(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_access_inventory_version="v2",
        check_only=False,
    )
    previous = copy.deepcopy(setup.doc)
    snapshot = copy.deepcopy(setup.state.network_policy)
    live = copy.deepcopy(snapshot)
    live["managed"][arn]["version"] = "v3"
    live["managed"][arn]["document"]["Statement"][0]["Action"].append("eks:TagResource")
    setup.state.network_policy = live
    setup.doc["authority"].update(
        generation=3, policy_sha256=owner.contract().digest(live)
    )
    return previous, snapshot, arn


def test_access_entry_tags_rotation_preserves_scope_and_recovers_lost_reply(setup):
    previous, snapshot, arn = access_entry_tag_rotation(setup)
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_access_entry_tags_version="v3",
    )
    assert setup.run(**args)["state"] == "rotation-reviewed"
    setup.state.lose_reply = True
    with pytest.raises(RuntimeError):
        setup.run(**args, check_only=False)
    assert setup.run(**args, check_only=False)["state"] == "configured"
    assert setup.state.writes == 3
    assert snapshot["managed"][arn]["version"] == "v2"
    assert setup.doc["authority"]["expires_at"] == previous["authority"]["expires_at"]


@pytest.mark.parametrize(
    "fault",
    [
        "write",
        "other-cluster",
        "node-entry",
        "agent-tag",
        "trust",
        "other-shard",
        "old-version",
        "renew",
        "other-repair",
        "concurrent",
        "snapshot",
    ],
)
def test_access_entry_tags_rotation_refuses_additional_changes(setup, fault):
    previous, snapshot, arn = access_entry_tag_rotation(setup)
    live = setup.state.network_policy
    statement = live["managed"][arn]["document"]["Statement"][0]
    args = dict(
        previous_document=previous,
        previous_tag_policy=snapshot,
        repair_access_entry_tags_version="v3",
        check_only=False,
    )
    if fault == "write":
        statement["Action"].append("eks:UpdateAccessEntry")
    elif fault == "other-cluster":
        statement["Resource"] = "*"
    elif fault == "node-entry":
        statement["Resource"] = [
            statement["Resource"][0].rsplit("/", 2)[0] + "/node-role/*"
        ]
    elif fault == "agent-tag":
        del statement["Condition"]
    elif fault == "trust":
        live["trust"]["Statement"].append(
            {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": "*"}
        )
    elif fault == "other-shard":
        live["managed"][next(k for k in live["managed"] if k != arn)]["version"] = "v9"
    elif fault == "old-version":
        args["repair_access_entry_tags_version"] = "v2"
    elif fault == "renew":
        args["renew"] = True
    elif fault == "other-repair":
        args["repair_access_inventory_version"] = "v3"
    elif fault == "concurrent":
        setup.state.items[setup.doc["authority"]["credential_id"]]["revision"]["S"] = (
            "f" * 64
        )
    elif fault == "snapshot":
        snapshot["managed"][arn]["version"] = "v9"
    setup.doc["authority"]["policy_sha256"] = owner.contract().digest(live)
    with pytest.raises((ValueError, RuntimeError)):
        setup.run(**args)
    assert setup.state.writes == 2
