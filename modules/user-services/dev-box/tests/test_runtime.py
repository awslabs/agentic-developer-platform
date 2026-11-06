"""Exercise the real runtime verifier against isolated EC2 and IAM substitutes."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dev_box_runtime", MODULE_DIR / "runtime.py")
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)
RELEASE_SPEC = importlib.util.spec_from_file_location("dev_box_release", MODULE_DIR / "release.py")
RELEASE = importlib.util.module_from_spec(RELEASE_SPEC)
RELEASE_SPEC.loader.exec_module(RELEASE)
ACCOUNT = "000000000000"
GUEST = f"arn:aws:iam::{ACCOUNT}:role/example-guest"
CONTROLLER = f"arn:aws:iam::{ACCOUNT}:role/example-controller-role"
PROFILE = f"arn:aws:iam::{ACCOUNT}:instance-profile/example-guest"


def sign(manifest):
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Ec2Fixture:
    def __init__(self, manifest):
        self.expected_image_id = manifest["image_id"]
        self.calls = []
        self.images = [
            {
                "ImageId": manifest["image_id"],
                "OwnerId": ACCOUNT,
                "Tags": [
                    {"Key": "adp:component", "Value": "dev-box"},
                    {"Key": "adp:source-revision", "Value": manifest["source_revision"]},
                    {"Key": "adp:image-revision", "Value": manifest["image_revision"]},
                ],
                "BlockDeviceMappings": [{"Ebs": {"Encrypted": True}}],
            }
        ]
        self.version = [
            {
                "VersionNumber": 4,
                "LaunchTemplateData": {
                    "ImageId": manifest["image_id"],
                    "InstanceType": manifest["instance_size"],
                    "MetadataOptions": {"HttpTokens": "required"},
                    "IamInstanceProfile": {"Arn": PROFILE},
                    "NetworkInterfaces": [
                        {"AssociatePublicIpAddress": False, "SubnetId": manifest["private_subnet_id"], "Groups": ["sg-33333333333333333"]}
                    ],
                    "BlockDeviceMappings": [{"Ebs": {"Encrypted": True, "DeleteOnTermination": True}}],
                },
            }
        ]
        self.group = [
            {
                "GroupId": "sg-33333333333333333",
                "IpPermissions": [],
                "IpPermissionsEgress": [
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": group}]}
                    for group in (manifest["gateway_security_group_id"], manifest["ssm_endpoint_security_group_id"])
                ],
            }
        ]

    def describe_images(self, **kwargs):
        self.calls.append("describe_images")
        assert kwargs["ImageIds"] == [self.expected_image_id]
        return {"Images": self.images}

    def describe_launch_template_versions(self, **kwargs):
        self.calls.append("describe_launch_template_versions")
        assert kwargs["Versions"] == ["4"]
        return {"LaunchTemplateVersions": self.version}

    def describe_security_groups(self, **kwargs):
        self.calls.append("describe_security_groups")
        assert kwargs["GroupIds"] == [self.group[0]["GroupId"]]
        return {"SecurityGroups": self.group}


class IamFixture:
    def __init__(self, manifest):
        self.manifest = manifest
        self.calls = []
        prefix = f"arn:aws:ec2:us-east-1:{ACCOUNT}"
        request = {
            "StringEquals": {"aws:RequestTag/adp:deployment-id": manifest["deployment_id"], "aws:RequestTag/adp:component": "dev-box"},
            "Null": {"aws:RequestTag/adp:owner-id": "false"},
        }
        owned = {
            "StringEquals": {"aws:ResourceTag/adp:deployment-id": manifest["deployment_id"], "aws:ResourceTag/adp:component": "dev-box"},
            "Null": {"aws:ResourceTag/adp:owner-id": "false"},
        }
        specs = [
            (
                "Inventory",
                ["ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages", "ssm:DescribeInstanceInformation"],
                "*",
                {"StringEquals": {"aws:RequestedRegion": "us-east-1"}},
            ),
            (
                "LaunchApprovedInputs",
                ["ec2:RunInstances"],
                [
                    f"arn:aws:ec2:us-east-1::image/{manifest['image_id']}",
                    f"{prefix}:subnet/{manifest['private_subnet_id']}",
                    f"{prefix}:security-group/sg-33333333333333333",
                    f"{prefix}:launch-template/lt-0123456789abcdef0",
                ],
                {},
            ),
            (
                "LaunchOwnedInstance",
                ["ec2:RunInstances"],
                f"{prefix}:instance/*",
                {
                    **request,
                    "StringEquals": {
                        **request["StringEquals"],
                        "ec2:InstanceType": "t3.small",
                        "ec2:InstanceProfile": PROFILE,
                        "ec2:MetadataHttpTokens": "required",
                    },
                },
            ),
            (
                "LaunchOwnedInterface",
                ["ec2:RunInstances"],
                f"{prefix}:network-interface/*",
                {**request, "Bool": {"ec2:AssociatePublicIpAddress": "false"}},
            ),
            ("LaunchOwnedVolume", ["ec2:RunInstances"], f"{prefix}:volume/*", request),
            (
                "CreateOwnedDisk",
                ["ec2:CreateVolume"],
                f"{prefix}:volume/*",
                {
                    **request,
                    "Bool": {"ec2:Encrypted": "true"},
                    "NumericLessThanEquals": {"ec2:VolumeSize": 40},
                    "NumericGreaterThanEquals": {"ec2:VolumeSize": 8},
                },
            ),
            (
                "RestoreOwnedSnapshot",
                ["ec2:CreateVolume"],
                f"{prefix}:snapshot/*",
                owned,
            ),
            ("OperateOwnedInstances", ["ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances"], f"{prefix}:instance/*", owned),
            ("OperateOwnedDisks", ["ec2:AttachVolume", "ec2:DetachVolume", "ec2:DeleteVolume"], f"{prefix}:volume/*", owned),
            ("GrowOwnedDisk", ["ec2:ModifyVolume"], f"{prefix}:volume/*", owned),
            ("OperateOwnedAttachments", ["ec2:AttachVolume", "ec2:DetachVolume"], f"{prefix}:instance/*", owned),
            ("SendOwnedCommand", ["ssm:SendCommand"], "arn:aws:ssm:us-east-1::document/AWS-RunShellScript", {}),
            ("SendOwnedInstanceCommand", ["ssm:SendCommand"], f"{prefix}:instance/*", owned),
            ("ReadCommandResult", ["ssm:GetCommandInvocation"], "*", {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}),
            (
                "TagOnCreation",
                ["ec2:CreateTags"],
                [f"{prefix}:instance/*", f"{prefix}:volume/*", f"{prefix}:network-interface/*"],
                {**request, "StringEquals": {**request["StringEquals"], "ec2:CreateAction": ["RunInstances", "CreateVolume"]}},
            ),
            ("PassOnlyGuest", ["iam:PassRole"], GUEST, {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}}),
        ]
        service = {"StringEquals": {"kms:ViaService": "ec2.us-east-1.amazonaws.com", "kms:CallerAccount": ACCOUNT}}
        self.dropped_guest_policies = set()
        self.policies = {
            "owned-host-lifecycle": {
                "Statement": [
                    {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resource, "Condition": condition}
                    for sid, actions, resource, condition in specs
                ]
            },
            "selected-ebs-key-only": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["kms:DescribeKey", "kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncryptFrom", "kms:ReEncryptTo"],
                        "Resource": manifest["kms_key_arn"],
                        "Condition": service,
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["kms:CreateGrant"],
                        "Resource": manifest["kms_key_arn"],
                        "Condition": {**service, "Bool": {"kms:GrantIsForAWSResource": "true"}},
                    },
                ]
            },
        }

    def get_role(self, **kwargs):
        self.calls.append("get_role")
        guest = kwargs["RoleName"] == "example-guest"
        return {
            "Role": {
                "Arn": GUEST if guest else CONTROLLER,
                "Tags": [{"Key": "adp:component", "Value": "dev-box"}, {"Key": "adp:deployment-id", "Value": self.manifest["deployment_id"]}],
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": "ec2.amazonaws.com"} if guest else {"AWS": self.manifest["controller_principal_arn"]},
                        }
                    ]
                },
            }
        }

    def list_attached_role_policies(self, **kwargs):
        self.calls.append("list_attached_role_policies")
        if kwargs["RoleName"] == "example-guest":
            return {
                "AttachedPolicies": [
                    {"PolicyName": "AmazonSSMManagedInstanceCore", "PolicyArn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}
                ]
            }
        return {"AttachedPolicies": []}

    def list_role_policies(self, **kwargs):
        self.calls.append("list_role_policies")
        if kwargs["RoleName"] == "example-guest":
            names = (["approved-model-route-only"] if self.manifest.get("gateway_execute_arn") else []) + ["deny-guest-ec2-actions"]
            if self.manifest.get("crew_bootstrap") == "user-data":
                names.append("crew-bootstrap-inputs-read")
            return {"PolicyNames": [name for name in names if name not in self.dropped_guest_policies]}
        return {"PolicyNames": [name for name in self.policies if name not in ("approved-model-route-only", "crew-bootstrap-inputs-read")]}

    def get_role_policy(self, **kwargs):
        self.calls.append("get_role_policy")
        if kwargs["PolicyName"] == "deny-guest-ec2-actions" and kwargs["PolicyName"] not in self.policies:
            return {"PolicyDocument": {"Statement": [{"Effect": "Deny", "Action": "ec2:*", "Resource": "*"}]}}
        if kwargs["PolicyName"] == "crew-bootstrap-inputs-read" and kwargs["PolicyName"] not in self.policies:
            bucket = self.manifest["crew_bundle_s3_uri"][len("s3://"):].split("/", 1)[0]
            return {"PolicyDocument": {"Statement": [
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/dev-box/image-inputs/*"]}
            ]}}
        return {"PolicyDocument": self.policies[kwargs["PolicyName"]]}

    def get_instance_profile(self, **kwargs):
        self.calls.append("get_instance_profile")
        assert kwargs["InstanceProfileName"] == "example-guest"
        return {"InstanceProfile": {"Arn": PROFILE, "Roles": [{"Arn": GUEST}]}}


@pytest.fixture
def provider():
    dispatch = {
        "adp_correlation": "c" * 64,
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "b" * 40,
        "customer_aws_label": "example-connection",
        "customer_account_id": ACCOUNT,
        "environment": "dev",
        "region": "us-east-1",
        "image_id": "ami-0123456789abcdef0",
        "image_revision": "base-v1",
        "template_revision": "example-r1",
        "instance_size": "t3.small",
        "workspace_disk_gb": 40,
        "allowed_sizes": ["t3.small"],
        "max_disk_gb": 64,
        "infra_sha256": "d" * 64,
        "module_sha256": "e" * 64,
        "private_subnet_id": "subnet-0123456789abcdef0",
        "gateway_security_group_id": "sg-11111111111111111",
        "ssm_endpoint_security_group_id": "sg-22222222222222222",
        "controller_principal_arn": f"arn:aws:iam::{ACCOUNT}:role/example-controller",
        "deployment_id": "example-deployment",
        "kms_key_arn": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/00000000-0000-0000-0000-000000000000",
        "dashboard_enabled": False,
    }
    manifest = RELEASE.create_manifest(dispatch, "a" * 40, "b" * 40)
    outputs = {
        name: {"value": value}
        for name, value in {
            "launch_template_id": "lt-0123456789abcdef0",
            "launch_template_version": 4,
            "guest_security_group_id": "sg-33333333333333333",
            "guest_instance_profile_arn": PROFILE,
            "guest_role_arn": GUEST,
            "controller_role_arn": CONTROLLER,
        }.items()
    }
    return SimpleNamespace(manifest=manifest, outputs=outputs, ec2=Ec2Fixture(manifest), iam=IamFixture(manifest))


def test_approved_foundation_checks_provider_and_does_not_claim_controller_ready(provider):
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["image_id"] == provider.manifest["image_id"]
    assert receipt["launch_template_version"] == 4
    assert receipt["release_manifest_sha256"] == provider.manifest["manifest_sha256"]
    assert receipt["controller_status"] == "absent" and receipt["controller_ready"] is False
    assert provider.ec2.calls == ["describe_images", "describe_launch_template_versions", "describe_security_groups"]
    assert "get_role_policy" in provider.iam.calls


def test_present_controller_requires_digest_and_release_bound_health(provider):
    provider.manifest["controller_status"] = "present"
    provider.manifest["controller_digest"] = "sha256:" + "f" * 64
    sign(provider.manifest)
    controller = SimpleNamespace(
        observe=lambda: {
            "image_digest": provider.manifest["controller_digest"],
            "source_revision": provider.manifest["source_revision"],
            "release_sha256": provider.manifest["manifest_sha256"],
            "healthy": True,
        }
    )
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam, controller)
    assert receipt["controller_ready"] is True


@pytest.mark.parametrize(
    "tamper,expected",
    [
        ("image_identity", "AMI identity mismatch"),
        ("image_revision", "AMI source revision mismatch"),
        ("template_image", "launch template AMI mismatch"),
    ],
)
def test_wrong_ami_rejected_even_with_healthy_controller_observation(provider, tamper, expected):
    provider.manifest["controller_status"] = "present"
    provider.manifest["controller_digest"] = "sha256:" + "f" * 64
    sign(provider.manifest)
    controller = SimpleNamespace(
        observe=lambda: {
            "image_digest": provider.manifest["controller_digest"],
            "source_revision": provider.manifest["source_revision"],
            "release_sha256": provider.manifest["manifest_sha256"],
            "healthy": True,
            "status_code": 200,
        }
    )
    if tamper == "image_identity":
        provider.ec2.images[0]["ImageId"] = "ami-11111111111111111"
    elif tamper == "image_revision":
        provider.ec2.images[0]["Tags"][2]["Value"] = "wrong-version"
    else:
        provider.ec2.version[0]["LaunchTemplateData"]["ImageId"] = "ami-11111111111111111"
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam, controller)


@pytest.mark.parametrize(
    "observed,expected",
    [
        ({"image_digest": "sha256:" + "0" * 64, "healthy": True, "status_code": 200}, "controller digest mismatch"),
        ({"image_digest": "sha256:" + "f" * 64, "healthy": True, "status_code": 200}, "controller source mismatch"),
        (
            {"image_digest": "sha256:" + "f" * 64, "source_revision": "a" * 40, "healthy": True, "status_code": 200},
            "controller service identity mismatch",
        ),
        ({"status_code": 200}, "controller digest mismatch"),
    ],
)
def test_controller_digest_or_release_identity_required_despite_http_200(provider, observed, expected):
    provider.manifest["controller_status"] = "present"
    provider.manifest["controller_digest"] = "sha256:" + "f" * 64
    sign(provider.manifest)
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam, SimpleNamespace(observe=lambda: observed))


def test_missing_expected_controller_digest_cannot_assert_readiness(provider):
    provider.manifest["controller_status"] = "present"
    sign(provider.manifest)
    with pytest.raises(RUNTIME.VerificationError, match="controller digest missing or invalid"):
        RUNTIME.verify_release(
            provider.manifest, provider.outputs, provider.ec2, provider.iam, SimpleNamespace(observe=lambda: {"status_code": 200, "healthy": True})
        )


def profile_image(provider):
    provider.manifest["crew_profile_revision"] = "crew-r1"
    provider.ec2.images[0]["Tags"].append({"Key": "adp:profile-revision", "Value": "crew-r1"})


def test_gateway_only_dashboard_ingress_is_verified_when_enabled(provider):
    provider.manifest["dashboard_enabled"] = True
    profile_image(provider)
    sign(provider.manifest)
    provider.ec2.group[0]["IpPermissions"] = [
        {"IpProtocol": "tcp", "FromPort": 8443, "ToPort": 8443, "UserIdGroupPairs": [{"GroupId": provider.manifest["gateway_security_group_id"]}]}
    ]
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["controller_ready"] is False
    assert receipt["crew_profile_revision"] == "crew-r1"


@pytest.mark.parametrize(
    "permission",
    [
        {"IpProtocol": "tcp", "FromPort": 8443, "ToPort": 8443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "UserIdGroupPairs": [{"GroupId": "sg-44444444444444444"}]},
        {"IpProtocol": "tcp", "FromPort": 8443, "ToPort": 8443, "UserIdGroupPairs": [{"GroupId": "sg-44444444444444444"}]},
    ],
)
def test_broad_dashboard_ingress_rejected_even_when_health_is_good(provider, permission):
    provider.manifest["controller_status"] = "present"
    provider.manifest["controller_digest"] = "sha256:" + "f" * 64
    provider.manifest["dashboard_enabled"] = True
    profile_image(provider)
    sign(provider.manifest)
    provider.ec2.group[0]["IpPermissions"] = [permission]
    controller = SimpleNamespace(
        observe=lambda: {
            "image_digest": provider.manifest["controller_digest"],
            "source_revision": provider.manifest["source_revision"],
            "release_sha256": provider.manifest["manifest_sha256"],
            "healthy": True,
            "status_code": 200,
        }
    )
    with pytest.raises(RUNTIME.VerificationError, match="dashboard ingress mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam, controller)


def test_public_egress_is_rejected(provider):
    provider.ec2.group[0]["IpPermissionsEgress"].append({"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]})
    with pytest.raises(RUNTIME.VerificationError, match="private egress mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


@pytest.mark.parametrize(
    "tamper,expected",
    [
        ("new_action", "action scope mismatch"),
        ("wildcard_resource", "resource scope mismatch"),
        ("wildcard_region_resource", "resource scope mismatch"),
        ("inventory_global", "inventory region scope mismatch"),
        ("missing_owner", "owner scope missing"),
        ("foreign_deployment", "deployment scope mismatch"),
        ("broad_kms", "KMS key scope mismatch"),
        ("guest_admin", "guest role grants mismatch"),
    ],
)
def test_excess_iam_permissions_rejected(provider, tamper, expected):
    statements = provider.iam.policies["owned-host-lifecycle"]["Statement"]
    owned = next(statement for statement in statements if statement["Sid"] == "OperateOwnedInstances")
    if tamper == "new_action":
        owned["Action"].append("iam:CreateUser")
    elif tamper == "wildcard_resource":
        owned["Resource"] = "*"
    elif tamper == "wildcard_region_resource":
        owned["Resource"] = "arn:aws:ec2:*"
    elif tamper == "inventory_global":
        next(statement for statement in statements if statement["Sid"] == "Inventory")["Condition"] = {}
    elif tamper == "missing_owner":
        owned["Condition"]["Null"] = {}
    elif tamper == "foreign_deployment":
        owned["Condition"]["StringEquals"]["aws:ResourceTag/adp:deployment-id"] = "other-deployment"
    elif tamper == "broad_kms":
        provider.iam.policies["selected-ebs-key-only"]["Statement"][0]["Resource"] = "*"
    else:
        provider.iam.list_attached_role_policies = lambda **_: {
            "AttachedPolicies": [{"PolicyName": "AdministratorAccess", "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]
        }
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_guest_policy_rejects_broad_api_or_direct_bedrock_grants(provider):
    enable_host_policies(provider)
    expected = f"{provider.manifest['gateway_execute_arn']}/POST/agent/model/*/invoke"
    RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    for resource, actions in (
        ("*", ["execute-api:Invoke"]),
        (expected.replace("/POST/agent/model/*/invoke", "/*/*/agent/*"), ["execute-api:Invoke"]),
        (expected, ["execute-api:Invoke", "bedrock:InvokeModel"]),
        ("arn:aws:bedrock:us-east-1:000000000000:foundation-model/*", ["bedrock:InvokeModel"]),
    ):
        provider.iam.policies["approved-model-route-only"]["Statement"][0].update(Resource=resource, Action=actions)
        with pytest.raises(RUNTIME.VerificationError, match="guest gateway route scope mismatch"):
            RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


RUN_TABLE_KEY = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
# Exactly what infra/main.tf (aws_iam_role_policy.controller_run_registry) emits for a
# run registry encrypted with a customer KMS key (#8139).
RUN_TABLE_KEY_STATEMENT = {
    "Effect": "Allow",
    "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
    "Resource": RUN_TABLE_KEY,
    "Condition": {"StringEquals": {"kms:ViaService": "dynamodb.us-east-1.amazonaws.com"}},
}


GATEWAY_HTTPS_EGRESS = {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}


def enable_host_policies(provider, *, gateway=True, run_table_key=False):
    table = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    provider.manifest["run_table_arn"] = table
    provider.outputs["run_table_arn"] = {"value": table}
    provider.iam.policies["owner-bound-host-runs"] = {
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"],
                "Resource": table,
            }
        ]
    }
    if run_table_key:
        provider.manifest["run_table_kms_key_arn"] = RUN_TABLE_KEY
        provider.iam.policies["owner-bound-host-runs"]["Statement"].append(dict(RUN_TABLE_KEY_STATEMENT))
    if gateway:
        provider.manifest["gateway_execute_arn"] = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
        # The approved gateway comes with the one egress that lets the guest reach its
        # Regional REST API through NAT: HTTPS to the internet, nothing else (#8223).
        provider.ec2.group[0]["IpPermissionsEgress"].append(GATEWAY_HTTPS_EGRESS.copy())
        provider.iam.policies["approved-model-route-only"] = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["execute-api:Invoke"],
                    "Resource": [
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/model/*/invoke",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/model/*/invoke-with-response-stream",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/model-token/refresh",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/host-restore/context",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/host-restore/grant",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/host-restore/release",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/tools",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/host-checkpoint/begin",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/host-checkpoint/complete",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/github-token",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/aws-credentials",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/openai/v1/responses",
                        f"{provider.manifest['gateway_execute_arn']}/GET/agent/crew/orchestration/*",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/orchestration/flows/drafts",
                        f"{provider.manifest['gateway_execute_arn']}/POST/agent/crew/orchestration/flows/drafts/preview",
                    ],
                }
            ]
        }
    sign(provider.manifest)


@pytest.mark.parametrize("gateway", [False, True])
def test_host_release_records_and_verifies_exact_scoped_policies(provider, gateway):
    enable_host_policies(provider, gateway=gateway)
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["run_table_arn"] == provider.manifest["run_table_arn"]
    assert receipt["gateway_execute_arn"] == provider.manifest.get("gateway_execute_arn")


@pytest.mark.parametrize(
    "tamper,expected",
    [
        ("no_https_egress", "private egress mismatch"),
        ("all_ports_egress", "private egress mismatch"),
        ("all_protocols_egress", "private egress mismatch"),
        ("narrower_cidr_egress", "private egress mismatch"),
        ("ipv6_egress", "private egress mismatch"),
        ("duplicate_https_egress", "private egress mismatch"),
    ],
)
def test_approved_gateway_requires_exactly_one_https_egress_to_the_internet(provider, tamper, expected):
    """The Crew host reaches the gateway's Regional REST API only through NAT (#8223: the
    hostname resolves into generic EC2 space, so no prefix list can name it). The read-back
    requires exactly one tcp/443 egress to 0.0.0.0/0 next to the gateway, SSM and S3 rules,
    and refuses anything wider (ports, protocols, IPv6) or different."""
    enable_host_policies(provider, gateway=True)
    egress = provider.ec2.group[0]["IpPermissionsEgress"]
    if tamper == "no_https_egress":
        egress.pop()
    elif tamper == "all_ports_egress":
        egress[-1] = {"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
    elif tamper == "all_protocols_egress":
        egress[-1] = {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
    elif tamper == "narrower_cidr_egress":
        egress[-1] = {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "52.20.0.0/14"}]}
    elif tamper == "ipv6_egress":
        egress[-1] = {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}
    elif tamper == "duplicate_https_egress":
        egress.append(GATEWAY_HTTPS_EGRESS.copy())
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_without_an_approved_gateway_internet_https_egress_is_refused(provider):
    enable_host_policies(provider, gateway=False)
    provider.ec2.group[0]["IpPermissionsEgress"].append(GATEWAY_HTTPS_EGRESS.copy())
    with pytest.raises(RUNTIME.VerificationError, match="private egress mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


@pytest.mark.parametrize("gateway", [False, True])
def test_run_registry_key_release_accepts_exactly_the_dynamodb_scoped_kms_grant(provider, gateway):
    enable_host_policies(provider, gateway=gateway, run_table_key=True)
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["run_table_arn"] == provider.manifest["run_table_arn"]
    assert receipt["run_table_kms_key_arn"] == RUN_TABLE_KEY, "the gateway read-back (#8145) learns the key from the receipt"


def test_run_registry_key_in_manifest_requires_its_kms_grant(provider):
    enable_host_policies(provider, run_table_key=True)
    provider.iam.policies["owner-bound-host-runs"]["Statement"].pop()
    with pytest.raises(RUNTIME.VerificationError, match="controller run registry key scope mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_kms_grant_without_the_key_in_the_manifest_is_refused(provider):
    enable_host_policies(provider)
    provider.iam.policies["owner-bound-host-runs"]["Statement"].append(dict(RUN_TABLE_KEY_STATEMENT))
    with pytest.raises(RUNTIME.VerificationError, match="controller run registry key scope mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


@pytest.mark.parametrize(
    "tamper",
    ["other_key", "wildcard_key", "extra_action", "missing_action", "reordered_actions", "no_condition", "other_service",
     "other_region", "duplicate_statement", "deny_effect"],
)
def test_run_registry_key_release_rejects_any_other_kms_grant(provider, tamper):
    enable_host_policies(provider, run_table_key=True)
    statements = provider.iam.policies["owner-bound-host-runs"]["Statement"]
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
    else:
        key_statement["Effect"] = "Deny"
    with pytest.raises(RUNTIME.VerificationError, match="controller run registry key scope mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_run_registry_key_without_a_run_registry_is_refused(provider):
    provider.manifest["run_table_kms_key_arn"] = RUN_TABLE_KEY
    sign(provider.manifest)
    with pytest.raises(RUNTIME.VerificationError, match="run registry key requires run registry"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_run_registry_release_still_requires_the_dynamodb_statement_first(provider):
    enable_host_policies(provider, run_table_key=True)
    provider.iam.policies["owner-bound-host-runs"]["Statement"].reverse()
    with pytest.raises(RUNTIME.VerificationError, match="controller run registry scope mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    provider.iam.policies["owner-bound-host-runs"]["Statement"] = []
    with pytest.raises(RUNTIME.VerificationError, match="controller run registry scope mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


@pytest.mark.parametrize("tamper", ["missing_put", "missing_update", "unsupported_action", "broad_table", "extra_policy", "global_ssm"])
def test_host_release_rejects_wrong_item_permissions_and_command_scope(provider, tamper):
    enable_host_policies(provider)
    statement = provider.iam.policies["owner-bound-host-runs"]["Statement"][0]
    if tamper == "missing_put":
        statement["Action"].remove("dynamodb:PutItem")
    elif tamper == "missing_update":
        statement["Action"].remove("dynamodb:UpdateItem")
    elif tamper == "unsupported_action":
        statement["Action"].append("dynamodb:TransactWriteItems")
    elif tamper == "broad_table":
        statement["Resource"] = "*"
    elif tamper == "extra_policy":
        provider.iam.policies["unexpected"] = {"Statement": []}
    else:
        next(row for row in provider.iam.policies["owned-host-lifecycle"]["Statement"] if row["Sid"] == "ReadCommandResult")["Condition"] = {}
    with pytest.raises(RUNTIME.VerificationError):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_release_receipt_names_the_run_registry_key_exactly_as_the_manifest_does(provider):
    """#8145: the gateway's Dev Box read-back (DevBoxRuntimeEvidence.run_table_kms_key_arn) learns from
    the receipt whether the controller's owner-bound-host-runs policy may carry the DynamoDB-only KMS
    statement (#8139). An AWS-owned key yields no key; a customer key is carried exactly as the manifest names it."""
    enable_host_policies(provider)
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["run_table_kms_key_arn"] is None
    provider.manifest["run_table_kms_key_arn"] = RUN_TABLE_KEY
    provider.iam.policies["owner-bound-host-runs"]["Statement"].append(dict(RUN_TABLE_KEY_STATEMENT))
    sign(provider.manifest)
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["run_table_kms_key_arn"] == RUN_TABLE_KEY


@pytest.mark.parametrize("tag", [None, "crew-r2"])
def test_dashboard_release_rejects_missing_or_foreign_profile_image(provider, tag):
    provider.manifest["dashboard_enabled"] = True
    provider.manifest["crew_profile_revision"] = "crew-r1"
    sign(provider.manifest)
    if tag:
        provider.ec2.images[0]["Tags"].append({"Key": "adp:profile-revision", "Value": tag})
    with pytest.raises(RUNTIME.VerificationError, match="Crew profile AMI mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_dashboard_release_rejects_missing_profile_pin(provider):
    provider.manifest["dashboard_enabled"] = True
    provider.ec2.images[0]["Tags"].append({"Key": "adp:profile-revision", "Value": "crew-r1"})
    sign(provider.manifest)
    with pytest.raises(RUNTIME.VerificationError, match="Crew profile AMI mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


# --- Crew bootstrap modes (#8055) -------------------------------------------------
BUNDLE_SHA = "c" * 64
BUNDLE_URI = f"s3://adp-terraform-state-{ACCOUNT}/dev-box/image-inputs/{BUNDLE_SHA}/crew.tar.gz"
CREW_SOURCE = "b" * 40
S3_PREFIX_LIST = "pl-0123456789abcdef0"


def bootstrap_script(**overrides):
    values = {
        "bundle_uri": BUNDLE_URI, "sha": BUNDLE_SHA, "profile": "crew-r1", "source": CREW_SOURCE,
        "install": 'python3 "$WORK/adp/install_crew.py" --manifest "$WORK/profile.json" --bundle "$WORK/crew.tar.gz" '
                   '--expected-source-revision "$SOURCE_REVISION" --expected-profile-revision "$PROFILE_REVISION"',
        "extra": "",
    }
    values.update(overrides)
    return (
        "#!/bin/bash\nset -euo pipefail\n"
        f"export BUNDLE_URI='{values['bundle_uri']}'\nexport BUNDLE_SHA256='{values['sha']}'\n"
        f"export PROFILE_REVISION='{values['profile']}'\nexport SOURCE_REVISION='{values['source']}'\n"
        'echo "$BUNDLE_SHA256  $WORK/crew.tar.gz" | sha256sum -c --quiet -\n'
        f"{values['install']}\n{values['extra']}\n"
        "write_marker ok /run/adp/crew-bootstrap.json\n"
    )


def user_data_release(provider, script=None):
    """A user-data release: base AMI (no profile tag), rendered user data, S3 egress and the read grant."""
    provider.manifest["crew_profile_revision"] = "crew-r1"
    provider.manifest["crew_bootstrap"] = "user-data"
    provider.manifest["crew_bundle_s3_uri"] = BUNDLE_URI
    provider.manifest["crew_bundle_sha256"] = BUNDLE_SHA
    provider.manifest["crew_source_revision"] = CREW_SOURCE
    sign(provider.manifest)
    provider.ec2.version[0]["LaunchTemplateData"]["UserData"] = base64.b64encode((script or bootstrap_script()).encode()).decode()
    provider.ec2.group[0]["IpPermissionsEgress"].append(
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "PrefixListIds": [{"PrefixListId": S3_PREFIX_LIST}]}
    )
    provider.outputs["crew_bootstrap_s3_prefix_list_id"] = {"value": S3_PREFIX_LIST}


def test_user_data_bootstrap_accepts_the_base_image_with_pinned_user_data(provider):
    user_data_release(provider)
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["crew_bootstrap"] == "user-data" and receipt["crew_bundle_sha256"] == BUNDLE_SHA
    assert receipt["crew_profile_revision"] == "crew-r1"
    assert "get_role_policy" in provider.iam.calls


def test_user_data_bootstrap_still_accepts_an_image_carrying_the_same_profile(provider):
    user_data_release(provider)
    provider.ec2.images[0]["Tags"].append({"Key": "adp:profile-revision", "Value": "crew-r1"})
    assert RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)["crew_bootstrap"] == "user-data"


def test_user_data_bootstrap_rejects_an_image_with_another_profile(provider):
    user_data_release(provider)
    provider.ec2.images[0]["Tags"].append({"Key": "adp:profile-revision", "Value": "crew-r2"})
    with pytest.raises(RUNTIME.VerificationError, match="Crew profile AMI mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def test_image_mode_receipt_is_unchanged_and_rejects_stray_user_data_or_egress(provider):
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["crew_bootstrap"] == "image" and receipt["crew_bundle_sha256"] is None
    provider.ec2.version[0]["LaunchTemplateData"]["UserData"] = base64.b64encode(bootstrap_script().encode()).decode()
    with pytest.raises(RUNTIME.VerificationError, match="user data must not contain secrets"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    del provider.ec2.version[0]["LaunchTemplateData"]["UserData"]
    provider.ec2.group[0]["IpPermissionsEgress"].append(
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "PrefixListIds": [{"PrefixListId": S3_PREFIX_LIST}]}
    )
    with pytest.raises(RUNTIME.VerificationError, match="private egress mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


@pytest.mark.parametrize(
    "tamper,expected",
    [
        ("missing_user_data", "Crew user-data bootstrap is missing"),
        ("other_digest", "does not pin the bundle digest"),
        ("other_profile", "does not pin the profile revision"),
        ("other_source", "does not pin the source revision"),
        ("no_verify", "does not pin the digest verification"),
        ("no_expected_flags", "does not pin the source revision check"),
        ("access_key", "user data must not contain secrets"),
        ("private_key", "user data must not contain secrets"),
        ("missing_prefix_list_egress", "private egress mismatch"),
        ("cidr_egress", "private egress mismatch"),
        ("missing_read_grant", "guest inline grants mismatch"),
        ("broad_read_grant", "guest Crew bootstrap read scope mismatch"),
        ("missing_prefix_list_output", "missing Terraform crew_bootstrap_s3_prefix_list_id"),
    ],
)
def test_user_data_bootstrap_fails_closed_on_tampered_user_data_network_or_grant(provider, tamper, expected):
    user_data_release(provider)
    definition = provider.ec2.version[0]["LaunchTemplateData"]
    if tamper == "missing_user_data":
        del definition["UserData"]
    elif tamper == "other_digest":
        definition["UserData"] = base64.b64encode(bootstrap_script(sha="d" * 64).encode()).decode()
    elif tamper == "other_profile":
        definition["UserData"] = base64.b64encode(bootstrap_script(profile="crew-r2").encode()).decode()
    elif tamper == "other_source":
        definition["UserData"] = base64.b64encode(bootstrap_script(source="e" * 40).encode()).decode()
    elif tamper == "no_verify":
        definition["UserData"] = base64.b64encode(bootstrap_script().replace("sha256sum -c", "cat").encode()).decode()
    elif tamper == "no_expected_flags":
        definition["UserData"] = base64.b64encode(bootstrap_script(install='python3 "$WORK/adp/install_crew.py" --manifest m --bundle b').encode()).decode()
    elif tamper == "access_key":
        definition["UserData"] = base64.b64encode(bootstrap_script(extra="export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE").encode()).decode()
    elif tamper == "private_key":
        definition["UserData"] = base64.b64encode(bootstrap_script(extra="-----BEGIN RSA PRIVATE KEY-----").encode()).decode()
    elif tamper == "missing_prefix_list_egress":
        provider.ec2.group[0]["IpPermissionsEgress"].pop()
    elif tamper == "cidr_egress":
        provider.ec2.group[0]["IpPermissionsEgress"][-1] = {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
    elif tamper == "missing_read_grant":
        provider.iam.list_role_policies = lambda **kwargs: {"PolicyNames": []}
    elif tamper == "broad_read_grant":
        provider.iam.policies["crew-bootstrap-inputs-read"] = {"Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::adp-terraform-state-{ACCOUNT}/*"]}
        ]}
    elif tamper == "missing_prefix_list_output":
        del provider.outputs["crew_bootstrap_s3_prefix_list_id"]
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"crew_bundle_sha256": "not-hex"}, "requires the bundle digest"),
        ({"crew_source_revision": "short"}, "requires the bundle source revision"),
        ({"crew_bundle_s3_uri": f"s3://adp-terraform-state-{ACCOUNT}/dev-box/image-inputs/latest/crew.tar.gz"}, "digest-addressed bundle location"),
        ({"crew_profile_revision": None}, "requires a pinned profile"),
        ({"crew_bootstrap": "ssm"}, "unknown Crew bootstrap mode"),
    ],
)
def test_user_data_manifest_must_pin_profile_digest_location_and_source(provider, changes, expected):
    user_data_release(provider)
    for key, value in changes.items():
        if value is None:
            provider.manifest.pop(key)
        else:
            provider.manifest[key] = value
    sign(provider.manifest)
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)


