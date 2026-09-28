#!/usr/bin/env python3
"""Read-only admission check for operator-owned workload ceilings.

Run with the operator identity before enabling deployment trust. A permissions
boundary is an authorization decision, not just a policy ARN that exists.
"""

import argparse
import base64
from fnmatch import fnmatchcase
import json
from pathlib import Path
import subprocess
import sys
import ssl
from urllib.request import Request, urlopen


def many(value):
    return value if isinstance(value, list) else [value]


def matches(statement, action):
    patterns = many(statement.get("Action", statement.get("NotAction", [])))
    match = any(fnmatchcase(action.lower(), p.lower()) for p in patterns)
    return not match if "NotAction" in statement else match


def maximum_resources(policy, action):
    """Conservative upper bound; conditional denies never count as a ceiling."""
    resources = set()
    for statement in policy["Statement"]:
        if statement["Effect"] == "Allow" and matches(statement, action):
            # An Allow with NotResource or conditions is still potentially broad.
            resources.update(many(statement.get("Resource", "*")))
    for statement in policy["Statement"]:
        if (
            statement["Effect"] != "Deny"
            or statement.get("Condition")
            or not matches(statement, action)
        ):
            continue
        if statement.get("Resource") == "*" or statement.get("Resource") == ["*"]:
            return set()
        if "NotResource" in statement:
            permitted = set(many(statement["NotResource"]))
            narrowed = set()
            for source in resources:
                for target in permitted:
                    source_glob = any(c in source for c in "*?")
                    target_glob = any(c in target for c in "*?")
                    if source_glob and target_glob:
                        narrowed.add(target if source == "*" else source)
                    elif source_glob and fnmatchcase(target, source):
                        narrowed.add(target)
                    elif not source_glob and fnmatchcase(source, target):
                        narrowed.add(source)
            resources = narrowed
    return resources


def denied_resource_ceiling(policy, action):
    # Model a resource policy granting directly to the workload's role session:
    # implicit boundary denies do not contain those grants. Only explicit,
    # unconditional denies establish the ceiling we admit.
    return maximum_resources(
        {
            "Statement": [
                {"Effect": "Allow", "Action": "*", "Resource": "*"},
                *[s for s in policy["Statement"] if s["Effect"] == "Deny"],
            ]
        },
        action,
    )


def verify_ceiling(policy, roles, execution_resources):
    # These are the IAM write actions in AWS's machine-readable service
    # authorization reference. Read/list operations do not alter identity.
    # Reject open-ended Allow action wildcards unless an unconditional finite
    # NotAction deny supplies the actual API ceiling (as our runner/build
    # boundaries do). New execution services cannot silently evade this audit.
    finite_ceiling = any(
        s["Effect"] == "Deny"
        and not s.get("Condition")
        and s.get("Resource") in ("*", ["*"])
        and "NotAction" in s
        and all(not any(c in a for c in "*?") for a in many(s["NotAction"]))
        for s in policy["Statement"]
    )
    for statement in policy["Statement"]:
        if statement["Effect"] == "Allow":
            if not ("NotAction" not in statement):
                raise AssertionError("Open-ended workload API ceiling")
            if not (
                finite_ceiling
                or all(
                    not any(c in a for c in "*?")
                    for a in many(statement.get("Action", []))
                )
            ):
                raise AssertionError("Workload API ceiling must enumerate actions")
    iam_writes = json.loads(
        (Path(__file__).parent / "iam-write-actions.json").read_text()
    )
    for action in iam_writes + [
        "sts:AssumeRole",
        "sts:AssumeRoleWithSAML",
        "sts:AssumeRoleWithWebIdentity",
    ]:
        if action == "iam:PassRole":
            continue
        if not (not denied_resource_ceiling(policy, action)):
            raise AssertionError(
                f"Workload ceiling permits identity mutation/role chaining: {action}"
            )
    # Require a finite explicit deny, including for future/unknown AWS APIs.
    # A blacklist of today's execution APIs misses services such as Glue or
    # Step Functions that inherit an existing role without a new PassRole.
    registry = json.loads((Path(__file__).parent / "workload-actions.json").read_text())
    supported = {
        a.lower() for a in registry["data"] + registry["execution"] + ["iam:PassRole"]
    }
    ceilings = [
        many(s["NotAction"])
        for s in policy["Statement"]
        if s["Effect"] == "Deny"
        and not s.get("Condition")
        and s.get("Resource") in ("*", ["*"])
        and "NotAction" in s
        and all(not any(c in a for c in "*?") for a in many(s["NotAction"]))
    ]
    if not (ceilings):
        raise AssertionError("Workload requires a finite explicit API deny ceiling")
    possible = set.intersection(*[{a.lower() for a in ceiling} for ceiling in ceilings])
    for action in possible - supported:
        if not (not denied_resource_ceiling(policy, action)):
            raise AssertionError(f"Unsupported workload API: {action}")
    # Supported executable services have an actual role lookup below. All
    # others must be explicitly denied until their verifier is implemented.
    for action in registry["execution"]:
        if not (denied_resource_ceiling(policy, action) <= execution_resources):
            raise AssertionError(f"Uninventoried executable capability: {action}")
    if not (denied_resource_ceiling(policy, "iam:PassRole") <= roles):
        raise AssertionError("Workload can pass an unbounded role")


