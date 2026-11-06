#!/usr/bin/env python3
"""Protected GitLab registration workflow. No credentials or API bodies in logs."""

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import zipfile
from urllib.parse import quote

from registration_delivery import (
    LIVE_CHECKS, check_reviewed_instance_plan, expected_observations,
    observe_ca_root, observe_registration_groups, private_receipt, target_binding, verify,
)

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
MAX_OBJECT = 32 * 1024 * 1024
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
        output.write(data)


def identity(session, role):
    actual = session.client("sts").get_caller_identity()
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_-]+)", role)
    require(match and actual["Account"] == match[1]
            and actual["Arn"].startswith(f"arn:aws:sts::{match[1]}:assumed-role/{match[2]}/"),
            "registration_identity_mismatch")
    return actual


def validate_config(config, env):
    """All target/plan/policy expectations originate in protected configuration."""
    required = {"schema_version", "account_id", "region", "environment", "source_revision", "correlation",
                "registrar_role_arn", "exporter_role_arn", "instance_id", "instance_url", "targets", "backend",
                "artifacts", "product", "qualification", "probe_operation_id", "ca", "admin_secret_arn",
                "admin_user_id", "secret_policy_sha256", "instance_change_approval"}
    require(isinstance(config, dict) and set(config) == required and config["schema_version"] == 1,
            "registration_config_schema_invalid")
    for key, variable in (("account_id", "ACCOUNT_ID"), ("region", "AWS_REGION"),
                          ("environment", "ENVIRONMENT"), ("source_revision", "ADP_SOURCE_REVISION"),
                          ("correlation", "ADP_CORRELATION")):
        require(config[key] == env[variable], "registration_config_binding_mismatch")
    require(config["registrar_role_arn"] == env["ADP_DEPLOY_ROLE_ARN"]
            and config["exporter_role_arn"] != config["registrar_role_arn"]
            and re.fullmatch(r"[0-9]{12}", config["account_id"])
            and re.fullmatch(r"[a-z][a-z0-9-]{0,23}", config["environment"])
            and re.fullmatch(r"[0-9a-f]{40}", config["source_revision"])
            and re.fullmatch(r"i-[0-9a-f]{8,17}", config["instance_id"]), "registration_target_invalid")
    account, region, environment = config["account_id"], config["region"], config["environment"]
    for role in (config["registrar_role_arn"], config["exporter_role_arn"]):
        require(re.fullmatch(fr"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_-]+", role), "registration_role_invalid")
    backend = config["backend"]
    require(backend == {"bucket": f"adp-terraform-state-{account}",
                        "key": f"{environment}/modules/gitlab/terraform.tfstate", "region": region,
                        "encrypt": True, "dynamodb_table": "adp-terraform-locks"},
            "original_gitlab_backend_required")
    targets, _ = target_binding({"ADP_REGISTRATION_TARGETS_JSON": json.dumps(config["targets"])})
    group_fields = {"tenant_id", "group_id", "tag_list", "access_level", "operation_id", "token_secret_arn"}
    for name, group_id in (("product", targets["tenant_parent"]), ("qualification", targets["qualification_parent"])):
        item = config[name]
        fields = group_fields | ({"product_id", "product_group_id", "project_id"} if name == "product" else set())
        require(isinstance(item, dict) and set(item) == fields and item["group_id"] == group_id
                and isinstance(item["tenant_id"], str) and 0 < len(item["tenant_id"]) <= 256
                and isinstance(item["tag_list"], list) and f"adp-group-{group_id}" in item["tag_list"]
                and len(set(item["tag_list"])) == len(item["tag_list"])
                and all(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tag)
                        and (not tag.startswith("adp-group-") or tag == f"adp-group-{group_id}") for tag in item["tag_list"])
                and item["access_level"] in {"ref_protected", "not_protected"}
                and re.fullmatch(UUID, item["operation_id"])
                and re.fullmatch(fr"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/gitlab-runner/{group_id}-[A-Za-z0-9]{{6}}", item["token_secret_arn"]),
                "registration_group_binding_invalid")
    require(config["product"]["product_group_id"] == targets["product_subgroup"]
            and config["product"]["project_id"] == targets["project"]
            and config["product"]["tenant_id"] != config["qualification"]["tenant_id"]
            and re.fullmatch(UUID, config["probe_operation_id"])
            and len({config["product"]["operation_id"], config["qualification"]["operation_id"], config["probe_operation_id"]}) == 3,
            "registration_operation_binding_invalid")
    ca = config["ca"]
    require(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?(?:/[A-Za-z0-9_-]+)*/?", config["instance_url"]),
            "registration_instance_url_invalid")
    require(set(ca) == {"certificate_arn", "runner_url", "ca_secret_arn", "ca_version"}
            and re.fullmatch(fr"arn:aws:acm:{region}:{account}:certificate/{UUID}", ca["certificate_arn"])
            and re.fullmatch(fr"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/gitlab/ca-chain-[A-Za-z0-9]{{6}}", ca["ca_secret_arn"])
            and re.fullmatch(UUID, ca["ca_version"])
            and re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?(?:/[A-Za-z0-9_-]+)*/?", ca["runner_url"]),
            "registration_ca_binding_invalid")
    require(re.fullmatch(fr"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/gitlab-admin-token-[A-Za-z0-9]{{6}}", config["admin_secret_arn"])
            and type(config["admin_user_id"]) is int and config["admin_user_id"] > 0,
            "registration_admin_binding_invalid")
    policy_arns = {config["admin_secret_arn"], config["product"]["token_secret_arn"], config["qualification"]["token_secret_arn"]}
    require(set(config["secret_policy_sha256"]) == policy_arns
            and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in config["secret_policy_sha256"].values()),
            "reviewed_secret_policy_digests_required")
    require(set(config["artifacts"]) == {"tfplan", "variables", "approved_root"}, "reviewed_artifacts_required")
    for artifact in config["artifacts"].values():
        require(set(artifact) == {"bucket", "key", "version_id", "sha256"}
                and artifact["bucket"] == f"adp-{environment}-deployment-evidence-{account}"
                and re.fullmatch(r"gitlab-registration/inputs/[A-Za-z0-9/_.-]+", artifact["key"])
                and ".." not in artifact["key"].split("/")
                and re.fullmatch(r"[A-Za-z0-9_.~%-]{1,256}", artifact["version_id"])
                and artifact["version_id"] != "null"
                and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]), "reviewed_artifact_invalid")
    require(config["artifacts"]["approved_root"]["sha256"] == targets["approved_root_sha256"], "approved_root_mismatch")
    return config