def planned_provider(provider):
    """The provider fixture re-signed with a two-size, two-subnet plan and IAM that admits exactly it (#8423)."""
    manifest = dict(provider.manifest)
    manifest.pop("manifest_sha256")
    manifest["allowed_sizes"] = ["t3.small", "t3a.small"]
    manifest["fallback_sizes"] = ["t3.small", "t3a.small"]
    manifest["instance_hour_rates_usd"] = {"t3.small": "0.0208", "t3a.small": "0.0188"}
    manifest["private_subnets"] = [
        {"subnet_id": manifest["private_subnet_id"], "availability_zone": "us-east-1a"},
        {"subnet_id": "subnet-0fedcba9876543210", "availability_zone": "us-east-1b"},
    ]
    sign(manifest)
    iam = IamFixture(manifest)
    prefix = f"arn:aws:ec2:us-east-1:{ACCOUNT}"
    for statement in iam.policies["owned-host-lifecycle"]["Statement"]:
        if statement["Sid"] == "LaunchApprovedInputs":
            statement["Resource"] = statement["Resource"] + [f"{prefix}:subnet/subnet-0fedcba9876543210"]
        if statement["Sid"] == "LaunchOwnedInstance":
            statement["Condition"]["StringEquals"]["ec2:InstanceType"] = ["t3.small", "t3a.small"]
    return SimpleNamespace(manifest=manifest, outputs=provider.outputs, ec2=Ec2Fixture(manifest), iam=iam)


