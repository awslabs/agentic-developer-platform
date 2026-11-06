"""A workflow receipt cannot stand in for a live private host and controller."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from src.orchestration.deployment_runtime_reader import PilotRuntimeReader
from src.orchestration.review_cycle import CycleBlockedError

ACCOUNT = "000000000000"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "ami-0123456789abcdef0"
TEMPLATE = "lt-0123456789abcdef0"
GUEST = f"arn:aws:iam::{ACCOUNT}:role/example-guest"
CONTROLLER = f"arn:aws:iam::{ACCOUNT}:role/example-controller"
PROFILE = f"arn:aws:iam::{ACCOUNT}:instance-profile/example-guest"
GROUP = "sg-33333333333333333"
GATEWAY = "sg-11111111111111111"


RUN_TABLE = f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/example-runs"
RUN_TABLE_KEY = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
# Exactly what infra/main.tf (aws_iam_role_policy.controller_run_registry) emits for a run
# registry encrypted with a customer KMS key (#8139); runtime.py accepts the same (#8144).
RUN_TABLE_KEY_STATEMENT = {
    "Effect": "Allow",
    "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
    "Resource": RUN_TABLE_KEY,
    "Condition": {"StringEquals": {"kms:ViaService": "dynamodb.us-east-1.amazonaws.com"}},
}


def release(*, ready=True, run_table_arn=None, run_table_kms_key_arn=None, gateway_execute_arn=None):
    receipt = {
        "schema_version": 1,
        "component": "dev-box",
        "release_manifest_sha256": "d" * 64,
        "source_revision": SHA,
        "definition_revision": SHA,
        "account_id": ACCOUNT,
        "region": "us-east-1",
        "module_sha256": "e" * 64,
        "image_id": IMAGE,
        "launch_template_id": TEMPLATE,
        "launch_template_version": 4,
        "guest_security_group_id": GROUP,
        "gateway_security_group_id": GATEWAY,
        "private_subnet_id": "subnet-0123456789abcdef0",
        "guest_instance_profile_arn": PROFILE,
        "guest_role_arn": GUEST,
        "controller_role_arn": CONTROLLER,
        "run_table_arn": run_table_arn,
        "run_table_kms_key_arn": run_table_kms_key_arn,
        "gateway_execute_arn": gateway_execute_arn,
        "dashboard_enabled": True,
        "controller_status": "present" if ready else "absent",
        "controller_digest": DIGEST if ready else None,
        "controller_ready": ready,
        "verified_at": datetime.now(UTC),
    }
    return ReleaseArtifact(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        source_revision=SHA,
        workflow_revision=SHA,
        workflow_path=".github/workflows/dev-box-deploy.yml",
        account_id=ACCOUNT,
        region="us-east-1",
        resource_id=TEMPLATE,
        component="dev-box",
        image_digest=DIGEST if ready else None,
        dev_box_receipt=receipt,
        assets={"dev-box-module.tar.gz": "e" * 64},
        produced_at=datetime.now(UTC),
    )


@pytest.fixture
def provider():
    template = {
        "ImageId": IMAGE,
        "MetadataOptions": {"HttpTokens": "required"},
        "IamInstanceProfile": {"Arn": PROFILE},
        "NetworkInterfaces": [{"AssociatePublicIpAddress": False, "Groups": [GROUP], "SubnetId": "subnet-0123456789abcdef0"}],
        "BlockDeviceMappings": [{"Ebs": {"Encrypted": True}}],
    }
    ec2 = SimpleNamespace(
        describe_images=lambda **kwargs: {"Images": [{"ImageId": IMAGE, "OwnerId": ACCOUNT, "Tags": [{"Key": "adp:source-revision", "Value": SHA}]}]},
        describe_launch_template_versions=lambda **kwargs: {"LaunchTemplateVersions": [{"VersionNumber": 4, "LaunchTemplateData": template}]},
        describe_security_groups=lambda **kwargs: {
            "SecurityGroups": [
                {
                    "GroupId": GROUP,
                    "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 8443, "ToPort": 8443, "UserIdGroupPairs": [{"GroupId": GATEWAY}]}],
                }
            ]
        },
    )
    iam = SimpleNamespace(
        get_role_policy=lambda **kwargs: {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Sid": "ReadCommandResult",
                        "Effect": "Allow",
                        "Action": ["ssm:GetCommandInvocation"],
                        "Resource": "*",
                        "Condition": {"StringEquals": {"aws:RequestedRegion": "us-east-1"}},
                    }
                ]
            }
        },
        get_instance_profile=lambda **kwargs: {"InstanceProfile": {"Arn": PROFILE, "Roles": [{"Arn": GUEST}]}},
        get_role=lambda **kwargs: {"Role": {"Arn": GUEST if kwargs["RoleName"] == "example-guest" else CONTROLLER}},
        list_attached_role_policies=lambda **kwargs: {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}]
            if kwargs["RoleName"] == "example-guest"
            else []
        },
        list_role_policies=lambda **kwargs: {
            "PolicyNames": [] if kwargs["RoleName"] == "example-guest" else ["owned-host-lifecycle", "selected-ebs-key-only"]
        },
    )
    scoped = SimpleNamespace(region_name="us-east-1", client=lambda name, **kwargs: {"ec2": ec2, "iam": iam}[name])
    observed = {"healthy": True, "image_digest": DIGEST, "source_revision": SHA, "release_sha256": "d" * 64}
    reader = PilotRuntimeReader(dev_box_controller=lambda scoped, receipt: observed)
    return SimpleNamespace(ec2=ec2, iam=iam, scoped=scoped, observed=observed, reader=reader, template=template)


def inspect(provider, artifact=None):
    return provider.reader.inspect(
        provider.scoped,
        {"LaunchTemplateId": TEMPLATE},
        None,
        artifact=artifact or release(),
        artifact_hash="f" * 64,
        evidence_ref="github/actions/runs/42/artifacts/77",
        environment="dev",
    )


def test_verified_controller_and_immutable_host_are_accepted(provider):
    result = inspect(provider)
    assert result.component == "dev-box" and result.image_digest == DIGEST
    assert result.host_image_id == IMAGE and result.host_template_id == TEMPLATE


def host_release(provider, *, gateway=True, run_table_key=False):
    artifact = release(
        run_table_arn=RUN_TABLE,
        run_table_kms_key_arn=RUN_TABLE_KEY if run_table_key else None,
        gateway_execute_arn=f"arn:aws:execute-api:us-east-1:{ACCOUNT}:exampleapi/dev" if gateway else None,
    )
    receipt = artifact.dev_box_receipt
    run_statements = [
        {
            "Effect": "Allow",
            "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"],
            "Resource": receipt.run_table_arn,
        }
    ]
    if run_table_key:
        run_statements.append(dict(RUN_TABLE_KEY_STATEMENT))
    policies = {
        "owned-host-lifecycle": provider.iam.get_role_policy()["PolicyDocument"],
        "owner-bound-host-runs": {"Statement": run_statements},
        "approved-model-route-only": {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["execute-api:Invoke"],
                    "Resource": [
                        f"{receipt.gateway_execute_arn}/POST/agent/model/*/invoke",
                        f"{receipt.gateway_execute_arn}/POST/agent/crew/model-token/refresh",
                        f"{receipt.gateway_execute_arn}/POST/agent/crew/host-restore/grant",
                        f"{receipt.gateway_execute_arn}/POST/agent/crew/host-restore/release",
                    ],
                }
            ]
        },
    }
    provider.iam.get_role_policy = lambda **kwargs: {"PolicyDocument": policies[kwargs["PolicyName"]]}
    provider.iam.list_role_policies = lambda **kwargs: {
        "PolicyNames": (["approved-model-route-only"] if gateway else [])
        if kwargs["RoleName"] == "example-guest"
        else ["owned-host-lifecycle", "selected-ebs-key-only", "owner-bound-host-runs"]
    }
    return artifact, policies


@pytest.mark.parametrize("gateway", [False, True])
def test_host_enabled_release_is_accepted_with_exact_policy_scopes(provider, gateway):
    artifact, _ = host_release(provider, gateway=gateway)
    ReleaseArtifact.model_validate(artifact.model_dump())
    assert inspect(provider, artifact).healthy


@pytest.mark.parametrize("tamper", ["table", "action", "gateway", "bedrock", "command_region", "extra_policy"])
def test_host_policy_drift_is_rejected(provider, tamper):
    artifact, policies = host_release(provider)
    run = policies["owner-bound-host-runs"]["Statement"][0]
    guest = policies["approved-model-route-only"]["Statement"][0]
    if tamper == "table":
        run["Resource"] = "*"
    elif tamper == "action":
        run["Action"].remove("dynamodb:UpdateItem")
    elif tamper == "gateway":
        guest["Resource"] = "*"
    elif tamper == "bedrock":
        guest["Action"].append("bedrock:InvokeModel")
    elif tamper == "command_region":
        policies["owned-host-lifecycle"]["Statement"][0]["Condition"] = {}
    else:
        provider.iam.list_role_policies = lambda **kwargs: {"PolicyNames": ["unexpected"]}
    with pytest.raises(CycleBlockedError, match="deployment_dev_box_"):
        inspect(provider, artifact)


@pytest.mark.parametrize("gateway", [False, True])
def test_encrypted_run_registry_release_accepts_exactly_the_dynamodb_scoped_kms_grant(provider, gateway):
    """#8145: the receipt names the registry's customer key, so the controller's policy may carry
    the one DynamoDB-only KMS statement Terraform writes (#8139) in addition to the table grant."""
    artifact, _ = host_release(provider, gateway=gateway, run_table_key=True)
    ReleaseArtifact.model_validate(artifact.model_dump())
    assert artifact.dev_box_receipt.run_table_kms_key_arn == RUN_TABLE_KEY
    assert inspect(provider, artifact).healthy


def test_named_run_registry_key_requires_its_kms_grant(provider):
    artifact, policies = host_release(provider, run_table_key=True)
    policies["owner-bound-host-runs"]["Statement"].pop()
    with pytest.raises(CycleBlockedError, match="deployment_dev_box_run_key_policy_changed"):
        inspect(provider, artifact)


def test_kms_grant_the_receipt_does_not_name_is_refused(provider):
    artifact, policies = host_release(provider)
    policies["owner-bound-host-runs"]["Statement"].append(dict(RUN_TABLE_KEY_STATEMENT))
    with pytest.raises(CycleBlockedError, match="deployment_dev_box_run_key_policy_changed"):
        inspect(provider, artifact)


@pytest.mark.parametrize(
    "tamper",
    [
        "other_key",
        "wildcard_key",
        "extra_action",
        "missing_action",
        "reordered_actions",
        "no_condition",
        "other_service",
        "other_region",
        "duplicate_statement",
        "deny_effect",
        "extra_field",
    ],
)
def test_encrypted_run_registry_release_rejects_any_other_kms_grant(provider, tamper):
    artifact, policies = host_release(provider, run_table_key=True)
    statements = policies["owner-bound-host-runs"]["Statement"]
    key_statement = statements[1]
    if tamper == "other_key":
        key_statement["Resource"] = RUN_TABLE_KEY.replace("1111", "9999")
    elif tamper == "wildcard_key":
        key_statement["Resource"] = "*"
    elif tamper == "extra_action":
        key_statement["Action"] = key_statement["Action"] + ["kms:CreateGrant"]
    elif tamper == "missing_action":
        key_statement["Action"] = key_statement["Action"][:-1]
    elif tamper == "reordered_actions":
        key_statement["Action"] = list(reversed(key_statement["Action"]))
    elif tamper == "no_condition":
        del key_statement["Condition"]
    elif tamper == "other_service":
        key_statement["Condition"] = {"StringEquals": {"kms:ViaService": "ec2.us-east-1.amazonaws.com"}}
    elif tamper == "other_region":
        key_statement["Condition"] = {"StringEquals": {"kms:ViaService": "dynamodb.us-west-2.amazonaws.com"}}
    elif tamper == "duplicate_statement":
        statements.append(dict(RUN_TABLE_KEY_STATEMENT))
    elif tamper == "deny_effect":
        key_statement["Effect"] = "Deny"
    else:
        key_statement["Principal"] = "*"
    with pytest.raises(CycleBlockedError, match="deployment_dev_box_run_key_policy_changed"):
        inspect(provider, artifact)


def test_encrypted_run_registry_release_still_requires_the_dynamodb_statement_first(provider):
    artifact, policies = host_release(provider, run_table_key=True)
    policies["owner-bound-host-runs"]["Statement"].reverse()
    with pytest.raises(CycleBlockedError, match="deployment_dev_box_run_policy_changed"):
        inspect(provider, artifact)
    for statements in ([], None, "Allow"):
        policies["owner-bound-host-runs"]["Statement"] = statements
        with pytest.raises(CycleBlockedError, match="deployment_dev_box_run_policy_changed"):
            inspect(provider, artifact)


def test_receipt_written_before_the_run_registry_key_field_is_still_accepted(provider):
    """Receipts from an older runtime.py (or an AWS-owned key) carry no run_table_kms_key_arn at all."""
    artifact, _ = host_release(provider)
    payload = artifact.model_dump()
    del payload["dev_box_receipt"]["run_table_kms_key_arn"]
    legacy = ReleaseArtifact.model_validate(payload)
    assert legacy.dev_box_receipt.run_table_kms_key_arn is None
    assert inspect(provider, legacy).healthy


@pytest.mark.parametrize(
    "tamper",
    ["foreign_account", "foreign_region", "alias", "wildcard", "bare_uuid", "partition", "unknown_field", "key_without_registry"],
)
def test_host_release_contract_rejects_unscoped_run_registry_keys(provider, tamper):
    from pydantic import ValidationError

    artifact, _ = host_release(provider, run_table_key=True)
    payload = artifact.model_dump()
    receipt = payload["dev_box_receipt"]
    if tamper == "foreign_account":
        receipt["run_table_kms_key_arn"] = RUN_TABLE_KEY.replace(ACCOUNT, "111111111111")
    elif tamper == "foreign_region":
        receipt["run_table_kms_key_arn"] = RUN_TABLE_KEY.replace("us-east-1", "us-west-2")
    elif tamper == "alias":
        receipt["run_table_kms_key_arn"] = f"arn:aws:kms:us-east-1:{ACCOUNT}:alias/example-runs"
    elif tamper == "wildcard":
        receipt["run_table_kms_key_arn"] = "*"
    elif tamper == "bare_uuid":
        receipt["run_table_kms_key_arn"] = RUN_TABLE_KEY.rsplit("/", 1)[-1]
    elif tamper == "partition":
        receipt["run_table_kms_key_arn"] = RUN_TABLE_KEY.replace("arn:aws:", "arn:aws-cn:")
    elif tamper == "unknown_field":
        receipt["run_table_kms_key_alias"] = receipt["run_table_kms_key_arn"]
    else:
        receipt["run_table_arn"] = None
        receipt["gateway_execute_arn"] = None
    with pytest.raises(ValidationError):
        ReleaseArtifact.model_validate(payload)


@pytest.mark.parametrize("tamper", ["foreign_table", "foreign_gateway", "wildcard_table", "wildcard_gateway", "missing_table"])
def test_host_release_contract_rejects_unscoped_authority(provider, tamper):
    from pydantic import ValidationError

    artifact, _ = host_release(provider)
    payload = artifact.model_dump()
    receipt = payload["dev_box_receipt"]
    if tamper == "missing_table":
        receipt["run_table_arn"] = None
    else:
        field = "run_table_arn" if "table" in tamper else "gateway_execute_arn"
        receipt[field] = receipt[field].replace(ACCOUNT, "111111111111") if tamper.startswith("foreign") else "*"
    with pytest.raises(ValidationError):
        ReleaseArtifact.model_validate(payload)


def test_absent_or_unobserved_controller_never_claims_ready(provider):
    with pytest.raises(CycleBlockedError, match="controller_unready"):
        inspect(provider, release(ready=False))
    provider.reader.dev_box_controller = None
    with pytest.raises(CycleBlockedError, match="controller_unready"):
        inspect(provider)


@pytest.mark.parametrize("failure", ["digest", "service", "subnet", "public", "ingress", "role"])
def test_green_workflow_cannot_hide_changed_provider_state(provider, failure):
    if failure == "digest":
        provider.observed["image_digest"] = "sha256:" + "c" * 64
    elif failure == "service":
        provider.observed["healthy"] = False
    elif failure == "subnet":
        provider.template["NetworkInterfaces"][0]["SubnetId"] = "subnet-99999999999999999"
    elif failure == "public":
        provider.template["NetworkInterfaces"][0]["AssociatePublicIpAddress"] = True
    elif failure == "ingress":
        provider.ec2.describe_security_groups = lambda **kwargs: {
            "SecurityGroups": [
                {"GroupId": GROUP, "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 8443, "ToPort": 8443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]}
            ]
        }
    else:
        provider.iam.list_attached_role_policies = lambda **kwargs: {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]
        }
    with pytest.raises(CycleBlockedError, match="deployment_dev_box_"):
        inspect(provider)