def read(*args):
    return json.loads(
        subprocess.check_output(["aws", *args, "--output", "json"], text=True)
    )


def cluster_nodes(cluster, region, aws):
    token = aws(
        "eks", "get-token", "--cluster-name", cluster["name"], "--region", region
    )["status"]["token"]
    context = ssl.create_default_context(
        cadata=base64.b64decode(cluster["certificateAuthority"]["data"]).decode()
    )
    request = Request(
        cluster["endpoint"].rstrip("/") + "/api/v1/nodes",
        headers={"Authorization": "Bearer " + token},
    )
    with urlopen(request, context=context, timeout=30) as response:
        return json.load(response)["items"]


def verify_mutable_policies(config, roles, aws):
    for arn in config.get("deployment_managed_policy_arns", []):
        if not (arn.split(":")[4] == config["account_id"]):
            raise AssertionError("Cross-account mutable policy")
        if not (arn not in roles.values()):
            raise AssertionError("A workload ceiling cannot be mutable")
        # Listing also supports a not-yet-created admitted policy without
        # swallowing access errors from GetPolicy/ListEntitiesForPolicy.
        existing = aws("iam", "list-policies", "--scope", "Local")["Policies"]
        if not any(p["Arn"] == arn for p in existing):
            continue
        for usage in ("PermissionsPolicy", "PermissionsBoundary"):
            entities = aws(
                "iam",
                "list-entities-for-policy",
                "--policy-arn",
                arn,
                "--policy-usage-filter",
                usage,
            )
            if not (
                not entities.get("PolicyUsers") and not entities.get("PolicyGroups")
            ):
                raise AssertionError(f"Mutable policy reaches users/groups: {arn}")
            attached = entities.get("PolicyRoles", [])
            if not (usage != "PermissionsBoundary" or not attached):
                raise AssertionError(f"Mutable policy is an identity ceiling: {arn}")
            for role in attached:
                actual = aws("iam", "get-role", "--role-name", role["RoleName"])[
                    "Role"
                ]["Arn"]
                if not (actual in roles):
                    raise AssertionError(
                        f"Mutable policy reaches an unbounded role: {arn} -> {actual}"
                    )