def lifecycle_statement(iam, sid):
    return next(item for item in iam.policies["owned-host-lifecycle"]["Statement"] if item["Sid"] == sid)


def test_launch_plan_is_verified_against_iam_and_recorded_in_the_receipt(provider):
    planned = planned_provider(provider)
    receipt = RUNTIME.verify_release(planned.manifest, planned.outputs, planned.ec2, planned.iam)
    assert receipt["fallback_sizes"] == ["t3.small", "t3a.small"]
    assert receipt["private_subnets"] == [planned.manifest["private_subnet_id"], "subnet-0fedcba9876543210"]
    # A manifest without a plan still verifies against the single-string condition Terraform wrote before #8423.
    receipt = RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
    assert receipt["fallback_sizes"] == ["t3.small"] and receipt["private_subnets"] == [provider.manifest["private_subnet_id"]]


@pytest.mark.parametrize("tamper, expected", [
    (lambda iam: lifecycle_statement(iam, "LaunchOwnedInstance")["Condition"]["StringEquals"].__setitem__("ec2:InstanceType", ["t3.small"]), "launch security"),
    (lambda iam: lifecycle_statement(iam, "LaunchOwnedInstance")["Condition"]["StringEquals"].__setitem__("ec2:InstanceType", ["t3.small", "t3a.small", "t3.large"]), "launch security"),
    (lambda iam: lifecycle_statement(iam, "LaunchApprovedInputs").__setitem__("Resource", lifecycle_statement(iam, "LaunchApprovedInputs")["Resource"][:-1]), "resource scope"),
])
def test_iam_that_admits_less_or_more_than_the_plan_is_refused(provider, tamper, expected):
    planned = planned_provider(provider)
    tamper(planned.iam)
    with pytest.raises(RUNTIME.VerificationError, match=expected):
        RUNTIME.verify_release(planned.manifest, planned.outputs, planned.ec2, planned.iam)


def test_dashboard_release_requires_the_guest_ec2_deny_policy(provider):
    # #8565/#8670: the guest role must carry deny-guest-ec2-actions; a stage without it is refused.
    provider.iam.dropped_guest_policies.add("deny-guest-ec2-actions")
    with pytest.raises(RUNTIME.VerificationError, match="guest inline grants mismatch"):
        RUNTIME.verify_release(provider.manifest, provider.outputs, provider.ec2, provider.iam)
