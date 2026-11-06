#!/usr/bin/env python3
"""Prepare/apply per-environment evaluator OIDC, EC2 and gateway-observer access.

Default mode only reads metadata and renders private files. --apply consumes a
saved Terraform plan, installs two-object observer RBAC, then publishes GitHub
secrets. --delete previews credential removal with evidence retained; add --apply
to execute. Run with an infrastructure operator identity, never the evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.e2e.cli_uplift import config as evaluator_config  # noqa: E402

SLUG = re.compile(r"[a-z][a-z0-9-]{0,62}")
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?")


class SetupError(ValueError):
    pass


def require(value, message):
    if not value:
        raise SetupError(message)


def read_json(path):
    value = json.loads(Path(path).read_text())
    require(isinstance(value, dict), "Configuration must be a JSON object")
    return value


def run(args, *, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True)
    # Provider/gh errors can echo input or private configuration. Keep them local.
    require(
        result.returncode == 0, f"{args[0]} command failed (exit {result.returncode})"
    )
    return result.stdout


def gh_api(path, *, method="GET", body=None):
    args = ["gh", "api", path, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    raw = run(args, data=json.dumps(body) if body is not None else None)
    return json.loads(raw) if raw.strip() else {}


def environment_status(repository, target):
    # List first so an authorization failure cannot be mistaken for absence.
    found = None
    page = 1
    while True:
        rows = gh_api(f"repos/{repository}/environments?per_page=100&page={page}")[
            "environments"
        ]
        found = next((e for e in rows if e["name"] == target), found)
        if len(rows) < 100:
            break
        page += 1
    if found is None:
        return None
    policy = found.get("deployment_branch_policy")
    if policy is None:
        require(
            all(
                r["type"] in {"wait_timer", "required_reviewers"}
                for r in found.get("protection_rules", [])
            ),
            "Existing environment has unsupported protection rules; configure main-only access separately",
        )
        return found
    require(
        policy.get("custom_branch_policies") is True
        and not policy.get("protected_branches"),
        "Existing GitHub environment must restrict deployment to the main branch",
    )
    rules = gh_api(
        f"repos/{repository}/environments/{target}/deployment-branch-policies?per_page=100"
    )
    require(
        rules.get("total_count") == 1
        and rules["branch_policies"][0].get("name") == "main"
        and rules["branch_policies"][0].get("type", "branch") == "branch",
        "Existing environment must allow exactly the main branch; protections were not changed",
    )
    return found


def check_variable_overrides(repository, target, exists, cfg):
    effective = {}
    paths = [f"repos/{repository}/actions/variables"]
    if exists:
        paths.append(f"repos/{repository}/environments/{target}/variables")
    for path in paths:
        page = 1
        while True:
            rows = gh_api(f"{path}?per_page=100&page={page}")["variables"]
            effective.update({v["name"]: v["value"] for v in rows})
            if len(rows) < 100:
                break
            page += 1
    for variable, key in evaluator_config.OVERLAY.items():
        if key in {
            "instance_profile",
            "instance_security_group_id",
            "state_bucket",
            "state_kms_key_id",
            "destination_role_arn",
            "provisioner_role_arn",
            "credential_secret_name",
        }:
            require(
                not effective.get(variable) or effective[variable] == cfg.get(key, ""),
                f"Conflicting {variable} override; remove it or set the matching target value before setup",
            )


def protect_environment(repository, target):
    existing = environment_status(repository, target)
    if existing and existing.get("deployment_branch_policy"):
        return  # Preserve every setting of an already protected environment.
    body = {
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        }
    }
    if existing:
        body["can_admins_bypass"] = existing.get("can_admins_bypass", True)
        for rule in existing.get("protection_rules", []):
            if rule["type"] == "wait_timer":
                body["wait_timer"] = rule["wait_timer"]
            elif rule["type"] == "required_reviewers":
                body["prevent_self_review"] = rule.get("prevent_self_review", False)
                body["reviewers"] = [
                    {"type": item["type"], "id": item["reviewer"]["id"]}
                    for item in rule["reviewers"]
                ]
    gh_api(f"repos/{repository}/environments/{target}", method="PUT", body=body)
    gh_api(
        f"repos/{repository}/environments/{target}/deployment-branch-policies",
        method="POST",
        body={"name": "main", "type": "branch"},
    )
    verified = environment_status(repository, target)
    require(
        verified and verified.get("deployment_branch_policy"),
        "Environment protection verification failed",
    )


def resource_environment(args):
    return getattr(args, "resource_environment", None) or args.environment


def validate_inputs(args, bindings, catalog):
    require(
        SLUG.fullmatch(args.environment),
        "Environment must be a lowercase slug of 1–63 characters",
    )
    require(
        SLUG.fullmatch(resource_environment(args)),
        "Resource environment must be a lowercase slug of 1–63 characters",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository),
        "Specify owner/repository",
    )
    require(
        re.fullmatch(r"[0-9]{12}", args.account),
        "Specify the expected 12-digit account",
    )
    require(
        not args.account.startswith("000000000") and args.account != "123456789012",
        "Illustrative accounts are not deployment targets",
    )
    require(
        all(bindings.get(k) for k in evaluator_config.REQUIRED),
        "Private bindings must explicitly supply every required evaluator target field",
    )
    evaluator_config.no_secrets(catalog, "catalog")
    cfg = evaluator_config.validate(bindings)
    require(
        cfg["platform_account"] == args.account,
        "Bindings account does not match --account",
    )
    require(
        cfg.get("gateway_deployment") in catalog,
        "Supply a catalog entry for gateway_deployment",
    )
    require(
        cfg.get("credential_secret_name") and cfg.get("instance_security_group_id"),
        "Supply a dedicated credential secret name and evaluator security group",
    )
    selected = catalog[cfg["gateway_deployment"]]
    require(
        isinstance(selected, dict)
        and all(
            selected.get(k) == v
            for k, v in {
                "account": args.account,
                "region": cfg["region"],
                "gateway_url": cfg["gateway_url"],
            }.items()
        ),
        "Gateway catalog does not match the account, region and URL in bindings",
    )
    for key in ("cluster", "namespace", "deployment", "service", "container"):
        require(
            isinstance(selected.get(key), str) and NAME.fullmatch(selected[key]),
            f"Invalid catalog {key}",
        )
    require(
        selected.get("cluster_arn")
        == f"arn:aws:eks:{cfg['region']}:{args.account}:cluster/{selected['cluster']}",
        "Catalog cluster ARN mismatch",
    )
    prefix = f"{args.account}.dkr.ecr.{cfg['region']}.amazonaws.com/"
    repo = selected.get("image_repository", "")
    require(
        repo.startswith(prefix)
        and re.fullmatch(r"[a-z0-9][a-z0-9/_.-]*", repo[len(prefix) :]),
        "Catalog image repository must belong to the target account and region",
    )
    require(
        isinstance(selected.get("selector"), dict) and bool(selected["selector"]),
        "Catalog selector is required",
    )
    # Historical build receipts need additional source-bucket access. New targets use immutable ECR tags.
    require(
        not selected.get("images"),
        "Use an empty images map and immutable ECR source tags for new targets",
    )
    roles = [
        cfg[k] for k in ("destination_role_arn", "provisioner_role_arn") if cfg.get(k)
    ]
    for arn in roles:
        require(
            re.fullmatch(
                rf"arn:aws:iam::{cfg['destination_account']}:role/[A-Za-z0-9_+=,.@/-]+",
                arn,
            ),
            "Destination access requires exact role ARNs in the configured destination account",
        )
    token = hashlib.sha256(
        f"{args.repository}:{args.environment}".encode()
    ).hexdigest()[:10]
    name = f"adp-eval-{args.environment[:20]}-{token}"
    bucket = f"{name}-{args.account}"
    cfg["evaluation_target"] = name
    for key, expected in (
        ("instance_profile", name + "-instance"),
        ("state_bucket", bucket),
        ("state_kms_key_id", "alias/" + name + "-state"),
    ):
        require(
            not bindings.get(key) or bindings[key] == expected,
            f"Remove {key} from input for first setup, or use this setup's generated value",
        )
        cfg[key] = expected
    return cfg, selected, name, sorted(set(roles))


def discover(session, args, cfg, selected, name):
    require(
        session.client("sts").get_caller_identity()["Account"] == args.account,
        "Active AWS identity does not match --account; no changes made",
    )
    bucket = session.client("s3").head_bucket(
        Bucket=args.state_bucket, ExpectedBucketOwner=args.account
    )
    bucket_region = bucket.get("BucketRegion") or bucket.get(
        "ResponseMetadata", {}
    ).get("HTTPHeaders", {}).get("x-amz-bucket-region")
    require(
        bucket_region == cfg["region"],
        "Terraform backend bucket must be in the target region",
    )
    ec2 = session.client("ec2")
    subnet = ec2.describe_subnets(SubnetIds=[cfg["private_subnet_id"]])["Subnets"][0]
    group = ec2.describe_security_groups(GroupIds=[cfg["instance_security_group_id"]])[
        "SecurityGroups"
    ][0]
    require(
        subnet["VpcId"] == group["VpcId"] == cfg["vpc_id"]
        and subnet["OwnerId"] == group["OwnerId"] == args.account,
        "Evaluator subnet/security group must belong to the selected account and VPC",
    )
    require(
        not group.get("IpPermissions") and not subnet.get("MapPublicIpOnLaunch"),
        "Evaluator requires a no-ingress security group and subnet without automatic public IPs",
    )
    secret = session.client("secretsmanager").describe_secret(
        SecretId=cfg["credential_secret_name"]
    )
    require(
        secret.get("Name") == cfg["credential_secret_name"]
        and secret["ARN"].startswith(
            f"arn:aws:secretsmanager:{cfg['region']}:{args.account}:secret:"
        )
        and not secret.get("DeletedDate"),
        "Fixture secret must exist in the selected account and region",
    )
    pool = session.client("cognito-idp").describe_user_pool(
        UserPoolId=cfg["cognito_user_pool_id"]
    )["UserPool"]
    require(
        pool.get("Arn")
        == f"arn:aws:cognito-idp:{cfg['region']}:{args.account}:userpool/{cfg['cognito_user_pool_id']}",
        "Cognito pool must belong to the selected target",
    )
    cluster = session.client("eks").describe_cluster(name=selected["cluster"])[
        "cluster"
    ]
    require(
        cluster["arn"] == selected["cluster_arn"]
        and cluster["status"] == "ACTIVE"
        and cluster.get("accessConfig", {}).get("authenticationMode")
        in ("API", "API_AND_CONFIG_MAP"),
        "Cluster must be active with EKS access entries enabled",
    )
    repo = session.client("ecr").describe_repositories(
        repositoryNames=[selected["image_repository"].split("/", 1)[1]]
    )["repositories"][0]
    require(
        repo["repositoryUri"] == selected["image_repository"]
        and repo["imageTagMutability"] == "IMMUTABLE",
        "Gateway source tags must use an immutable ECR repository",
    )
    kms = ""
    if secret.get("KmsKeyId") and secret["KmsKeyId"] != "alias/aws/secretsmanager":
        kms = session.client("kms").describe_key(KeyId=secret["KmsKeyId"])[
            "KeyMetadata"
        ]["Arn"]
    iam = session.client("iam")
    try:
        provider = iam.get_open_id_connect_provider(
            OpenIDConnectProviderArn=f"arn:aws:iam::{args.account}:oidc-provider/token.actions.githubusercontent.com"
        )
        require(
            provider["Url"] == "token.actions.githubusercontent.com"
            and "sts.amazonaws.com" in provider["ClientIDList"],
            "Existing GitHub OIDC provider has incompatible settings",
        )
        # Preserve ownership on repeat runs; do not plan deletion of our shared provider.
        create_oidc = any(
            t["Key"] == "adp:evaluator-owner" and t["Value"] == name
            for t in provider.get("Tags", [])
        )
    except iam.exceptions.NoSuchEntityException:
        create_oidc = True
    return secret["ARN"], kms, create_oidc, cluster


def observer_objects(selected, name):
    metadata = {
        "name": name,
        "namespace": selected["namespace"],
        "labels": {"app.kubernetes.io/managed-by": "adp-evaluator-setup"},
    }
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": metadata,
                "rules": [
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments"],
                        "resourceNames": [selected["deployment"]],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["services"],
                        "resourceNames": [selected["service"]],
                        "verbs": ["get"],
                    },
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": metadata,
                "subjects": [
                    {
                        "kind": "Group",
                        "name": "adp:evaluator:" + name,
                        "apiGroup": "rbac.authorization.k8s.io",
                    }
                ],
                "roleRef": {
                    "kind": "Role",
                    "name": name,
                    "apiGroup": "rbac.authorization.k8s.io",
                },
            },
        ],
    }


def check_kubernetes(kubeconfig, cluster, selected, name):
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]
    context = json.loads(run(command + ["config", "view", "--minify", "-o", "json"]))
    require(
        context["clusters"][0]["cluster"]["server"] == cluster["endpoint"],
        "Kubeconfig server does not match the selected EKS cluster",
    )
    command += ["-n", selected["namespace"]]
    for resource in ("roles", "rolebindings"):
        existing = json.loads(
            run(command + ["get", resource, name, "--ignore-not-found", "-o", "json"])
            or "{}"
        )
        if existing:
            require(
                existing.get("metadata", {})
                .get("labels", {})
                .get("app.kubernetes.io/managed-by")
                == "adp-evaluator-setup",
                "Refusing to replace an observer object owned by another tool",
            )
        verb = "patch" if existing else "create"
        require(
            run(command + ["auth", "can-i", verb, resource]).strip() == "yes",
            "Operator lacks Kubernetes RBAC installation access",
        )
    return command


def reject_destructive_plan(plan):
    require(
        not any(
            "delete" in r["change"]["actions"] for r in plan.get("resource_changes", [])
        ),
        "Setup plan would delete or replace resources; refusing apply",
    )


def publish(repository, target, cfg, catalog, arn, out=None):
    # Publish the role last, after all access and bindings are in place. No values in argv/logs.
    for key, value in (
        ("CLI_UPLIFT_EVAL_BINDINGS_JSON", json.dumps(cfg)),
        ("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", json.dumps(catalog)),
        ("AWS_CLI_UPLIFT_EVAL_ROLE_ARN", arn),
    ):
        run(
            ["gh", "secret", "set", key, "--repo", repository, "--env", target],
            data=value,
        )
        if out is not None:
            receipt = out / "published-secrets.json"
            owned = read_json(receipt) if receipt.exists() else {}
            owned[key] = gh_api(
                f"repos/{repository}/environments/{target}/secrets/{key}"
            )
            temporary = out / "published-secrets.tmp"
            temporary.write_text(json.dumps(owned, indent=2) + "\n")
            temporary.replace(receipt)


def validate_output_directory(out, target):
    if not out.exists() or not any(out.iterdir()):
        return
    marker = out / "setup-target.json"

    def normalized(value):
        return {
            **value,
            "resource_environment": value.get(
                "resource_environment", value.get("environment")
            ),
        }

    require(
        marker.is_file()
        and not marker.is_symlink()
        and normalized(read_json(marker)) == normalized(target),
        "Output directory belongs to another setup or has no setup marker; use a new private directory",
    )
    for path in out.iterdir():
        require(not path.is_symlink(), "Setup output must not contain symlinks")
        if path.name.endswith((".tf", ".tf.json", ".tfvars", ".tfvars.json")):
            require(
                path.name in {"main.tf", "terraform.tfvars.json"},
                "Output directory contains unrelated Terraform configuration",
            )


SECRET_NAMES = (
    "AWS_CLI_UPLIFT_EVAL_ROLE_ARN",
    "CLI_UPLIFT_EVAL_BINDINGS_JSON",
    "CLI_UPLIFT_EVAL_GATEWAY_CATALOG",
)


def owned_secrets(repository, target, out):
    receipt = out / "published-secrets.json"
    owned = read_json(receipt) if receipt.exists() else {}
    found = {}
    page = 1
    while True:
        rows = gh_api(
            f"repos/{repository}/environments/{target}/secrets?per_page=100&page={page}"
        )["secrets"]
        found.update({s["name"]: s for s in rows})
        if len(rows) < 100:
            break
        page += 1
    result = []
    for key in SECRET_NAMES:
        if key not in found:
            continue  # An interrupted delete is safe to repeat.
        require(
            all(
                found[key].get(field)
                and found[key][field] == owned.get(key, {}).get(field)
                for field in ("name", "created_at", "updated_at")
            ),
            "Evaluator secret has no matching publication receipt or was replaced; refusing deletion",
        )
        result.append(key)
    return result


def check_idle(session, account, name):
    ec2 = session.client("ec2")
    # Check both ownership tags and profile use, including stopped instances.
    for field, value in (
        ("tag:adp:evaluator-target", name),
        (
            "iam-instance-profile.arn",
            f"arn:aws:iam::{account}:instance-profile/{name}-instance",
        ),
    ):
        pages = ec2.get_paginator("describe_instances").paginate(
            Filters=[
                {"Name": field, "Values": [value]},
                {
                    "Name": "instance-state-name",
                    "Values": [
                        "pending",
                        "running",
                        "stopping",
                        "stopped",
                        "shutting-down",
                    ],
                },
            ]
        )
        require(
            not any(r.get("Instances") for p in pages for r in p["Reservations"]),
            "Evaluator instances remain; finish evaluation cleanup before deleting credentials",
        )


def check_delete_plan(plan, account, name, selected):
    role = f"{name}-oidc"
    worker = f"{name}-instance"
    allowed = {
        "aws_iam_role.evaluator[0]": {
            "name": role,
            "arn": f"arn:aws:iam::{account}:role/{role}",
        },
        "aws_iam_role.instance[0]": {
            "name": worker,
            "arn": f"arn:aws:iam::{account}:role/{worker}",
        },
        "aws_iam_instance_profile.instance[0]": {
            "name": worker,
            "arn": f"arn:aws:iam::{account}:instance-profile/{worker}",
        },
        "aws_iam_role_policy.evaluator[0]": {
            "role": role,
            "name": "bounded-evaluation-and-gateway-observer",
        },
        "aws_iam_role_policy.instance[0]": {
            "role": worker,
            "name": "evaluation-bundle-and-fixture",
        },
        "aws_iam_role_policy_attachment.ssm[0]": {
            "role": worker,
            "policy_arn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        },
        "aws_eks_access_entry.evaluator[0]": {
            "cluster_name": selected["cluster"],
            "principal_arn": f"arn:aws:iam::{account}:role/{role}",
        },
    }
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        if change["actions"] == ["no-op"]:
            continue
        expected = allowed.get(resource["address"])
        before = change.get("before") or {}
        require(
            change["actions"] == ["delete"]
            and expected
            and all(before.get(k) == v for k, v in expected.items()),
            "Delete plan includes changes outside the selected evaluator credentials; refusing apply",
        )
        if resource["type"] in {
            "aws_iam_role",
            "aws_iam_instance_profile",
            "aws_eks_access_entry",
        }:
            require(
                before.get("tags_all", {}).get("adp:evaluator-owner") == name
                and before.get("tags_all", {}).get("adp:managed-by")
                == "evaluator-setup",
                "Delete plan contains credentials without matching ownership tags",
            )


def delete_observer_command(kubeconfig, cluster, selected, name):
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]
    context = json.loads(run(command + ["config", "view", "--minify", "-o", "json"]))
    require(
        context["clusters"][0]["cluster"]["server"] == cluster["endpoint"],
        "Kubeconfig server does not match the selected EKS cluster",
    )
    command += ["-n", selected["namespace"]]
    for expected in observer_objects(selected, name)["items"]:
        kind = expected["kind"]
        current = json.loads(
            run(command + ["get", kind, name, "--ignore-not-found", "-o", "json"])
            or "{}"
        )
        if not current:
            continue
        require(
            current.get("metadata", {})
            .get("labels", {})
            .get("app.kubernetes.io/managed-by")
            == "adp-evaluator-setup"
            and all(
                current.get(k) == v
                for k, v in expected.items()
                if k not in {"metadata", "apiVersion", "kind"}
            ),
            "Observer object ownership or access has changed; refusing deletion",
        )
        require(
            run(command + ["auth", "can-i", "delete", kind]).strip() == "yes",
            "Operator lacks Kubernetes RBAC deletion access",
        )
    return command


def delete_credentials(args, session, cfg, selected, name, out, target):
    require(
        (out / "setup-target.json").is_file(),
        "Delete requires the original private setup directory and ownership records",
    )
    require(
        args.kubeconfig,
        "--delete requires an operator --kubeconfig for preview and apply",
    )
    require(
        session.client("sts").get_caller_identity()["Account"] == args.account,
        "Active AWS identity does not match --account; no changes made",
    )
    variables = read_json(out / "terraform.tfvars.json")
    original = variables.get("config", {})
    require(
        (original.get("resource_environment") or original.get("target"))
        == resource_environment(args),
        "Saved setup uses a different AWS resource environment",
    )
    require(
        all(
            original.get(k) == v
            for k, v in {
                "account": args.account,
                "repository": args.repository,
                "target": args.environment,
                "region": cfg["region"],
                "prefix": name,
                "bucket": cfg["state_bucket"],
                "cluster": selected["cluster"],
                "namespace": selected["namespace"],
            }.items()
        )
        and read_json(out / "observer.json") == observer_objects(selected, name),
        "Saved setup does not match the selected evaluator target",
    )
    backend = {
        "bucket": target["state_bucket"],
        "key": f"evaluator-setup/{name}.tfstate",
        "region": cfg["region"],
        "encrypt": True,
        "use_lockfile": True,
        "allowed_account_ids": [args.account],
    }
    if args.profile:
        backend["profile"] = args.profile
        os.environ["AWS_PROFILE"] = args.profile
    os.environ["AWS_REGION"] = cfg["region"]
    os.environ["AWS_DEFAULT_REGION"] = cfg["region"]
    # An existing state is mandatory; never treat a wrong/missing backend as empty.
    session.client("s3").head_object(
        Bucket=args.state_bucket, Key=backend["key"], ExpectedBucketOwner=args.account
    )
    check_idle(session, args.account, name)
    secrets = owned_secrets(args.repository, args.environment, out)
    cluster = session.client("eks").describe_cluster(name=selected["cluster"])[
        "cluster"
    ]
    require(cluster["arn"] == selected["cluster_arn"], "EKS cluster identity mismatch")
    kubectl = delete_observer_command(args.kubeconfig, cluster, selected, name)
    for filename in ("main.tf", ".terraform.lock.hcl"):
        shutil.copyfile(ROOT / "platform/evaluator-infra" / filename, out / filename)
    (out / "backend.json").write_text(json.dumps(backend, indent=2) + "\n")
    tf = ["terraform", f"-chdir={out}"]
    run(tf + ["init", "-input=false", "-reconfigure", "-backend-config=backend.json"])
    plan = run(
        tf
        + [
            "plan",
            "-input=false",
            "-var=credentials_enabled=false",
            f"-lock={'true' if args.apply else 'false'}",
            "-out=delete.tfplan",
        ]
    )
    (out / "delete-plan.txt").write_text(plan)
    check_delete_plan(
        json.loads(run(tf + ["show", "-json", "delete.tfplan"])),
        args.account,
        name,
        selected,
    )
    print(
        "Delete plan: remove evaluator roles/profile, EKS access, observer RBAC and setup-owned GitHub secrets."
    )
    print(
        "Retain evidence bucket, KMS key/alias, shared OIDC, backend state, fixture credentials and GitHub environment."
    )
    if not args.apply:
        print(
            f"Preview only. Review {out / 'delete-plan.txt'}; repeat with --delete --apply to remove access."
        )
        return 0
    # Recheck immediately before mutation. Role secret is removed first to stop new consumers.
    check_idle(session, args.account, name)
    require(
        owned_secrets(args.repository, args.environment, out) == secrets,
        "Secrets changed during planning; retry",
    )
    if SECRET_NAMES[0] in secrets:
        gh_api(
            f"repos/{args.repository}/environments/{args.environment}/secrets/{SECRET_NAMES[0]}",
            method="DELETE",
        )
    run(tf + ["apply", "-input=false", "delete.tfplan"])
    kubectl = delete_observer_command(args.kubeconfig, cluster, selected, name)
    for kind in ("RoleBinding", "Role"):
        run(kubectl + ["delete", kind, name, "--ignore-not-found=true", "--wait=true"])
    for key in secrets:
        if key != SECRET_NAMES[0]:
            gh_api(
                f"repos/{args.repository}/environments/{args.environment}/secrets/{key}",
                method="DELETE",
            )
    require(
        not owned_secrets(args.repository, args.environment, out),
        "Evaluator secrets remain after deletion",
    )
    print(
        "Evaluator credentials and observer access removed. Evidence and shared resources retained; storage charges may continue."
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in (
        "account",
        "repository",
        "environment",
        "bindings",
        "catalog",
        "state-bucket",
        "output-dir",
    ):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument(
        "--profile",
        help="AWS operator profile (otherwise standard AWS credential chain)",
    )
    parser.add_argument(
        "--resource-environment",
        help="AWS resource namespace for gateway parameters (defaults to --environment)",
    )
    parser.add_argument(
        "--kubeconfig",
        help="Operator access to the target cluster; required for --apply",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Remove evaluator credentials/access; retain evidence and shared resources",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    bindings, catalog = read_json(args.bindings), read_json(args.catalog)
    cfg, selected, name, roles = validate_inputs(args, bindings, catalog)
    out = Path(args.output_dir).expanduser().resolve()
    require(
        not out.is_relative_to(ROOT),
        "Keep generated private configuration outside the checkout",
    )
    require(
        not args.apply or args.kubeconfig, "--apply requires an operator --kubeconfig"
    )
    target = {
        "account": args.account,
        "repository": args.repository,
        "environment": args.environment,
        "resource_environment": resource_environment(args),
        "region": cfg["region"],
        "state_bucket": args.state_bucket,
    }
    validate_output_directory(out, target)
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=cfg["region"])
    if args.delete:
        return delete_credentials(args, session, cfg, selected, name, out, target)
    fixture, kms, create_oidc, cluster = discover(session, args, cfg, selected, name)
    exists = environment_status(args.repository, args.environment)
    check_variable_overrides(args.repository, args.environment, exists, cfg)
    kubectl = (
        check_kubernetes(args.kubeconfig, cluster, selected, name)
        if args.apply
        else None
    )
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    (out / "setup-target.json").write_text(json.dumps(target, indent=2) + "\n")
    for filename in ("main.tf", ".terraform.lock.hcl"):
        shutil.copyfile(ROOT / "platform/evaluator-infra" / filename, out / filename)
    variables = {
        "config": {
            "account": args.account,
            "region": cfg["region"],
            "repository": args.repository,
            "target": args.environment,
            "resource_environment": resource_environment(args),
            "prefix": name,
            "bucket": cfg["state_bucket"],
            "cluster": selected["cluster"],
            "namespace": selected["namespace"],
            "image_repository": selected["image_repository"].split("/", 1)[1],
            "subnet": cfg["private_subnet_id"],
            "security_group": cfg["instance_security_group_id"],
            "user_pool": cfg["cognito_user_pool_id"],
            "fixture_arn": fixture,
            "fixture_kms_arn": kms,
            "instance_type": cfg["instance_type"],
            "destination_roles": roles,
            "create_oidc": create_oidc,
        }
    }
    backend = {
        "bucket": args.state_bucket,
        "key": f"evaluator-setup/{name}.tfstate",
        "region": cfg["region"],
        "encrypt": True,
        "use_lockfile": True,
        "allowed_account_ids": [args.account],
    }
    if args.profile:
        backend["profile"] = args.profile
    for filename, data in (
        ("terraform.tfvars.json", variables),
        ("backend.json", backend),
        ("bindings.json", cfg),
        ("catalog.json", catalog),
        ("observer.json", observer_objects(selected, name)),
    ):
        (out / filename).write_text(json.dumps(data, indent=2) + "\n")
        (out / filename).chmod(0o600)
    print(f"Prepared private evaluator setup in {out}. No AWS or GitHub changes made.")
    if not args.apply:
        print(
            "Review main.tf, terraform.tfvars.json and observer.json; repeat with --apply and --kubeconfig to install."
        )
        return 0
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    os.environ["AWS_REGION"] = cfg["region"]
    os.environ["AWS_DEFAULT_REGION"] = cfg["region"]
    tf = ["terraform", f"-chdir={out}"]
    run(tf + ["init", "-input=false", "-reconfigure", "-backend-config=backend.json"])
    plan_output = run(tf + ["plan", "-input=false", "-out=setup.tfplan"])
    (out / "plan.txt").write_text(plan_output)
    reject_destructive_plan(json.loads(run(tf + ["show", "-json", "setup.tfplan"])))
    protect_environment(args.repository, args.environment)
    run(tf + ["apply", "-input=false", "setup.tfplan"])
    run(kubectl + ["apply", "-f", str(out / "observer.json")])
    outputs = json.loads(run(tf + ["output", "-json"]))
    expected_arn = f"arn:aws:iam::{args.account}:role/{name}-oidc"
    require(
        outputs["role_arn"]["value"] == expected_arn
        and outputs["instance_profile"]["value"] == cfg["instance_profile"]
        and outputs["state_bucket"]["value"] == cfg["state_bucket"]
        and outputs["state_kms_key_id"]["value"] == cfg["state_kms_key_id"],
        "Terraform outputs do not match the selected target",
    )
    publish(args.repository, args.environment, cfg, catalog, expected_arn, out)
    print(
        "Evaluator identity and private bindings installed. Run the snapshot/login checkpoint to verify OIDC and connectivity."
    )
    print(
        "Full regression still requires separate checks/browser/deploy roles and owned scenario fixtures; see the setup guide."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print a provider exception or subprocess stderr containing private input.
        code = (
            getattr(exc, "response", {})
            .get("Error", {})
            .get("Code", type(exc).__name__)
        )
        operation = getattr(exc, "operation_name", "validation")
        print(
            str(exc)
            if isinstance(exc, SetupError)
            else f"Setup failed: {operation} ({code})",
            file=sys.stderr,
        )
        raise SystemExit(1)
