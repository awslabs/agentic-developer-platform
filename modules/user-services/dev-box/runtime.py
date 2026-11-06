"""Read back an exact Dev Box release; health alone never establishes identity."""

import argparse
import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path


class VerificationError(ValueError):
    pass


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise VerificationError(detail)


def _output(outputs: dict, name: str) -> str:
    value = outputs.get(name, {}).get("value")
    require(isinstance(value, str) and bool(value), f"missing Terraform {name}")
    return value


def _permissions(permissions: list, expected: set[tuple[int, str]]) -> bool:
    """True when the group's rules are exactly ``expected`` (port, source) pairs.

    EC2 reports rules that share protocol and port range as ONE ``IpPermission``
    carrying several ``UserIdGroupPairs`` (the two 443 egress rules of the guest
    group come back merged), so pairs are flattened before comparing. A source is a
    security group id or, for the S3 gateway endpoint the user-data Crew bootstrap
    reads from (#8055), a managed prefix list id; the caller names which ones it
    expects. An IPv4 CIDR is a source too, so the caller can expect exactly the one
    internet HTTPS egress an approved gateway adds (#8223); an unexpected CIDR, any IPv6
    source, non-TCP protocol, port range, duplicate pair or unexpected pair field fails
    the check.
    """
    actual: list[tuple[int, str]] = []
    for permission in permissions:
        if permission.get("IpProtocol") != "tcp" or permission.get("FromPort") != permission.get("ToPort"):
            return False
        if permission.get("Ipv6Ranges"):
            return False
        sources = permission.get("UserIdGroupPairs", [])
        prefix_lists = permission.get("PrefixListIds", [])
        cidrs = permission.get("IpRanges", [])
        if not sources and not prefix_lists and not cidrs:
            return False
        for cidr in cidrs:
            if set(cidr) - {"CidrIp", "Description"}:
                return False
            actual.append((permission["FromPort"], cidr.get("CidrIp")))
        for source in sources:
            if set(source) - {"GroupId", "UserId", "VpcId", "Description", "VpcPeeringConnectionId", "PeeringStatus", "GroupName"}:
                return False
            actual.append((permission["FromPort"], source.get("GroupId")))
        for prefix_list in prefix_lists:
            if set(prefix_list) - {"PrefixListId", "Description"}:
                return False
            actual.append((permission["FromPort"], prefix_list.get("PrefixListId")))
    return len(actual) == len(set(actual)) == len(expected) and set(actual) == expected


# Patterns no first-boot script may carry; the bootstrap is reviewed source rendered
# with object paths, digests and revisions only (crew_bootstrap.tf).
USER_DATA_SECRET_PATTERNS = (
    r"AKIA[0-9A-Z]{16}", r"ASIA[0-9A-Z]{16}", r"(?i)aws_secret_access_key", r"(?i)session_token",
    r"(?i)x-amz-security-token", r"PRIVATE KEY", r"(?i)bearer ", r"(?i)api[_-]?key",
)