def instance_gate(config):
    approval = config["instance_change_approval"]
    if approval is None:
        return False
    # Only protected configuration may carry this reviewed authority record.
    # Dispatch inputs cannot turn on instance mutation.
    require(isinstance(approval, dict) and set(approval) == {"account_id", "instance_id", "source_revision", "plan_sha256", "approval_ref"}
            and all(approval[key] == config[key] for key in ("account_id", "instance_id", "source_revision"))
            and approval["plan_sha256"] == config["artifacts"]["tfplan"]["sha256"]
            and isinstance(approval["approval_ref"], str) and 0 < len(approval["approval_ref"]) < 512,
            "instance_change_approval_mismatch")
    return True


def download(s3, artifact, account):
    response = s3.get_object(Bucket=artifact["bucket"], Key=artifact["key"],
                             VersionId=artifact["version_id"], ExpectedBucketOwner=account)
    require(response.get("VersionId") == artifact["version_id"] and 0 < response["ContentLength"] <= MAX_OBJECT,
            "reviewed_artifact_version_invalid")
    try:
        data = response["Body"].read(MAX_OBJECT + 1)
    finally:
        response["Body"].close()
    require(len(data) == response["ContentLength"] and hashlib.sha256(data).hexdigest() == artifact["sha256"],
            "reviewed_artifact_digest_mismatch")
    return data


def terraform(directory, *args, allowed=(0,)):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("TF_CLI_ARGS", "TF_VAR_"))}
    environment.update(TF_DATA_DIR=str(directory / "terraform-data"), TF_IN_AUTOMATION="true", TF_WORKSPACE="default")
    result = subprocess.run(["terraform", f"-chdir={INFRA}", *args], capture_output=True,
                            timeout=1800, env=environment, check=False)
    # Retain complete provider output privately; never print state or diagnostics.
    if result.returncode not in allowed:
        failure = directory / "terraform-failure.json"
        if not failure.exists():
            write_private(failure, canonical({"phase": args[0], "exit_code": result.returncode,
                "stdout_base64": base64.b64encode(result.stdout).decode(),
                "stderr_base64": base64.b64encode(result.stderr).decode()}))
    require(result.returncode in allowed, "registration_terraform_operation_failed")
    return result