def verify_inventory(config, aws=read, nodes=cluster_nodes):
    account = aws("sts", "get-caller-identity")["Account"]
    if not (account == config["account_id"]):
        raise AssertionError("Wrong AWS account")
    roles = config["deployment_role_boundaries"]
    targets = set(config["deployment_execution_resources"])
    if not (roles):
        raise AssertionError("No workload roles have been admitted")
    verify_mutable_policies(config, roles, aws)
    policies = {}
    for arn, boundary in roles.items():
        if not (arn.split(":")[4] == account and boundary.split(":")[4] == account):
            raise AssertionError("Cross-account role or ceiling")
        role = aws("iam", "get-role", "--role-name", arn.rsplit("/", 1)[1])["Role"]
        if not (
            role["Arn"] == arn
            and role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn")
            == boundary
        ):
            raise AssertionError(f"Missing/wrong ceiling: {arn}")
        if boundary not in policies:
            metadata = aws("iam", "get-policy", "--policy-arn", boundary)["Policy"]
            policy = aws(
                "iam",
                "get-policy-version",
                "--policy-arn",
                boundary,
                "--version-id",
                metadata["DefaultVersionId"],
            )["PolicyVersion"]["Document"]
            verify_ceiling(policy, set(roles), targets)
            policies[boundary] = policy
    for arn in targets:
        service, region, target_account, resource = arn.split(":", 5)[2:]
        if not (target_account == account):
            raise AssertionError("Cross-account execution target")
        if service == "lambda":
            role = aws(
                "lambda",
                "get-function-configuration",
                "--region",
                region,
                "--function-name",
                arn,
            )["Role"]
        elif service == "codebuild":
            projects = aws(
                "codebuild",
                "batch-get-projects",
                "--region",
                region,
                "--names",
                resource.split("/", 1)[1],
            )["projects"]
            if not (len(projects) == 1):
                raise AssertionError(f"Missing build project: {arn}")
            role = projects[0]["serviceRole"]
        elif service == "ec2":
            instances = aws(
                "ec2",
                "describe-instances",
                "--region",
                region,
                "--instance-ids",
                resource.split("/", 1)[1],
            )["Reservations"]
            instance = instances[0]["Instances"][0]
            profile = instance.get("IamInstanceProfile", {}).get("Arn")
            if not profile:
                continue
            profile_roles = aws(
                "iam",
                "get-instance-profile",
                "--instance-profile-name",
                profile.rsplit("/", 1)[1],
            )["InstanceProfile"]["Roles"]
            if not (len(profile_roles) == 1):
                raise AssertionError("Unexpected instance profile")
            role = profile_roles[0]["Arn"]
        else:
            raise AssertionError(f"Unsupported execution target: {arn}")
        if not (role in roles):
            raise AssertionError(
                f"Existing executable carries an unbounded role: {arn} -> {role}"
            )
    # Cluster administrators can start pods as ANY IRSA/Pod Identity service
    # account, not only those used by today's running pods. Check the providers'
    # entire local-account role trust inventory as well as node/cluster roles.
    all_roles = aws("iam", "list-roles")["Roles"]
    for cluster in config["clusters"]:
        name, region = cluster["name"], cluster["region"]
        value = aws("eks", "describe-cluster", "--name", name, "--region", region)[
            "cluster"
        ]
        if not (value["roleArn"] in roles):
            raise AssertionError(f"Unbounded EKS service role: {name}")
        issuer = value["identity"]["oidc"]["issuer"].removeprefix("https://")
        provider = f"arn:aws:iam::{account}:oidc-provider/{issuer}"
        for role in all_roles:
            trusts_cluster = issuer in json.dumps(role["AssumeRolePolicyDocument"])
            for trust in many(role["AssumeRolePolicyDocument"].get("Statement", [])):
                principal = trust.get("Principal", {})
                federated = (
                    principal.get("Federated", [])
                    if isinstance(principal, dict)
                    else principal
                )
                if trust.get("Effect") == "Allow" and matches(
                    trust, "sts:AssumeRoleWithWebIdentity"
                ):
                    trusts_cluster |= any(
                        fnmatchcase(provider, p) for p in many(federated)
                    )
            if trusts_cluster:
                if not (role["Arn"] in roles):
                    raise AssertionError(
                        f"Unbounded role trusting the cluster: {role['Arn']}"
                    )
        # Query Kubernetes itself so untagged/self-managed EC2 nodes are covered.
        for node in nodes(value, region, aws):
            provider_id = node.get("spec", {}).get("providerID", "")
            if "i-" not in provider_id:
                if not (
                    node.get("metadata", {})
                    .get("labels", {})
                    .get("eks.amazonaws.com/compute-type")
                    == "fargate"
                ):
                    raise AssertionError("Unknown node execution identity")
                continue
            instance_id = provider_id.rsplit("/", 1)[1]
            instance = aws(
                "ec2",
                "describe-instances",
                "--region",
                region,
                "--instance-ids",
                instance_id,
            )["Reservations"][0]["Instances"][0]
            profile = instance.get("IamInstanceProfile", {}).get("Arn")
            if profile:
                node_roles = aws(
                    "iam",
                    "get-instance-profile",
                    "--instance-profile-name",
                    profile.rsplit("/", 1)[1],
                )["InstanceProfile"]["Roles"]
                if not (all(r["Arn"] in roles for r in node_roles)):
                    raise AssertionError(f"Unbounded self-managed node: {instance_id}")
        for profile in aws(
            "eks", "list-fargate-profiles", "--cluster-name", name, "--region", region
        )["fargateProfileNames"]:
            fargate = aws(
                "eks",
                "describe-fargate-profile",
                "--cluster-name",
                name,
                "--fargate-profile-name",
                profile,
                "--region",
                region,
            )["fargateProfile"]
            if not (fargate["podExecutionRoleArn"] in roles):
                raise AssertionError(f"Unbounded Fargate role: {profile}")
        for nodegroup in aws(
            "eks", "list-nodegroups", "--cluster-name", name, "--region", region
        )["nodegroups"]:
            node = aws(
                "eks",
                "describe-nodegroup",
                "--cluster-name",
                name,
                "--nodegroup-name",
                nodegroup,
                "--region",
                region,
            )["nodegroup"]
            if not (node["nodeRole"] in roles):
                raise AssertionError(f"Unbounded node role: {nodegroup}")
        for association in aws(
            "eks",
            "list-pod-identity-associations",
            "--cluster-name",
            name,
            "--region",
            region,
        )["associations"]:
            binding = aws(
                "eks",
                "describe-pod-identity-association",
                "--cluster-name",
                name,
                "--association-id",
                association["associationId"],
                "--region",
                region,
            )["association"]
            if not (binding["roleArn"] in roles):
                raise AssertionError(f"Unbounded pod identity: {binding['roleArn']}")
            if not (
                not binding.get("targetRoleArn") or binding["targetRoleArn"] in roles
            ):
                raise AssertionError("Unbounded chained Pod Identity target")
    return {
        "account": account,
        "roles": len(roles),
        "ceilings": len(policies),
        "execution_targets": len(targets),
    }


