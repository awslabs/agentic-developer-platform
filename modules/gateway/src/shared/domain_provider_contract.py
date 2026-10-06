"""Closed installation-owned provider record. No SDK, HTTP or secret material.

Shared by Gateway and the app-owned conditional enrollment command. A record is
reviewed configuration; current human and provider proof is always established
separately at consumption time.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime

PREFIX = "spda1:"
OWNER = "webhook-terraform-domain-provider-v1"
WORKSPACE_NAMESPACE = uuid.UUID("0831ad7c-9dba-5dc3-b75d-eabf596c9975")
FIELDS = frozenset(
    {
        "version",
        "credential_id",
        "owner",
        "domain",
        "installation_id",
        "adp_org_id",
        "org_id",
        "workspace_id",
        "request_id",
        "subject",
        "user_id",
        "membership_id",
        "account_id",
        "region",
        "service",
        "label",
        "role_arn",
        "role_id",
        "policy_sha256",
        "secret_arn",
        "secret_version",
        "binding_sha256",
        "generation",
        "status",
        "expires_at",
        "validation_profile",
        "child_boundary_arn",
        "child_boundary_sha256",
        "managed_policy_arns",
    }
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reserved(value):
    # Reject aliases rather than allowing a case variant to enter personal paths.
    return isinstance(value, str) and value.lower().startswith(PREFIX)


def handle(role_arn):
    return PREFIX + str(uuid.uuid5(uuid.NAMESPACE_URL, "adp:domain-provider-authority:v1:" + role_arn))


def validate(value):
    def require(condition):
        if not condition:
            raise ValueError("invalid domain provider authority")

    require(isinstance(value, dict) and set(value) == FIELDS)
    require(type(value["version"]) is int and value["version"] == 1)
    require(type(value["generation"]) is int and value["generation"] >= 1)
    require(value["owner"] == OWNER and value["domain"] == "superplane" and value["service"] == "aws")
    require(value["status"] in {"active", "revoked"})
    for key in FIELDS - {"version", "generation", "validation_profile", "managed_policy_arns"}:
        require(isinstance(value[key], str) and 1 <= len(value[key]) <= 1024)
    for key in ("org_id", "workspace_id", "request_id"):
        require(str(uuid.UUID(value[key])) == value[key])
    require(value["workspace_id"] == str(uuid.uuid5(WORKSPACE_NAMESPACE, f"{value['org_id']}/{value['request_id']}")))
    require(re.fullmatch(r"[0-9a-f]{24}", value["installation_id"]))
    require(re.fullmatch(r"[0-9]{12}", value["account_id"]))
    require(re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", value["region"]))
    require(re.fullmatch(rf"arn:aws:iam::{value['account_id']}:role/[A-Za-z0-9+=,.@_-]+", value["role_arn"]))
    role_name = value["role_arn"].rsplit("/", 1)[1]
    identity = re.fullmatch(r"adp-([a-z][a-z0-9-]{0,19})-spp-([0-9a-f]{32})", role_name)
    require(identity is not None and identity[2] == digest([value["org_id"], value["workspace_id"]])[:32])
    require(value["managed_policy_arns"] == managed_policies(value["role_arn"]))
    require(value["child_boundary_arn"] == value["role_arn"].replace(":role/", ":policy/") + "-child-boundary")
    require(re.fullmatch(r"AROA[A-Z0-9]{17}", value["role_id"]))
    require(value["credential_id"] == handle(value["role_arn"]))
    require(re.fullmatch(rf"arn:aws:secretsmanager:{value['region']}:{value['account_id']}:secret:[A-Za-z0-9/_+=.@-]+", value["secret_arn"]))
    require(re.fullmatch(r"[A-Za-z0-9-]{32,64}", value["secret_version"]))
    require(re.fullmatch(rf"arn:aws:iam::{value['account_id']}:policy/[A-Za-z0-9+=,.@_/-]+", value["child_boundary_arn"]))
    for key in ("policy_sha256", "binding_sha256", "child_boundary_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", value[key]))
    require(datetime.fromisoformat(value["expires_at"]).utcoffset() is not None)
    require(isinstance(value["validation_profile"], dict))
    profile = value["validation_profile"]
    require(set(profile) == {"region", "image_id", "instance_type", "subnet_id", "security_group_ids"})
    require(profile["region"] == value["region"])
    for key, pattern in (("image_id", r"ami-[0-9a-f]+"), ("subnet_id", r"subnet-[0-9a-f]+"), ("instance_type", r"[a-z][a-z0-9-]*\.[a-z0-9]+")):
        require(isinstance(profile[key], str) and re.fullmatch(pattern, profile[key]))
    require(isinstance(profile["security_group_ids"], list) and 1 <= len(profile["security_group_ids"]) <= 5)
    require(all(isinstance(group, str) and re.fullmatch(r"sg-[0-9a-f]+", group) for group in profile["security_group_ids"]))
    return value


def boundary_identity(iam, arn):
    policy = iam.get_policy(PolicyArn=arn)["Policy"]
    version = policy["DefaultVersionId"]
    document = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"]["Document"]
    return digest({"arn": arn, "version": version, "document": document})


def managed_policies(role_arn):
    prefix = role_arn.replace(":role/", ":policy/")
    return sorted(prefix + "-" + suffix for suffix in ("network", "identity", "lifecycle", "state-validation"))


def policy_identity(iam, role_arn, managed_policy_arns):
    """The exact four app-owned policy shards, current versions and role trust.

    AWS's inline aggregate limit cannot hold the scoped native policy. The only
    accepted alternative is this fixed four-shard recipe, never arbitrary managed
    attachments. No inline or provider-boundary change can hide beside the digest.
    """
    if managed_policy_arns != managed_policies(role_arn):
        raise ValueError("provider policy identity refused")
    name = role_arn.rsplit("/", 1)[1]
    role = iam.get_role(RoleName=name)["Role"]
    inline = iam.list_role_policies(RoleName=name)
    attached = iam.list_attached_role_policies(RoleName=name)
    arns = [item["PolicyArn"] for item in attached.get("AttachedPolicies", [])]
    if (
        role["Arn"] != role_arn
        or role.get("PermissionsBoundary")
        or inline.get("IsTruncated")
        or attached.get("IsTruncated")
        or inline.get("PolicyNames")
        or len(arns) != 4
        or sorted(arns) != managed_policy_arns
    ):
        raise ValueError("provider policy identity refused")
    policies = {}
    for arn in arns:
        policy = iam.get_policy(PolicyArn=arn)["Policy"]
        version = policy["DefaultVersionId"]
        document = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"]["Document"]
        policies[arn] = {"version": version, "document": document}
    return role["RoleId"], digest({"trust": role["AssumeRolePolicyDocument"], "managed": policies})