def restore_plan_lock(directory, infra=INFRA):
    """Use the provider selections embedded in the approved binary plan."""
    with zipfile.ZipFile(directory / "tfplan") as archive:
        entries = [entry for entry in archive.infolist() if entry.filename == ".terraform.lock.hcl"]
        require(len(entries) == 1 and 0 < entries[0].file_size <= 65536, "reviewed_provider_lock_required")
        data = archive.read(entries[0])
    target = infra / ".terraform.lock.hcl"
    if target.exists() or target.is_symlink():
        require(not target.is_symlink() and target.read_bytes() == data, "reviewed_provider_lock_mismatch")
    else:
        write_private(target, data)


def prepare(config, directory, session):
    require(json.loads(terraform(directory, "version", "-json").stdout)["terraform_version"] == "1.14.6",
            "reviewed_terraform_version_required")
    for name, artifact in config["artifacts"].items():
        filename = {"tfplan": "tfplan", "variables": "variables.tfvars.json", "approved_root": "root.pem"}[name]
        write_private(directory / filename, download(session.client("s3"), artifact, config["account_id"]))
    variables = json.loads((directory / "variables.tfvars.json").read_bytes())
    require(variables.get("environment") == config["environment"] and variables.get("aws_region") == config["region"],
            "registration_variables_target_mismatch")
    write_private(directory / "backend.json", canonical(config["backend"]))
    restore_plan_lock(directory)
    terraform(directory, "init", f"-backend-config={directory / 'backend.json'}", "-input=false", "-reconfigure", "-lockfile=readonly")
    plan = json.loads(terraform(directory, "show", "-json", str(directory / "tfplan")).stdout)
    require(plan.get("terraform_version") == "1.14.6", "reviewed_plan_version_mismatch")
    require(all(plan.get("variables", {}).get(key) == {"value": value} for key, value in variables.items()),
            "reviewed_plan_variables_mismatch")
    write_private(directory / "plan.json", canonical(plan))


def guard(config, directory):
    require(hashlib.sha256((directory / "tfplan").read_bytes()).hexdigest() == config["artifacts"]["tfplan"]["sha256"],
            "saved_plan_changed")
    plan = json.loads((directory / "plan.json").read_bytes())
    check_reviewed_instance_plan(plan, config["targets"], config["instance_id"], instance_gate=instance_gate(config))


def apply_plan(config, directory):
    guard(config, directory)
    terraform(directory, "apply", "-input=false", "-no-color", str(directory / "tfplan"))
    # Replan from the exact reviewed variables, never refresh-only or -target.
    result = terraform(directory, "plan", f"-var-file={directory / 'variables.tfvars.json'}", "-input=false",
                       "-no-color", "-detailed-exitcode", f"-out={directory / 'post.tfplan'}", allowed=(0, 2))
    post = json.loads(terraform(directory, "show", "-json", str(directory / "post.tfplan")).stdout)
    require(result.returncode == 0 and all(row.get("change", {}).get("actions") == ["no-op"]
            for row in post.get("resource_changes", []) if row.get("mode") == "managed"),
            "registration_post_apply_drift")
    write_private(directory / "apply.json", canonical({"reviewed_plan_sha256": config["targets"]["reviewed_plan_sha256"],
                  "applied_plan_matches_review": True, "post_apply_zero_change_plan": True}))



def instance_state(client, instance_id):
    result = client.describe_instances(InstanceIds=[instance_id])
    rows = [row for reservation in result["Reservations"] for row in reservation["Instances"]]
    require(len(rows) == 1 and rows[0]["InstanceId"] == instance_id, "registration_instance_identity_mismatch")
    return rows[0]["State"]["Name"]


def restore_running(client, instance_id):
    state = instance_state(client, instance_id)
    if state == "stopping":
        client.get_waiter("instance_stopped").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
        state = instance_state(client, instance_id)
    require(state in {"running", "pending", "stopped"}, "registration_instance_not_recoverable")
    if state == "stopped":
        client.start_instances(InstanceIds=[instance_id])
    if state != "running":
        client.get_waiter("instance_running").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    require(instance_state(client, instance_id) == "running", "registration_instance_restore_failed")


