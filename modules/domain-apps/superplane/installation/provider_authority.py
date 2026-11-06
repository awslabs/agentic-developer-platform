"""Protected owner enrollment/revocation, after reviewed Terraform composition.

No human bearer token, UserCredential insert, implicit restoration or runtime
readiness claim. Run with the selected installation operator and kube context.
Input contains references/selectors, never SecretString or session credentials.
"""

import argparse
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import boto3


def contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_provider_contract.py"
    )
    spec = importlib.util.spec_from_file_location("provider_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition):
    if not condition:
        raise ValueError("protected provider enrollment refused")


def installed_config(namespace, cluster):
    require(
        isinstance(namespace, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", namespace)
    )
    context = subprocess.run(
        [
            "kubectl",
            "config",
            "view",
            "--minify",
            "--raw",
            "-o",
            "jsonpath={.clusters}",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    current = json.loads(context.stdout)
    require(
        len(current) == 1
        and current[0]["cluster"]["server"] == cluster["endpoint"]
        and current[0]["cluster"].get("certificate-authority-data")
        == cluster["certificateAuthority"]["data"]
        and not current[0]["cluster"].get("insecure-skip-tls-verify")
    )
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "configmap",
            "adp-worker-authority-config",
            "-n",
            namespace,
            "-o",
            "json",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    value = json.loads(result.stdout)
    require(value["metadata"]["namespace"] == namespace)
    return value["data"]


def repaired_session_tag_policy(snapshot, previous, schema):
    """Only split the original trust; never change principal, tags or IAM grants.

    The old complete policy snapshot is authenticated by the enrolled digest.
    It contains the ExternalId and must remain an owner-private document.
    """
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    trust = snapshot["trust"]
    require(set(trust) == {"Version", "Statement"} and trust["Version"] == "2012-10-17")
    require(len(trust["Statement"]) == 1)
    statement = trust["Statement"][0]
    require(set(statement) == {"Effect", "Principal", "Action", "Condition"})
    require(statement["Effect"] == "Allow")
    require(statement["Action"] == ["sts:AssumeRole", "sts:TagSession"])
    require(set(statement["Principal"]) == {"AWS"})
    require(
        re.fullmatch(
            rf"arn:aws:iam::{previous['account_id']}:role/[A-Za-z0-9+=,.@_-]+",
            statement["Principal"]["AWS"],
        )
    )
    condition = statement["Condition"]
    external_id = condition.get("StringEquals", {}).get("sts:ExternalId")
    require(isinstance(external_id, str) and 16 <= len(external_id) <= 255)
    require(
        condition
        == {
            "StringEquals": {
                "sts:ExternalId": external_id,
                "aws:RequestTag/adp:user_id": previous["user_id"],
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
        }
    )
    result = copy.deepcopy(snapshot)
    assume, tagging = copy.deepcopy(statement), copy.deepcopy(statement)
    # IAM GetRole canonicalizes singleton Action arrays to scalar strings.
    # Hash that exact readback form; do not normalize any permission or condition.
    assume["Action"] = "sts:AssumeRole"
    tagging["Action"] = "sts:TagSession"
    del tagging["Condition"]["StringEquals"]["sts:ExternalId"]
    result["trust"]["Statement"] = [assume, tagging]
    return result


def repaired_beneficiary_tag_policy(snapshot, previous, schema):
    """Correct only a login-subject tag to this same enrolled human's user ID."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(previous["subject"] != previous["user_id"])
    trust = snapshot["trust"]
    require(isinstance(trust, dict) and len(trust.get("Statement", [])) == 2)
    assume, tagging = trust["Statement"]
    require(assume.get("Action") == "sts:AssumeRole")
    require(tagging.get("Action") == "sts:TagSession")
    # Reuse the exact trust recipe validator, then require byte-equivalent
    # canonical structure for both existing statements and every managed grant.
    combined = copy.deepcopy(snapshot)
    combined["trust"]["Statement"] = [copy.deepcopy(assume)]
    combined["trust"]["Statement"][0]["Action"] = ["sts:AssumeRole", "sts:TagSession"]
    expected = {
        **previous,
        "user_id": previous["subject"],
        "policy_sha256": schema.digest(combined),
    }
    require(repaired_session_tag_policy(combined, expected, schema) == snapshot)
    repaired = copy.deepcopy(snapshot)
    for statement in repaired["trust"]["Statement"]:
        statement["Condition"]["StringEquals"]["aws:RequestTag/adp:user_id"] = previous[
            "user_id"
        ]
    return repaired


def repaired_network_reads_policy(snapshot, previous, schema, version):
    """Add exactly the two regional reads required by the pinned Terraform provider.

    The enrolled digest authenticates every old grant. Only the existing
    state-validation shard version and its first regional read list may change.
    """
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    arn = previous["role_arn"].replace(":role/", ":policy/") + "-state-validation"
    shard = snapshot["managed"][arn]
    require(isinstance(version, str) and re.fullmatch(r"v[1-9][0-9]*", version))
    require(re.fullmatch(r"v[1-9][0-9]*", shard["version"]))
    require(int(version[1:]) > int(shard["version"][1:]))
    statement = shard["document"]["Statement"][0]
    actions = statement.get("Action")
    require(isinstance(actions, list) and all(isinstance(x, str) for x in actions))
    require(len(actions) == len(set(actions)))
    require(
        statement
        == {
            "Effect": "Allow",
            "Action": actions,
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestedRegion": previous["region"]}},
        }
    )
    additions = ["ec2:DescribeAddressesAttribute", "ec2:DescribePrefixLists"]
    require(not set(additions).intersection(actions))
    require(actions.count("ec2:DescribeVpcAttribute") == 1)
    repaired = copy.deepcopy(snapshot)
    shard = repaired["managed"][arn]
    shard["version"] = version
    index = actions.index("ec2:DescribeVpcAttribute")
    shard["document"]["Statement"][0]["Action"][index:index] = additions
    return repaired


def repaired_inventory_read_policy(snapshot, previous, schema, version):
    """Add only the regional resource-tag inventory read to its existing shard."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    arn = previous["role_arn"].replace(":role/", ":policy/") + "-state-validation"
    shard = snapshot["managed"][arn]
    require(isinstance(version, str) and re.fullmatch(r"v[1-9][0-9]*", version))
    require(re.fullmatch(r"v[1-9][0-9]*", shard["version"]))
    require(int(version[1:]) > int(shard["version"][1:]))
    statement = shard["document"]["Statement"][0]
    actions = statement.get("Action")
    require(isinstance(actions, list) and all(isinstance(x, str) for x in actions))
    require(len(actions) == len(set(actions)))
    require(
        statement
        == {
            "Effect": "Allow",
            "Action": actions,
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestedRegion": previous["region"]}},
        }
    )
    require("tag:GetResources" not in actions)
    repaired = copy.deepcopy(snapshot)
    shard = repaired["managed"][arn]
    shard["version"] = version
    shard["document"]["Statement"][0]["Action"].append("tag:GetResources")
    return repaired


def repaired_state_history_read_policy(snapshot, previous, schema, version):
    """Add version listing only to the authenticated workspace state prefix."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    arn = previous["role_arn"].replace(":role/", ":policy/") + "-state-validation"
    shard = snapshot["managed"][arn]
    require(isinstance(version, str) and re.fullmatch(r"v[1-9][0-9]*", version))
    require(re.fullmatch(r"v[1-9][0-9]*", shard["version"]))
    require(int(version[1:]) > int(shard["version"][1:]))
    role = previous["role_arn"].rsplit("/", 1)[1]
    environment = re.fullmatch(r"adp-([a-z][a-z0-9-]{0,19})-spp-[0-9a-f]{32}", role)
    require(environment is not None)
    prefix = (
        f"{environment[1]}/modules/superplane-workspaces/v2/"
        f"{previous['org_id']}/{previous['workspace_id']}"
    )
    statements = shard["document"]["Statement"]
    matches = [
        index
        for index, statement in enumerate(statements)
        if statement.get("Action") == ["s3:ListBucket"]
    ]
    require(len(matches) == 1)
    index = matches[0]
    require(
        statements[index]
        == {
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": f"arn:aws:s3:::adp-terraform-state-{previous['account_id']}",
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
    repaired = copy.deepcopy(snapshot)
    shard = repaired["managed"][arn]
    shard["version"] = version
    shard["document"]["Statement"][index]["Action"].append("s3:ListBucketVersions")
    return repaired


def repaired_nodegroup_role_read_policy(snapshot, previous, schema, version):
    """Add only GetRole for the existing EKS managed-nodegroup service role."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    arn = previous["role_arn"].replace(":role/", ":policy/") + "-identity"
    shard = snapshot["managed"][arn]
    require(isinstance(version, str) and re.fullmatch(r"v[1-9][0-9]*", version))
    require(re.fullmatch(r"v[1-9][0-9]*", shard["version"]))
    require(int(version[1:]) > int(shard["version"][1:]))
    statement = {
        "Effect": "Allow",
        "Action": ["iam:GetRole"],
        "Resource": f"arn:aws:iam::{previous['account_id']}:role/aws-service-role/"
        "eks-nodegroup.amazonaws.com/AWSServiceRoleForAmazonEKSNodegroup",
    }
    require(statement not in shard["document"]["Statement"])
    repaired = copy.deepcopy(snapshot)
    repaired["managed"][arn]["version"] = version
    repaired["managed"][arn]["document"]["Statement"].append(statement)
    return repaired


def repaired_access_inventory_policy(snapshot, previous, schema, version):
    """Separate exact-cluster description from actor-only entry mutations."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    arn = previous["role_arn"].replace(":role/", ":policy/") + "-lifecycle"
    shard = snapshot["managed"][arn]
    require(isinstance(version, str) and re.fullmatch(r"v[1-9][0-9]*", version))
    require(re.fullmatch(r"v[1-9][0-9]*", shard["version"]))
    require(int(version[1:]) > int(shard["version"][1:]))
    cluster = previous["role_arn"].rsplit("/", 1)[1].replace("-spp-", "-spw-", 1)
    prefix = f"arn:aws:eks:{previous['region']}:{previous['account_id']}:access-entry/{cluster}/"
    statements = shard["document"]["Statement"]
    matches = [
        i
        for i, stmt in enumerate(statements)
        if "eks:DescribeAccessEntry" in stmt.get("Action", [])
    ]
    require(len(matches) == 1)
    index = matches[0]
    old = statements[index]
    resources = old.get("Resource")
    condition = {
        "StringEquals": {"aws:PrincipalTag/adp:agent_id": "superplane-operation"}
    }
    require(isinstance(resources, list) and len(resources) == 3)
    require(
        all(
            isinstance(r, str)
            and r.startswith(prefix + "role/" + previous["account_id"] + "/")
            and r.endswith("/*")
            for r in resources
        )
    )
    require(
        old
        == {
            "Effect": "Allow",
            "Action": [
                "eks:DescribeAccessEntry",
                "eks:ListAssociatedAccessPolicies",
                "eks:DeleteAccessEntry",
                "eks:DisassociateAccessPolicy",
            ],
            "Resource": resources,
            "Condition": condition,
        }
    )
    repaired = copy.deepcopy(snapshot)
    shard = repaired["managed"][arn]
    shard["version"] = version
    shard["document"]["Statement"][index]["Action"].remove("eks:DescribeAccessEntry")
    shard["document"]["Statement"].append(
        {
            "Effect": "Allow",
            "Action": ["eks:DescribeAccessEntry"],
            "Resource": prefix + "*",
            "Condition": condition,
        }
    )
    return repaired


def repaired_access_entry_tags_policy(snapshot, previous, schema, version):
    """Permit tagged creation only on the already enrolled bootstrap actors."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"trust", "managed"})
    require(schema.digest(snapshot) == previous["policy_sha256"])
    require(set(snapshot["managed"]) == set(previous["managed_policy_arns"]))
    arn = previous["role_arn"].replace(":role/", ":policy/") + "-lifecycle"
    shard = snapshot["managed"][arn]
    require(isinstance(version, str) and re.fullmatch(r"v[1-9][0-9]*", version))
    require(re.fullmatch(r"v[1-9][0-9]*", shard["version"]))
    require(int(version[1:]) > int(shard["version"][1:]))
    cluster = previous["role_arn"].rsplit("/", 1)[1].replace("-spp-", "-spw-", 1)
    prefix = f"arn:aws:eks:{previous['region']}:{previous['account_id']}:access-entry/{cluster}/role/{previous['account_id']}/"
    statements = shard["document"]["Statement"]
    matches = [
        i
        for i, stmt in enumerate(statements)
        if "eks:DeleteAccessEntry" in stmt.get("Action", [])
    ]
    require(len(matches) == 1)
    index = matches[0]
    resources = statements[index].get("Resource")
    require(isinstance(resources, list) and len(resources) == 3)
    require(
        all(
            isinstance(r, str) and r.startswith(prefix) and r.endswith("/*")
            for r in resources
        )
    )
    require(
        statements[index]
        == {
            "Effect": "Allow",
            "Action": [
                "eks:ListAssociatedAccessPolicies",
                "eks:DeleteAccessEntry",
                "eks:DisassociateAccessPolicy",
            ],
            "Resource": resources,
            "Condition": {
                "StringEquals": {
                    "aws:PrincipalTag/adp:agent_id": "superplane-operation"
                }
            },
        }
    )
    repaired = copy.deepcopy(snapshot)
    repaired["managed"][arn]["version"] = version
    repaired["managed"][arn]["document"]["Statement"][index]["Action"].append(
        "eks:TagResource"
    )
    return repaired


def enroll(
    document,
    session,
    *,
    config_reader=installed_config,
    check_only=True,
    revoke=False,
    previous_document=None,
    previous_tag_policy=None,
    renew=False,
    repair_beneficiary_tag=False,
    repair_network_reads_version=None,
    repair_nodegroup_role_read_version=None,
    repair_inventory_read_version=None,
    repair_access_inventory_version=None,
    repair_access_entry_tags_version=None,
    repair_state_history_read_version=None,
):
    require(
        isinstance(document, dict)
        and set(document) - {"management_region"}
        == {
            "version",
            "environment",
            "operator_role_arn",
            "operator_role_id",
            "gateway_namespace",
            "registry_table",
            "authority",
            "management_cluster",
        }
    )
    require(type(document["version"]) is int and document["version"] == 1)
    environment = document["environment"]
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment))
    schema = contract()
    value = schema.validate(document["authority"])
    require(value["status"] == "active")
    require(not (revoke and previous_document is not None))
    require(previous_tag_policy is None or previous_document is not None)
    require(
        not renew
        or (
            previous_document is not None and previous_tag_policy is None and not revoke
        )
    )
    require(
        not repair_beneficiary_tag
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
        )
    )
    require(
        repair_network_reads_version is None
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
            and not repair_beneficiary_tag
        )
    )
    require(
        repair_nodegroup_role_read_version is None
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
            and not repair_beneficiary_tag
            and repair_network_reads_version is None
        )
    )
    require(
        repair_inventory_read_version is None
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
            and not repair_beneficiary_tag
            and repair_network_reads_version is None
            and repair_nodegroup_role_read_version is None
        )
    )
    require(
        repair_access_inventory_version is None
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
            and not repair_beneficiary_tag
            and repair_network_reads_version is None
            and repair_nodegroup_role_read_version is None
            and repair_inventory_read_version is None
        )
    )
    require(
        repair_access_entry_tags_version is None
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
            and not repair_beneficiary_tag
            and repair_network_reads_version is None
            and repair_nodegroup_role_read_version is None
            and repair_inventory_read_version is None
            and repair_access_inventory_version is None
        )
    )
    require(
        repair_state_history_read_version is None
        or (
            previous_document is not None
            and previous_tag_policy is not None
            and not renew
            and not revoke
            and not repair_beneficiary_tag
            and repair_network_reads_version is None
            and repair_nodegroup_role_read_version is None
            and repair_inventory_read_version is None
            and repair_access_inventory_version is None
            and repair_access_entry_tags_version is None
        )
    )
    previous = None
    binding_only_rotation = False
    if previous_document is not None:
        require(isinstance(previous_document, dict))
        require(
            {k: v for k, v in previous_document.items() if k != "authority"}
            == {k: v for k, v in document.items() if k != "authority"}
        )
        previous = schema.validate(previous_document["authority"])
        require(previous["status"] == "active")
        require(value["generation"] == previous["generation"] + 1)
        changed_field = (
            "expires_at"
            if renew
            else "binding_sha256"
            if previous_tag_policy is None
            else "policy_sha256"
        )
        binding_only_rotation = changed_field == "binding_sha256"
        if renew:
            # Explicit owner renewal may advance an expired active grant, never a
            # revoked one. All live ownership/policy/binding checks below still run.
            expiry = datetime.fromisoformat(value["expires_at"])
            require(
                datetime.now(timezone.utc)
                < expiry
                <= datetime.now(timezone.utc) + timedelta(hours=24)
                and expiry > datetime.fromisoformat(previous["expires_at"])
            )
        require(value[changed_field] != previous[changed_field])
        if previous_tag_policy is not None:
            repair = (
                repaired_beneficiary_tag_policy
                if repair_beneficiary_tag
                else repaired_session_tag_policy
            )
            repaired = (
                repaired_state_history_read_policy(
                    previous_tag_policy,
                    previous,
                    schema,
                    repair_state_history_read_version,
                )
                if repair_state_history_read_version is not None
                else repaired_access_entry_tags_policy(
                    previous_tag_policy,
                    previous,
                    schema,
                    repair_access_entry_tags_version,
                )
                if repair_access_entry_tags_version is not None
                else repaired_access_inventory_policy(
                    previous_tag_policy,
                    previous,
                    schema,
                    repair_access_inventory_version,
                )
                if repair_access_inventory_version is not None
                else repaired_inventory_read_policy(
                    previous_tag_policy, previous, schema, repair_inventory_read_version
                )
                if repair_inventory_read_version is not None
                else repaired_nodegroup_role_read_policy(
                    previous_tag_policy,
                    previous,
                    schema,
                    repair_nodegroup_role_read_version,
                )
                if repair_nodegroup_role_read_version is not None
                else repaired_network_reads_policy(
                    previous_tag_policy, previous, schema, repair_network_reads_version
                )
                if repair_network_reads_version is not None
                else repair(previous_tag_policy, previous, schema)
            )
            require(schema.digest(repaired) == value["policy_sha256"])
        require(
            {k: v for k, v in value.items() if k not in {changed_field, "generation"}}
            == {
                k: v
                for k, v in previous.items()
                if k not in {changed_field, "generation"}
            }
        )
        # Owner binding reconciliation can retain an expired identity baseline.
        # The exact expiry is unchanged above; normal consumption still refuses
        # expired authority. Renewal and permission repair remain separate modes.
        require(
            binding_only_rotation
            or datetime.fromisoformat(value["expires_at"]) > datetime.now(timezone.utc)
        )
    elif not revoke:
        require(value["generation"] == 1)
    account, region = value["account_id"], value["region"]
    management_region = document.get("management_region", region)
    require(
        isinstance(management_region, str)
        and re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]+", management_region)
    )
    caller = session.client("sts", region_name=region).get_caller_identity()
    operator = document["operator_role_arn"]
    require(
        caller["Account"] == account
        and re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_-]+", operator)
    )
    require(
        caller["Arn"].startswith(
            f"arn:aws:sts::{account}:assumed-role/{operator.rsplit('/', 1)[1]}/"
        )
    )
    require(caller["UserId"].split(":", 1)[0] == document["operator_role_id"])
    iam = session.client("iam", region_name=region)
    owner = iam.get_role(RoleName=operator.rsplit("/", 1)[1])["Role"]
    require(
        owner["Arn"] == operator and owner["RoleId"] == document["operator_role_id"]
    )
    ddb = session.client("dynamodb", region_name=management_region)
    table = f"adp-{environment}-superplane-provider-authorities"
    key = {"record_id": {"S": value["credential_id"]}}
    expected = {
        **key,
        "document": {"S": schema.canonical(value)},
        "revision": {"S": schema.digest(value)},
    }
    old = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    revoked = {**value, "status": "revoked", "generation": value["generation"] + 1}
    revoked_item = {
        **key,
        "document": {"S": schema.canonical(revoked)},
        "revision": {"S": schema.digest(revoked)},
    }
    if revoke:
        # Revocation still works if the provider/installation is broken or removed.
        # It can only withdraw the exact original owner-enrolled generation.
        require(old in (expected, revoked_item))
        if old == expected and not check_only:
            ddb.put_item(
                TableName=table,
                Item=revoked_item,
                ConditionExpression="revision = :expected",
                ExpressionAttributeValues={":expected": expected["revision"]},
            )
            require(
                ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
                == revoked_item
            )
        return {
            "state": "revoke-reviewed" if check_only else "revoked",
            "credential_id": value["credential_id"],
        }
    predecessor = None
    if previous is not None:
        predecessor = {
            **key,
            "document": {"S": schema.canonical(previous)},
            "revision": {"S": schema.digest(previous)},
        }
        require(old in (predecessor, expected))
    else:
        require(old is None or old == expected)
    require(
        schema.policy_identity(iam, value["role_arn"], value["managed_policy_arns"])
        == (value["role_id"], value["policy_sha256"])
    )
    require(
        schema.boundary_identity(iam, value["child_boundary_arn"])
        == value["child_boundary_sha256"]
    )
    versions = session.client("secretsmanager", region_name=region).describe_secret(
        SecretId=value["secret_arn"]
    )["VersionIdsToStages"]
    require(
        [version for version, stages in versions.items() if "AWSCURRENT" in stages]
        == [value["secret_version"]]
    )
    response = session.client("s3", region_name=management_region).get_object(
        Bucket=f"adp-terraform-state-{account}",
        Key=f"domain-routes/{environment}/superplane/public-route.json",
    )
    body = response["Body"]
    try:
        raw = body.read(4097)
    finally:
        body.close()
    require(len(raw) <= 4096)
    route = json.loads(raw)
    require(
        route.get("version") == 2
        and route.get("enabled") is True
        and route.get("installation_id") == value["installation_id"]
    )
    cluster = session.client("eks", region_name=management_region).describe_cluster(
        name=document["management_cluster"]
    )["cluster"]
    require(
        cluster["arn"]
        == f"arn:aws:eks:{management_region}:{account}:cluster/{document['management_cluster']}"
        and cluster["status"] == "ACTIVE"
    )
    configured = config_reader(document["gateway_namespace"], cluster)
    require(configured.get("ADP_DOMAIN_PROVIDER_ACCOUNT_ID") == account)
    require(configured.get("ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE") == table)
    require(
        configured.get("ADP_DOMAIN_PROVIDER_EVIDENCE_TABLE")
        == f"adp-{environment}-superplane-provider-evidence"
    )
    bindings = json.loads(configured["ADP_DOMAIN_OPERATION_BINDINGS"])
    matches = [
        item
        for item in bindings
        if item.get("domain") == "superplane" and item.get("org_id") == value["org_id"]
    ]
    require(len(matches) == 1)
    binding = {
        "worker_scaled_job": "superplane-paid-worker",
        "current_identity_enforced": False,
        "domain_database_secret_id": "",
        "domain_database_schema": "",
        **matches[0],
    }
    require(
        schema.digest(binding) == value["binding_sha256"]
        and binding["adp_org_id"] == value["adp_org_id"]
        and binding["worker_namespace"] == route["namespace"]
    )
    require(re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", document["registry_table"]))
    registry = session.client("ssm", region_name=management_region).get_parameter(
        Name=f"/adp/{environment}/gateway/agent-registry-table"
    )["Parameter"]["Value"]
    require(registry == document["registry_table"])
    for kind, suffix, scopes in (
        ("producer", "api-producer", ["domain:operation-producer"]),
        (
            "worker",
            "domain-worker",
            ["domain:operation-executor", "domain:operation-recovery"],
        ),
    ):
        entry = ddb.get_item(
            TableName=document["registry_table"],
            Key={"agent_id": {"S": binding[f"{kind}_registry_id"]}},
            ConsistentRead=True,
        ).get("Item", {})
        role_arn = f"arn:aws:iam::{account}:role/adp-{environment}-superplane-{suffix}"
        require(
            entry.get("owner") == {"S": "webhook-terraform-domain-operations-v1"}
            and entry.get("scope") == {"S": "internal"}
            and entry.get("status") == {"S": "active"}
            and entry.get("org_id") == {"S": value["adp_org_id"]}
            and entry.get("domain_org_id") == {"S": value["org_id"]}
            and entry.get("role_arn") == {"S": role_arn}
            and sorted(entry.get("credential_scopes", {}).get("SS", []))
            == sorted(scopes)
        )
        live = iam.get_role(RoleName=role_arn.rsplit("/", 1)[1])["Role"]
        require(
            live["Arn"] == role_arn
            and entry.get("iam_role_id") == {"S": live["RoleId"]}
        )
    if old is None and not check_only:
        ddb.put_item(
            TableName=table,
            Item=expected,
            ConditionExpression="attribute_not_exists(record_id)",
        )
        require(
            ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
            == expected
        )
    if predecessor is not None and old == predecessor and not check_only:
        # Only this exact active generation may advance. Revocation, concurrent
        # rotation and lost replies cannot restore or overwrite another record.
        require(
            binding_only_rotation
            or datetime.fromisoformat(value["expires_at"]) > datetime.now(timezone.utc)
        )
        ddb.put_item(
            TableName=table,
            Item=expected,
            ConditionExpression="revision = :expected",
            ExpressionAttributeValues={":expected": predecessor["revision"]},
        )
        require(
            ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
            == expected
        )
    return {
        "state": (
            ("renewal-reviewed" if renew else "rotation-reviewed")
            if predecessor is not None and old == predecessor and check_only
            else "configured"
            if old is not None or not check_only
            else "absent"
        ),
        "credential_id": value["credential_id"],
        "revision": schema.digest(value),
        "human_admitted": False,
        "worker_ready": False,
    }


def private_document(filename):
    path = Path(filename)
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and not info.st_mode & 0o077
        and info.st_uid == os.getuid()
    )
    require(info.st_size <= 65536)
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", required=True)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the reviewed exact conditional enrollment, binding rotation or revocation",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--revoke", action="store_true")
    mode.add_argument(
        "--rotate-binding-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Rotate only the installed binding digest, retaining the exact expiry even when expired",
    )
    mode.add_argument(
        "--repair-session-tag-trust-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Advance only the exact original provider trust split, preserving all scope and expiry",
    )
    mode.add_argument(
        "--repair-beneficiary-tag-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Correct only the enrolled login-subject trust tag to that same human's internal user ID",
    )
    mode.add_argument(
        "--repair-network-reads-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Add only the pinned Terraform provider's regional EIP and prefix-list reads",
    )
    mode.add_argument(
        "--repair-nodegroup-role-read-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Add only GetRole for the existing EKS managed-nodegroup service role",
    )
    mode.add_argument(
        "--repair-inventory-read-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Add only regional tag:GetResources for completed infrastructure inventory",
    )
    mode.add_argument(
        "--repair-access-inventory-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Permit exact-cluster EKS entry descriptions, preserving actor-only mutations",
    )
    parser.add_argument(
        "--repaired-policy-version",
        help="Exact changed managed-policy version from the reviewed apply",
    )
    mode.add_argument(
        "--repair-access-entry-tags-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Permit tagged creation only for the enrolled workspace bootstrap actors",
    )
    mode.add_argument(
        "--repair-state-history-read-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Add only version listing on the existing workspace state prefix",
    )
    mode.add_argument(
        "--renew-from",
        metavar="PREVIOUS_DOCUMENT",
        help="Renew the exact active owner authority for at most 24 hours, preserving identity and permissions",
    )
    parser.add_argument(
        "--previous-policy-document",
        metavar="PRIVATE_SNAPSHOT",
        help="Original trust/managed policy snapshot authenticated by the predecessor digest",
    )
    args = parser.parse_args()
    try:
        require(
            bool(
                args.repair_session_tag_trust_from
                or args.repair_beneficiary_tag_from
                or args.repair_network_reads_from
                or args.repair_nodegroup_role_read_from
                or args.repair_inventory_read_from
                or args.repair_access_inventory_from
                or args.repair_access_entry_tags_from
                or args.repair_state_history_read_from
            )
            == bool(args.previous_policy_document)
        )
        require(
            bool(
                args.repair_network_reads_from
                or args.repair_nodegroup_role_read_from
                or args.repair_inventory_read_from
                or args.repair_access_inventory_from
                or args.repair_access_entry_tags_from
                or args.repair_state_history_read_from
            )
            == bool(args.repaired_policy_version)
        )
        prior_path = (
            args.rotate_binding_from
            or args.repair_session_tag_trust_from
            or args.repair_beneficiary_tag_from
            or args.repair_network_reads_from
            or args.repair_nodegroup_role_read_from
            or args.repair_inventory_read_from
            or args.repair_access_inventory_from
            or args.repair_access_entry_tags_from
            or args.repair_state_history_read_from
            or args.renew_from
        )
        result = enroll(
            private_document(args.document),
            boto3.Session(),
            check_only=not args.write,
            revoke=args.revoke,
            renew=bool(args.renew_from),
            repair_beneficiary_tag=bool(args.repair_beneficiary_tag_from),
            repair_network_reads_version=args.repaired_policy_version
            if args.repair_network_reads_from
            else None,
            repair_nodegroup_role_read_version=args.repaired_policy_version
            if args.repair_nodegroup_role_read_from
            else None,
            repair_inventory_read_version=args.repaired_policy_version
            if args.repair_inventory_read_from
            else None,
            repair_access_inventory_version=args.repaired_policy_version
            if args.repair_access_inventory_from
            else None,
            repair_access_entry_tags_version=args.repaired_policy_version
            if args.repair_access_entry_tags_from
            else None,
            repair_state_history_read_version=args.repaired_policy_version
            if args.repair_state_history_read_from
            else None,
            previous_document=private_document(prior_path) if prior_path else None,
            previous_tag_policy=private_document(args.previous_policy_document)
            if args.previous_policy_document
            else None,
        )
    except Exception:
        # No remote text, path, manifest or secret reference is printed on failure.
        raise SystemExit(
            "Provider authority operation refused; preserve the exact document and reconcile with its owner."
        ) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