def verify_webhook_code_targets(config, aws=read):
    """Lambda-only admission: verify each target's execution role is bounded."""
    account = aws("sts", "get-caller-identity")["Account"]
    if not (account == config["account_id"]):
        raise AssertionError("Wrong AWS account")
    targets = config["webhook_code_lambda_targets"]
    boundaries = config["deployment_role_boundaries"]
    if not (targets):
        raise AssertionError("No webhook code Lambda targets to verify")
    manifest = config["deployment_manifest"]
    if not (manifest["account_id"] == account):
        raise AssertionError("Deployment manifest account mismatch")
    if not (manifest["archive_prefix"] == config["webhook_code_archive_prefix"]):
        raise AssertionError("Deployment manifest archive mismatch")
    if not (
        {t["function_arn"]: t["execution_role"] for t in manifest["targets"]} == targets
    ):
        raise AssertionError("Deployment manifest target mismatch")
    if not (len(manifest["targets"]) == len(targets)):
        raise AssertionError("Duplicate deployment manifest target")
    for fn_arn, expected_role in targets.items():
        service, region, fn_account = fn_arn.split(":", 5)[2:5]
        if not (service == "lambda"):
            raise AssertionError(f"Not a Lambda ARN: {fn_arn}")
        if not (fn_account == account):
            raise AssertionError(f"Cross-account Lambda target: {fn_arn}")
        if not (region == manifest["region"]):
            raise AssertionError("Deployment manifest region mismatch")
        actual_role = aws(
            "lambda",
            "get-function-configuration",
            "--region",
            region,
            "--function-name",
            fn_arn,
        )["Role"]
        if not (actual_role == expected_role):
            raise AssertionError(
                f"Lambda {fn_arn} execution role mismatch: expected {expected_role}, got {actual_role}"
            )
        if not (expected_role in boundaries):
            raise AssertionError(
                f"Execution role not in admitted boundary map: {expected_role}"
            )
        boundary = boundaries[expected_role]
        if not (
            expected_role.split(":")[4] == account and boundary.split(":")[4] == account
        ):
            raise AssertionError("Cross-account role or ceiling")
        role = aws("iam", "get-role", "--role-name", expected_role.rsplit("/", 1)[1])[
            "Role"
        ]
        if not (
            role["Arn"] == expected_role
            and role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn")
            == boundary
        ):
            raise AssertionError("Missing/wrong ceiling")
        metadata = aws("iam", "get-policy", "--policy-arn", boundary)["Policy"]
        policy = aws(
            "iam",
            "get-policy-version",
            "--policy-arn",
            boundary,
            "--version-id",
            metadata["DefaultVersionId"],
        )["PolicyVersion"]["Document"]
        # Code deployment inherits execution authority. Admit only bounded data
        # roles; transitive execution/PassRole requires a separate reviewed profile.
        verify_ceiling(policy, set(), set())
    return {"account": account, "targets": len(targets)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path, nargs="?")
    parser.add_argument("--terraform", action="store_true")
    parser.add_argument("--webhook-code", action="store_true")
    args = parser.parse_args()
    if args.webhook_code:
        config = json.loads(json.load(sys.stdin)["inventory"])
        result = verify_webhook_code_targets(config)
        print(json.dumps({"verified": "true", "inventory": json.dumps(result)}))
    elif args.terraform:
        config = json.loads(json.load(sys.stdin)["inventory"])
        result = verify_inventory(config)
        print(json.dumps({"verified": "true", "inventory": json.dumps(result)}))
    else:
        if args.inventory is None:
            parser.error("inventory is required")
        print(
            json.dumps(
                verify_inventory(json.loads(args.inventory.read_text())), indent=2
            )
        )


if __name__ == "__main__":
    main()