def apply(config, directory, session):
    # The approved operation starts from running. A failed attribute update must
    # restore that state without changing metadata, replacing or deleting anything.
    guard(config, directory)
    client = session.client("ec2")
    require(instance_state(client, config["instance_id"]) == "running", "reviewed_instance_not_running")
    try:
        apply_plan(config, directory)
    except Exception:
        restore_running(client, config["instance_id"])
        raise


def retain_failure(config, directory, session, env):
    path = directory / "terraform-failure.json"
    if not path.exists():
        return
    repository = int(env["GITHUB_REPOSITORY_ID"])
    run, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    require(repository > 0 and run > 0 and attempt > 0, "registration_diagnostic_identity_invalid")
    key = f"gitlab-registration-diagnostics/repos/{repository}/runs/{run}/attempts/{attempt}/terraform-failure.json"
    result = session.client("s3").put_object(Bucket=f"adp-{config['environment']}-deployment-evidence-{config['account_id']}",
        Key=key, Body=path.read_bytes(), ContentType="application/json", ServerSideEncryption="AES256",
        IfNoneMatch="*", ExpectedBucketOwner=config["account_id"])
    require(result.get("VersionId") not in (None, "null"), "registration_diagnostic_version_missing")


def registrar_module():
    spec = importlib.util.spec_from_file_location("protected_registrar", Path(__file__).with_name("register-runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_secret_policies(secrets, config):
    for arn, expected in config["secret_policy_sha256"].items():
        response = secrets.get_resource_policy(SecretId=arn)
        require(response.get("ARN") == arn and digest(json.loads(response["ResourcePolicy"])) == expected,
                "registration_secret_policy_mismatch")


async def stage_group(registrar, admin, store, item):
    lifecycle = registrar.RunnerLifecycle(admin, store, item["tenant_id"], item["group_id"], item["tag_list"], item["access_level"])
    record = store.read_record()
    result = await lifecycle.reconcile(item["operation_id"]) if record else await lifecycle.begin(item["operation_id"])
    require(result["phase"] == "staged", "paused_registration_required")
    require(await lifecycle._candidates(item["operation_id"]) == [result["runner_id"]],
            "registration_runner_policy_mismatch")
    pending = store.pending(item["operation_id"])
    require(pending and pending["runner_id"] == result["runner_id"] and pending["tenant_id"] == item["tenant_id"],
            "registration_pending_readback_failed")
    return {**item, "runner_id": result["runner_id"]}


async def token_verifier(client, token, runner_id, operation_id):
    response = await client.post("runners/verify", json={"token": token,
                                "system_id": "s_" + hashlib.sha256(operation_id.encode()).hexdigest()[:12]})
    if response.status_code == 200:
        require(response.json().get("id") == runner_id, "probe_runner_identity_mismatch")
        return "accepted"
    require(response.status_code == 403, "probe_token_provider_failure")
    return "rejected"


async def verify_project_bindings(admin, config, *, configure=False):
    registrar = registrar_module()
    product = config["product"]
    project = await registrar.verify_product_scope(admin, product["tenant_id"], product["group_id"],
                                                   product["product_id"], product["product_group_id"], product["project_id"])
    await registrar.verify_tenant_group(admin, config["qualification"]["tenant_id"], config["qualification"]["group_id"])
    require(type(project.get("shared_runners_enabled")) is bool, "project_runner_setting_unverified")
    if configure and project["shared_runners_enabled"]:
        # The protected registrar changes only the verified product's runner
        # isolation. CI activation and human membership remain separate steps.
        await admin.request("PUT", f"projects/{product['project_id']}", json={"shared_runners_enabled": False})
        project = await registrar.verify_product_scope(admin, product["tenant_id"], product["group_id"],
                                                       product["product_id"], product["product_group_id"], product["project_id"])
    if configure:
        require(project.get("shared_runners_enabled") is False, "shared_project_runners_enabled")


async def preflight(config, directory, session, exporter):
    """Check live inputs before the reviewed stateful-instance stop/start."""
    sys.path.insert(0, str(ROOT.parents[1] / "gateway"))
    from src.gitlab.admin_client import GitlabAdmin

    secrets = session.client("secretsmanager")
    verify_secret_policies(secrets, config)
    observe_ca_root(exporter.client("acm"), exporter.client("secretsmanager"), config["targets"],
                    root_file=directory / "root.pem", **config["ca"])
    ssm = session.client("ssm")
    for name, expected in (("runner-url", config["ca"]["runner_url"]), ("url", config["instance_url"])):
        actual = ssm.get_parameter(Name=f"/adp/{config['environment']}/gitlab/{name}")["Parameter"]["Value"]
        require(actual == expected, "registration_discovery_mismatch")
    token = secrets.get_secret_value(SecretId=config["admin_secret_arn"])["SecretString"]
    require(isinstance(token, str) and token.startswith("glpat-"), "admin_credential_not_verified")
    async with GitlabAdmin(config["ca"]["runner_url"], token, ca_file=directory / "root.pem") as admin:
        require(admin.owner_id == config["admin_user_id"], "registration_admin_identity_mismatch")
        await verify_project_bindings(admin, config)


async def collect(config, directory, session, exporter):
    import httpx
    sys.path.insert(0, str(ROOT.parents[1] / "gateway"))
    from src.gitlab.admin_client import GitlabAdmin

    # All read-only prerequisites pass before the first runner mutation.
    applied = private_receipt(directory / "apply.json")
    require(applied == expected_observations(config["targets"])["instance-no-unreviewed-change"], "verified_apply_required")
    secrets = session.client("secretsmanager")
    verify_secret_policies(secrets, config)
    ca = observe_ca_root(exporter.client("acm"), exporter.client("secretsmanager"), config["targets"],
                         root_file=directory / "root.pem", **config["ca"])
    url = config["ca"]["runner_url"]
    discovery = session.client("ssm").get_parameter(Name=f"/adp/{config['environment']}/gitlab/runner-url")["Parameter"]["Value"]
    require(discovery == url, "runner_url_discovery_mismatch")
    public_url = session.client("ssm").get_parameter(Name=f"/adp/{config['environment']}/gitlab/url")["Parameter"]["Value"]
    require(public_url == config["instance_url"], "instance_url_discovery_mismatch")
    admin_token = secrets.get_secret_value(SecretId=config["admin_secret_arn"])["SecretString"]
    require(isinstance(admin_token, str) and admin_token.startswith("glpat-"), "admin_credential_not_verified")
    registrar = registrar_module()
    observed = {"ca-root-verified": ca, "instance-no-unreviewed-change": applied}
    async with GitlabAdmin(url, admin_token, ca_file=directory / "root.pem") as admin:
        require(admin.owner_id == config["admin_user_id"], "registration_admin_identity_mismatch")
        await verify_project_bindings(admin, config, configure=True)
        staged = {}
        for name in ("product", "qualification"):
            item = config[name]
            store = registrar.AwsRunnerStore(secrets, session.client("ssm"), config["environment"], item["tenant_id"],
                                             item["group_id"], config["instance_url"], url, item["tag_list"], item["access_level"])
            staged[name] = await stage_group(registrar, admin, store, item)
        observed.update(await observe_registration_groups(admin, staged, config["targets"]))
        observed["group-token-scope"] = expected_observations(config["targets"])["group-token-scope"]
        fixture = config["qualification"]
        probe = registrar.RunnerLifecycle(admin, None, fixture["tenant_id"], fixture["group_id"], fixture["tag_list"], fixture["access_level"])
        async with httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v4/", timeout=15,
                                    verify=ssl.create_default_context(cafile=directory / "root.pem"),
                                    follow_redirects=False, trust_env=False) as client:
            async def verify_token(token, runner_id, operation_id):
                return await token_verifier(client, token, runner_id, operation_id)
            revocation = await probe.probe_revocation(config["probe_operation_id"], verify_token)
        observed["runner-revocation"] = {key: revocation[key] for key in expected_observations(config["targets"])["runner-revocation"]}
    details = {"runner_ids": {name: value["runner_id"] for name, value in staged.items()},
               "operation_ids": {name: value["operation_id"] for name, value in staged.items()},
               "probe_runner_id": revocation["runner_id"], "probe_operation_id": config["probe_operation_id"],
               "ca_version": config["ca"]["ca_version"], "policy_sha256": config["secret_policy_sha256"]}
    return observed, details


def publish_readbacks(s3, config, env, observations, details):
    targets, targets_sha = target_binding({"ADP_REGISTRATION_TARGETS_JSON": json.dumps(config["targets"])})
    require(observations == expected_observations(targets), "six_live_checks_required")
    binding = {"schema_version": 1, "source_revision": config["source_revision"],
               "workflow_run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
               "account_id": config["account_id"], "region": config["region"],
               "correlation": config["correlation"], "targets_sha256": targets_sha}
    bucket = f"adp-{config['environment']}-deployment-evidence-{config['account_id']}"
    owner = {"ExpectedBucketOwner": config["account_id"]}
    require(s3.get_bucket_versioning(Bucket=bucket, **owner).get("Status") == "Enabled", "versioned_evidence_required")
    block = s3.get_public_access_block(Bucket=bucket, **owner)["PublicAccessBlockConfiguration"]
    require(all(block.get(key) is True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")),
            "private_evidence_required")
    checks = {}
    for name in sorted(LIVE_CHECKS):
        key = f"gitlab-registration/repos/{int(env['GITHUB_REPOSITORY_ID'])}/runs/{binding['workflow_run_id']}/attempts/{binding['run_attempt']}/{name}.json"
        body = canonical({**binding, "check": name, "observed": observations[name], "details": details})
        response = s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                                 ServerSideEncryption="AES256", IfNoneMatch="*", **owner)
        version = response.get("VersionId")
        require(isinstance(version, str) and version and version != "null", "versioned_evidence_required")
        saved = s3.get_object(Bucket=bucket, Key=key, VersionId=version, **owner)
        try:
            require(saved.get("VersionId") == version and saved["Body"].read(len(body) + 1) == body, "evidence_readback_failed")
        finally:
            saved["Body"].close()
        checks[name] = {"verdict": "pass", "observed": observations[name],
                        "evidence_ref": f"s3://{bucket}/{key}?versionId={quote(version, safe='')}"}
    receipt = {**binding, "checks": checks}
    verify(receipt, {**env, "ADP_REGISTRATION_TARGETS_JSON": json.dumps(targets)}, config["account_id"], config["source_revision"])
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "guard", "apply", "collect"))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        import boto3
        config = validate_config(json.loads(os.environ["ADP_REGISTRATION_CONFIG_JSON"]), os.environ)
        directory = Path(os.environ["RUNNER_TEMP"]) / "gitlab-registration"
        session = boto3.Session(region_name=config["region"])
        identity(session, config["registrar_role_arn"])
        if args.phase == "prepare":
            directory.mkdir(mode=0o700)
            prepare(config, directory, session)
        elif args.phase == "guard":
            guard(config, directory)
            credentials = session.client("sts").assume_role(RoleArn=config["exporter_role_arn"],
                            RoleSessionName=f"gitlab-ca-{int(os.environ['GITHUB_RUN_ID'])}", DurationSeconds=900)["Credentials"]
            exporter = boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
                                     aws_session_token=credentials["SessionToken"], region_name=config["region"])
            identity(exporter, config["exporter_role_arn"])
            asyncio.run(preflight(config, directory, session, exporter))
        elif args.phase == "apply":
            apply(config, directory, session)
        else:
            credentials = session.client("sts").assume_role(RoleArn=config["exporter_role_arn"],
                            RoleSessionName=f"gitlab-ca-{int(os.environ['GITHUB_RUN_ID'])}", DurationSeconds=900)["Credentials"]
            exporter = boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
                                     aws_session_token=credentials["SessionToken"], region_name=config["region"])
            identity(exporter, config["exporter_role_arn"])
            observations, details = asyncio.run(collect(config, directory, session, exporter))
            receipt = publish_readbacks(session.client("s3"), config, os.environ, observations, details)
            write_private(directory / "receipt.json", canonical(receipt))
            with open(os.environ["GITHUB_ENV"], "a") as output:
                output.write(f"ADP_REGISTRATION_RECEIPT={directory / 'receipt.json'}\n")
                output.write(f"ADP_REGISTRATION_TARGETS_JSON={json.dumps(config['targets'])}\n")
    except Exception as error:
        if "config" in locals() and "directory" in locals() and "session" in locals():
            try:
                retain_failure(config, directory, session, os.environ)
            except Exception:
                print("registration_private_failure_retention_failed", file=sys.stderr)
        # Only our fixed error vocabulary may be logged. Never provider messages,
        # subprocess output, arguments, tokens, PEM, policy or configuration data.
        code = str(error) if type(error) is ValueError and re.fullmatch(r"[a-z_]{3,96}", str(error)) else "registration_phase_failed"
        print(code, file=sys.stderr)
        return 1
    print(f"Registration {args.phase} verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