def _crew_bootstrap(manifest: dict) -> str:
    mode = manifest.get("crew_bootstrap", "image")
    require(mode in ("image", "user-data"), "unknown Crew bootstrap mode")
    if mode == "user-data":
        require(bool(manifest.get("crew_profile_revision")), "user-data Crew bootstrap requires a pinned profile")
        require(isinstance(manifest.get("crew_bundle_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", manifest["crew_bundle_sha256"]) is not None,
                "user-data Crew bootstrap requires the bundle digest")
        require(isinstance(manifest.get("crew_source_revision"), str) and re.fullmatch(r"[0-9a-f]{40}", manifest["crew_source_revision"]) is not None,
                "user-data Crew bootstrap requires the bundle source revision")
        uri = manifest.get("crew_bundle_s3_uri")
        require(isinstance(uri, str) and uri.startswith("s3://") and f"/dev-box/image-inputs/{manifest['crew_bundle_sha256']}/crew.tar.gz" in uri,
                "user-data Crew bootstrap requires the digest-addressed bundle location")
    return mode


def _bootstrap_bucket(manifest: dict) -> str:
    return manifest["crew_bundle_s3_uri"][len("s3://"):].split("/", 1)[0]


def _user_data(definition: dict, manifest: dict, mode: str) -> None:
    encoded = definition.get("UserData")
    if mode != "user-data":
        require(not encoded, "user data must not contain secrets")
        return
    require(isinstance(encoded, str) and bool(encoded), "Crew user-data bootstrap is missing from the launch template")
    try:
        script = base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise VerificationError("Crew bootstrap user data is not a rendered script") from error
    for needle, label in (
        (f"BUNDLE_SHA256='{manifest['crew_bundle_sha256']}'", "bundle digest"),
        (f"BUNDLE_URI='{manifest['crew_bundle_s3_uri']}'", "bundle location"),
        (f"PROFILE_REVISION='{manifest['crew_profile_revision']}'", "profile revision"),
        (f"SOURCE_REVISION='{manifest['crew_source_revision']}'", "source revision"),
        ("sha256sum -c", "digest verification"),
        ("install_crew.py", "installer"),
        ("--expected-source-revision", "source revision check"),
        ("--expected-profile-revision", "profile revision check"),
        ("/run/adp/crew-bootstrap.json", "bootstrap receipt"),
    ):
        require(needle in script, f"Crew bootstrap user data does not pin the {label}")
    for pattern in USER_DATA_SECRET_PATTERNS:
        require(re.search(pattern, script) is None, "user data must not contain secrets")


def _role(iam, role_arn: str, principal: dict, manifest: dict) -> str:
    account = manifest["account_id"]
    require(role_arn.startswith(f"arn:aws:iam::{account}:role/"), "role account mismatch")
    name = role_arn.rsplit("/", 1)[1]
    role = iam.get_role(RoleName=name)["Role"]
    require(role.get("Arn") == role_arn, "role identity mismatch")
    tags = {item["Key"]: item["Value"] for item in role.get("Tags", [])}
    require(tags.get("adp:component") == "dev-box" and tags.get("adp:deployment-id") == manifest["deployment_id"], "role ownership mismatch")
    require(
        role.get("AssumeRolePolicyDocument", {}).get("Statement") == [{"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": principal}],
        "role trust mismatch",
    )
    return name


LIFECYCLE_ACTIONS = {
    "Inventory": {"ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages", "ssm:DescribeInstanceInformation"},
    "LaunchApprovedInputs": {"ec2:RunInstances"},
    "LaunchOwnedInstance": {"ec2:RunInstances"},
    "LaunchOwnedInterface": {"ec2:RunInstances"},
    "LaunchOwnedVolume": {"ec2:RunInstances"},
    "CreateOwnedDisk": {"ec2:CreateVolume"},
    # #8565: a Replace may restore the owner's disk from one of their own tagged snapshots.
    "RestoreOwnedSnapshot": {"ec2:CreateVolume"},
    "OperateOwnedInstances": {"ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances"},
    "OperateOwnedDisks": {"ec2:AttachVolume", "ec2:DetachVolume", "ec2:DeleteVolume"},
    # #8835 disk growth: the controller enlarges an owned retained disk in place.
    "GrowOwnedDisk": {"ec2:ModifyVolume"},
    "OperateOwnedAttachments": {"ec2:AttachVolume", "ec2:DetachVolume"},
    "SendOwnedCommand": {"ssm:SendCommand"},
    "SendOwnedInstanceCommand": {"ssm:SendCommand"},
    "ReadCommandResult": {"ssm:GetCommandInvocation"},
    "TagOnCreation": {"ec2:CreateTags"},
    "PassOnlyGuest": {"iam:PassRole"},
}


def _launch_plan(manifest: dict) -> tuple[list, list]:
    """The manifest's ordered sizes and approved subnets (#8423); without a plan, exactly the template's own."""
    plan_subnets = [item["subnet_id"] for item in manifest.get("private_subnets") or [{"subnet_id": manifest["private_subnet_id"]}]]
    plan_sizes = list(manifest.get("fallback_sizes") or [manifest["instance_size"]])
    require(plan_subnets[0] == manifest["private_subnet_id"] and plan_sizes[0] == manifest["instance_size"], "launch plan mismatch")
    return plan_sizes, plan_subnets


def _iam(manifest: dict, outputs: dict, iam) -> None:
    guest_arn = _output(outputs, "guest_role_arn")
    controller_arn = _output(outputs, "controller_role_arn")
    guest = _role(iam, guest_arn, {"Service": "ec2.amazonaws.com"}, manifest)
    controller = _role(iam, controller_arn, {"AWS": manifest["controller_principal_arn"]}, manifest)
    guest_policies = iam.list_attached_role_policies(RoleName=guest)
    require(
        not guest_policies.get("IsTruncated")
        and guest_policies.get("AttachedPolicies")
        == [{"PolicyName": "AmazonSSMManagedInstanceCore", "PolicyArn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}],
        "guest role grants mismatch",
    )
    guest_inline = iam.list_role_policies(RoleName=guest)
    gateway_arn = manifest.get("gateway_execute_arn")
    run_table_arn = manifest.get("run_table_arn")
    require(not gateway_arn or bool(run_table_arn), "guest gateway requires run registry")
    # deny-guest-ec2-actions (#8565): the box's own role may never read, create or delete snapshots —
    # or any EC2 resource — the controller and DLM own that plane. #8670 added the policy in Terraform
    # without teaching this verifier, so the first stage after it refused "guest inline grants mismatch".
    expected_guest_policies = (["approved-model-route-only"] if gateway_arn else []) + ["deny-guest-ec2-actions"]
    if manifest.get("crew_bootstrap") == "user-data":
        expected_guest_policies.append("crew-bootstrap-inputs-read")
    require(not guest_inline.get("IsTruncated") and sorted(guest_inline.get("PolicyNames") or []) == sorted(expected_guest_policies), "guest inline grants mismatch")
    deny = iam.get_role_policy(RoleName=guest, PolicyName="deny-guest-ec2-actions")["PolicyDocument"]["Statement"]
    require(
        any(statement.get("Effect") == "Deny" and statement.get("Action") in ("ec2:*", ["ec2:*"]) and statement.get("Resource") in ("*", ["*"]) for statement in deny),
        "guest EC2 deny missing",
    )
    if manifest.get("crew_bootstrap") == "user-data":
        bootstrap_policy = iam.get_role_policy(RoleName=guest, PolicyName="crew-bootstrap-inputs-read")["PolicyDocument"]
        require(
            bootstrap_policy.get("Statement")
            == [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{_bootstrap_bucket(manifest)}/dev-box/image-inputs/*"]}],
            "guest Crew bootstrap read scope mismatch",
        )
    if gateway_arn:
        policy = iam.get_role_policy(RoleName=guest, PolicyName="approved-model-route-only")["PolicyDocument"]
        require(
            policy.get("Statement")
            == [
                {
                    "Effect": "Allow",
                    "Action": ["execute-api:Invoke"],
                    "Resource": [
                        f"{gateway_arn}/POST/agent/model/*/invoke",
                        f"{gateway_arn}/POST/agent/model/*/invoke-with-response-stream",
                        f"{gateway_arn}/POST/agent/crew/model-token/refresh",
                        f"{gateway_arn}/POST/agent/crew/host-restore/context",
                        f"{gateway_arn}/POST/agent/crew/host-restore/grant",
                        f"{gateway_arn}/POST/agent/crew/host-restore/release",
                        f"{gateway_arn}/POST/agent/crew/tools",
                        f"{gateway_arn}/POST/agent/crew/host-checkpoint/begin",
                        f"{gateway_arn}/POST/agent/crew/host-checkpoint/complete",
                        # #8555: the owner's GitHub token for the box's gh/git, same host proof as the model route.
                        f"{gateway_arn}/POST/agent/crew/github-token",
                        f"{gateway_arn}/POST/agent/crew/aws-credentials",
                        # Codex on GPT models: the adapter forwards Responses requests under the host proof (2026-10-26).
                        f"{gateway_arn}/POST/agent/openai/v1/responses",
                        # #9485/#9528: owner-scoped host-run orchestration reads and the two inert draft routes
                        # for the Dev Box adp CLI; no gate, execution or amendment route.
                        f"{gateway_arn}/GET/agent/crew/orchestration/*",
                        f"{gateway_arn}/POST/agent/crew/orchestration/flows/drafts",
                        f"{gateway_arn}/POST/agent/crew/orchestration/flows/drafts/preview",
                    ],
                }
            ],
            "guest gateway route scope mismatch",
        )
    controller_attached = iam.list_attached_role_policies(RoleName=controller)
    require(not controller_attached.get("IsTruncated") and not controller_attached.get("AttachedPolicies"), "controller managed grants mismatch")
    policies = iam.list_role_policies(RoleName=controller)
    expected_controller_policies = {"owned-host-lifecycle", "selected-ebs-key-only"}
    if run_table_arn:
        expected_controller_policies.add("owner-bound-host-runs")
    require(not policies.get("IsTruncated") and set(policies.get("PolicyNames", [])) == expected_controller_policies, "controller policies mismatch")
    if run_table_arn:
        require(_output(outputs, "run_table_arn") == run_table_arn, "run registry output mismatch")
        run_policy = iam.get_role_policy(RoleName=controller, PolicyName="owner-bound-host-runs")["PolicyDocument"]
        run_statements = run_policy.get("Statement")
        require(isinstance(run_statements, list) and bool(run_statements), "controller run registry scope mismatch")
        require(
            run_statements[0]
            == {
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"],
                "Resource": run_table_arn,
            },
            "controller run registry scope mismatch",
        )
        # A registry encrypted with a customer KMS key (#8139) adds exactly one KMS statement,
        # usable through DynamoDB only, and only when the manifest names that key. A KMS grant
        # the manifest does not name, or a named key without its grant, fails closed; the
        # action order is the one infra/main.tf (aws_iam_role_policy.controller_run_registry) emits.
        run_table_key = manifest.get("run_table_kms_key_arn")
        expected_key_statements = (
            [
                {
                    "Effect": "Allow",
                    "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    "Resource": run_table_key,
                    "Condition": {"StringEquals": {"kms:ViaService": f"dynamodb.{manifest['region']}.amazonaws.com"}},
                }
            ]
            if run_table_key
            else []
        )
        require(run_statements[1:] == expected_key_statements, "controller run registry key scope mismatch")
    else:
        require(not manifest.get("run_table_kms_key_arn"), "run registry key requires run registry")
    lifecycle = iam.get_role_policy(RoleName=controller, PolicyName="owned-host-lifecycle")["PolicyDocument"]
    statements = lifecycle.get("Statement", [])
    require(isinstance(statements, list) and len(statements) == len(LIFECYCLE_ACTIONS), "controller policy count mismatch")
    statement_by_sid = {statement.get("Sid"): statement for statement in statements}
    require(len(statement_by_sid) == len(statements) and set(statement_by_sid) == set(LIFECYCLE_ACTIONS), "controller policy scope mismatch")
    for sid, actions in LIFECYCLE_ACTIONS.items():
        statement = statement_by_sid[sid]
        observed_actions = statement.get("Action")
        require(
            statement.get("Effect") == "Allow"
            and isinstance(observed_actions, list)
            and len(observed_actions) == len(actions)
            and set(observed_actions) == actions,
            f"controller {sid} action scope mismatch",
        )
    prefix = f"arn:aws:ec2:{manifest['region']}:{manifest['account_id']}"
    plan_sizes, plan_subnets = _launch_plan(manifest)
    approved_resources = {
        f"arn:aws:ec2:{manifest['region']}::image/{manifest['image_id']}",
        *(f"{prefix}:subnet/{subnet}" for subnet in plan_subnets),
        f"{prefix}:security-group/{_output(outputs, 'guest_security_group_id')}",
        # RunInstances through a launch template is authorized against the template too.
        f"{prefix}:launch-template/{_output(outputs, 'launch_template_id')}",
    }
    resources_by_sid = {
        "Inventory": "*",
        "LaunchApprovedInputs": approved_resources,
        "LaunchOwnedInstance": f"{prefix}:instance/*",
        "LaunchOwnedInterface": f"{prefix}:network-interface/*",
        "LaunchOwnedVolume": f"{prefix}:volume/*",
        "CreateOwnedDisk": f"{prefix}:volume/*",
        "RestoreOwnedSnapshot": f"{prefix}:snapshot/*",
        "OperateOwnedInstances": f"{prefix}:instance/*",
        "OperateOwnedDisks": f"{prefix}:volume/*",
        "GrowOwnedDisk": f"{prefix}:volume/*",
        "OperateOwnedAttachments": f"{prefix}:instance/*",
        "SendOwnedCommand": f"arn:aws:ssm:{manifest['region']}::document/AWS-RunShellScript",
        "SendOwnedInstanceCommand": f"{prefix}:instance/*",
        "ReadCommandResult": "*",
        "TagOnCreation": {f"{prefix}:instance/*", f"{prefix}:volume/*", f"{prefix}:network-interface/*"},
        "PassOnlyGuest": guest_arn,
    }
    for sid, expected in resources_by_sid.items():
        resources = statement_by_sid[sid].get("Resource")
        if isinstance(expected, set):
            require(
                isinstance(resources, list) and len(resources) == len(expected) and set(resources) == expected,
                f"controller {sid} resource scope mismatch",
            )
        else:
            require(resources == expected, f"controller {sid} resource scope mismatch")
    require(
        statement_by_sid["Inventory"].get("Condition", {}).get("StringEquals", {}).get("aws:RequestedRegion") == manifest["region"],
        "controller inventory region scope mismatch",
    )
    require(
        statement_by_sid["ReadCommandResult"].get("Condition") == {"StringEquals": {"aws:RequestedRegion": manifest["region"]}},
        "controller command result region scope mismatch",
    )
    for sid in ("OperateOwnedInstances", "OperateOwnedDisks", "GrowOwnedDisk", "OperateOwnedAttachments", "SendOwnedInstanceCommand", "RestoreOwnedSnapshot"):
        condition = statement_by_sid[sid].get("Condition", {})
        scope = condition.get("StringEquals", {})
        require(
            scope.get("aws:ResourceTag/adp:deployment-id") == manifest["deployment_id"] and scope.get("aws:ResourceTag/adp:component") == "dev-box",
            f"controller {sid} deployment scope mismatch",
        )
        require(condition.get("Null", {}).get("aws:ResourceTag/adp:owner-id") == "false", f"controller {sid} owner scope missing")
    for sid in ("LaunchOwnedInstance", "LaunchOwnedInterface", "LaunchOwnedVolume", "CreateOwnedDisk", "TagOnCreation"):
        condition = statement_by_sid[sid].get("Condition", {})
        scope = condition.get("StringEquals", {})
        require(
            scope.get("aws:RequestTag/adp:deployment-id") == manifest["deployment_id"]
            and scope.get("aws:RequestTag/adp:component") == "dev-box"
            and condition.get("Null", {}).get("aws:RequestTag/adp:owner-id") == "false",
            f"controller {sid} request scope mismatch",
        )
    admitted_types = statement_by_sid["LaunchOwnedInstance"]["Condition"]["StringEquals"].get("ec2:InstanceType")
    admitted_types = [admitted_types] if isinstance(admitted_types, str) else admitted_types
    require(
        isinstance(admitted_types, list) and sorted(admitted_types) == sorted(plan_sizes)
        and statement_by_sid["LaunchOwnedInstance"]["Condition"]["StringEquals"].get("ec2:MetadataHttpTokens") == "required"
        and statement_by_sid["LaunchOwnedInterface"]["Condition"].get("Bool", {}).get("ec2:AssociatePublicIpAddress") == "false",
        "controller launch security mismatch",
    )
    require(
        statement_by_sid["CreateOwnedDisk"]["Condition"].get("Bool", {}).get("ec2:Encrypted") == "true"
        and statement_by_sid["CreateOwnedDisk"]["Condition"].get("NumericLessThanEquals", {}).get("ec2:VolumeSize") == manifest["workspace_disk_gb"],
        "controller disk security mismatch",
    )
    key_policy = iam.get_role_policy(RoleName=controller, PolicyName="selected-ebs-key-only")["PolicyDocument"]
    key_statements = key_policy.get("Statement", [])
    expected_key_actions = [
        {"kms:DescribeKey", "kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncryptFrom", "kms:ReEncryptTo"},
        {"kms:CreateGrant"},
    ]
    require(
        len(key_statements) == 2
        and all(
            statement.get("Effect") == "Allow"
            and statement.get("Resource") == manifest["kms_key_arn"]
            and isinstance(statement.get("Action"), list)
            and set(statement["Action"]) == actions
            and statement.get("Condition", {}).get("StringEquals", {}).get("kms:ViaService") == f"ec2.{manifest['region']}.amazonaws.com"
            and statement.get("Condition", {}).get("StringEquals", {}).get("kms:CallerAccount") == manifest["account_id"]
            for statement, actions in zip(key_statements, expected_key_actions, strict=True)
        ),
        "controller KMS key scope mismatch",
    )
    require(key_statements[1]["Condition"].get("Bool", {}).get("kms:GrantIsForAWSResource") == "true", "controller KMS grant scope mismatch")
    profile_arn = _output(outputs, "guest_instance_profile_arn")
    profile = iam.get_instance_profile(InstanceProfileName=profile_arn.rsplit("/", 1)[1])["InstanceProfile"]
    require(
        profile.get("Arn") == profile_arn and [role["Arn"] for role in profile.get("Roles", [])] == [guest_arn],
        "instance profile must contain only the guest role",
    )


def verify_release(manifest: dict, outputs: dict, ec2, iam, controller=None) -> dict:
    digest = manifest.get("manifest_sha256")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
    require(digest == hashlib.sha256(canonical.encode()).hexdigest(), "release manifest digest mismatch")
    require(manifest.get("component") == "dev-box" and manifest.get("schema_version") == 1, "component mismatch")
    image_id = manifest["image_id"]
    images = ec2.describe_images(ImageIds=[image_id]).get("Images", [])
    require(len(images) == 1 and images[0].get("ImageId") == image_id, "AMI identity mismatch")
    image = images[0]
    tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
    require(image.get("OwnerId") == manifest["account_id"], "AMI owner mismatch")
    require(
        tags.get("adp:component") == "dev-box"
        and tags.get("adp:source-revision") == manifest["source_revision"]
        and tags.get("adp:image-revision") == manifest["image_revision"],
        "AMI source revision mismatch",
    )
    profile_revision = manifest.get("crew_profile_revision")
    bootstrap = _crew_bootstrap(manifest)
    if manifest["dashboard_enabled"] or profile_revision is not None:
        require(isinstance(profile_revision, str) and bool(profile_revision), "Crew profile AMI mismatch")
        if bootstrap == "user-data":
            # The base AMI carries no profile tag; the pinned profile arrives at first boot
            # and the controller reads the bootstrap receipt before trusting the host.
            require(tags.get("adp:profile-revision") in (None, profile_revision), "Crew profile AMI mismatch")
        else:
            require(tags.get("adp:profile-revision") == profile_revision, "Crew profile AMI mismatch")
    require(
        all(mapping.get("Ebs", {}).get("Encrypted") is True for mapping in image.get("BlockDeviceMappings", []))
        and bool(image.get("BlockDeviceMappings")),
        "AMI disk encryption mismatch",
    )
    template_id = _output(outputs, "launch_template_id")
    version = outputs.get("launch_template_version", {}).get("value")
    require(type(version) is int and version > 0, "missing immutable launch template version")
    versions = ec2.describe_launch_template_versions(LaunchTemplateId=template_id, Versions=[str(version)]).get("LaunchTemplateVersions", [])
    require(len(versions) == 1 and versions[0].get("VersionNumber") == version, "launch template version mismatch")
    definition = versions[0]["LaunchTemplateData"]
    require(definition.get("ImageId") == image_id, "launch template AMI mismatch")
    require(definition.get("InstanceType") == manifest["instance_size"], "instance size mismatch")
    require(definition.get("MetadataOptions", {}).get("HttpTokens") == "required", "IMDSv2 mismatch")
    interfaces = definition.get("NetworkInterfaces", [])
    guest_group = _output(outputs, "guest_security_group_id")
    require(
        len(interfaces) == 1
        and interfaces[0].get("AssociatePublicIpAddress") is False
        and interfaces[0].get("SubnetId") == manifest["private_subnet_id"]
        and interfaces[0].get("Groups") == [guest_group],
        "private network mismatch",
    )
    require(definition.get("IamInstanceProfile", {}).get("Arn") == _output(outputs, "guest_instance_profile_arn"), "guest profile mismatch")
    mappings = definition.get("BlockDeviceMappings", [])
    require(
        len(mappings) == 1 and mappings[0].get("Ebs", {}).get("Encrypted") is True and mappings[0]["Ebs"].get("DeleteOnTermination") is True,
        "replaceable encrypted root mismatch",
    )
    _user_data(definition, manifest, bootstrap)
    groups = ec2.describe_security_groups(GroupIds=[guest_group]).get("SecurityGroups", [])
    require(len(groups) == 1 and groups[0].get("GroupId") == guest_group, "guest security group mismatch")
    ingress = {(8443, manifest["gateway_security_group_id"])} if manifest["dashboard_enabled"] else set()
    egress = {(443, manifest["gateway_security_group_id"]), (443, manifest["ssm_endpoint_security_group_id"])}
    if bootstrap == "user-data":
        prefix_list = _output(outputs, "crew_bootstrap_s3_prefix_list_id")
        require(prefix_list.startswith("pl-"), "Crew bootstrap S3 prefix list mismatch")
        egress.add((443, prefix_list))
    if manifest.get("gateway_execute_arn"):
        # The gateway's Regional REST API is reached through NAT: exactly one HTTPS egress
        # to the internet, no other port or protocol (main.tf, #8223).
        egress.add((443, "0.0.0.0/0"))
    require(_permissions(groups[0].get("IpPermissions", []), ingress), "dashboard ingress mismatch")
    require(_permissions(groups[0].get("IpPermissionsEgress", []), egress), "private egress mismatch")
    _iam(manifest, outputs, iam)
    status = manifest["controller_status"]
    if status == "present":
        expected_digest = manifest.get("controller_digest")
        require(
            isinstance(expected_digest, str)
            and len(expected_digest) == 71
            and expected_digest.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in expected_digest[7:]),
            "controller digest missing or invalid",
        )
    require(status in ("absent", "present"), "unknown controller status")
    ready = False
    if status == "present":
        require(controller is not None, "controller observation missing")
        observed = controller.observe()
        require(observed.get("image_digest") == manifest.get("controller_digest"), "controller digest mismatch")
        require(observed.get("source_revision") == manifest["source_revision"], "controller source mismatch")
        require(observed.get("healthy") is True and observed.get("release_sha256") == digest, "controller service identity mismatch")
        ready = True
    return {
        "schema_version": 1,
        "component": "dev-box",
        "release_manifest_sha256": digest,
        "source_revision": manifest["source_revision"],
        "definition_revision": manifest["definition_revision"],
        "account_id": manifest["account_id"],
        "region": manifest["region"],
        "module_sha256": manifest["module_sha256"],
        "image_id": image_id,
        "crew_profile_revision": profile_revision,
        "crew_bootstrap": bootstrap,
        "crew_bundle_sha256": manifest.get("crew_bundle_sha256") if bootstrap == "user-data" else None,
        "launch_template_id": template_id,
        "launch_template_version": version,
        "guest_security_group_id": guest_group,
        "gateway_security_group_id": manifest["gateway_security_group_id"],
        "private_subnet_id": manifest["private_subnet_id"],
        "fallback_sizes": _launch_plan(manifest)[0],
        "private_subnets": _launch_plan(manifest)[1],
        "guest_instance_profile_arn": _output(outputs, "guest_instance_profile_arn"),
        "guest_role_arn": _output(outputs, "guest_role_arn"),
        "controller_role_arn": _output(outputs, "controller_role_arn"),
        "run_table_arn": manifest.get("run_table_arn") or None,
        "run_table_kms_key_arn": manifest.get("run_table_kms_key_arn") or None,
        "gateway_execute_arn": manifest.get("gateway_execute_arn") or None,
        "dashboard_enabled": manifest["dashboard_enabled"],
        "controller_status": status,
        "controller_digest": manifest.get("controller_digest") if ready else None,
        "controller_ready": ready,
        "verified_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    arguments = parser.parse_args()
    import boto3

    receipt = verify_release(
        json.loads(arguments.manifest.read_text()), json.loads(arguments.outputs.read_text()), boto3.client("ec2"), boto3.client("iam")
    )
    arguments.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
