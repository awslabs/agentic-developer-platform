#!/usr/bin/env python3
"""Activate a paused group runner and prove real product CI in one protected job."""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote

from registration_workflow import canonical, digest, download, identity, registrar_module, restore_plan_lock, write_private
from runner_delivery import LIVE_CHECKS, verify
from manager_identity import verify_current_manager

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = ROOT / "runners"
sys.path.insert(0, str(RUNNERS / "scripts"))
from activate_manager import fresh_registration, recover_terminating_manager, rollout, stamp


def require(value, code):
    if not value:
        raise ValueError(code)


def validate(config, env):
    keys = {"schema_version", "account_id", "region", "environment", "source_revision", "correlation",
            "registrar_role_arn", "exporter_role_arn", "backend", "artifacts", "plan_sha256", "release_lock_sha256", "product",
            "admin_secret_arn", "admin_user_id", "ca_secret_arn", "ca_version", "ca_sha256", "instance_url",
            "runner_url", "developer_user_id", "cluster", "vpc_id", "asg_arn", "asg_name", "launch_template_id",
            "ami_id", "smoke"}
    require(isinstance(config, dict) and set(config) == keys and config["schema_version"] == 1, "runner_config_invalid")
    for k, e in (("account_id", "ACCOUNT_ID"), ("region", "AWS_REGION"), ("environment", "ENVIRONMENT"),
                 ("source_revision", "ADP_SOURCE_REVISION"), ("correlation", "ADP_CORRELATION"), ("registrar_role_arn", "ADP_DEPLOY_ROLE_ARN")):
        require(config[k] == env[e], "runner_config_binding_mismatch")
    account, region, environment = config["account_id"], config["region"], config["environment"]
    require(re.fullmatch(r"[0-9]{12}", account) and environment == "dev"
            and re.fullmatch(r"[0-9a-f]{40}", config["source_revision"])
            and config["registrar_role_arn"].startswith(f"arn:aws:iam::{account}:role/"), "runner_target_invalid")
    require(config["exporter_role_arn"] != config["registrar_role_arn"]
            and re.fullmatch(fr"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_-]+", config["exporter_role_arn"]), "runner_exporter_identity_invalid")
    require(config["backend"] == {"bucket": f"adp-terraform-state-{account}", "key": "dev/modules/gitlab-runners/terraform.tfstate",
            "region": region, "encrypt": True, "use_lockfile": True}, "original_runner_backend_required")
    p = config["product"]
    require(set(p) == {"tenant_id", "group_id", "product_id", "product_group_id", "project_id", "tag_list", "access_level", "operation_id", "token_secret_arn"}
            and all(type(p[k]) is int and p[k] > 0 for k in ("group_id", "product_group_id", "project_id"))
            and p["tag_list"] == [f"adp-group-{p['group_id']}"] and p["access_level"] == "not_protected"
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", p["operation_id"]), "runner_product_invalid")
    require(set(config["artifacts"]) == {"tfplan", "release_lock"}, "runner_artifacts_required")
    for a in config["artifacts"].values():
        require(set(a) == {"bucket", "key", "version_id", "sha256"}
                and a["bucket"] == f"adp-dev-deployment-evidence-{account}"
                and re.fullmatch(r"gitlab-runners/inputs/[A-Za-z0-9/_.-]+", a["key"])
                and ".." not in a["key"].split("/") and a["version_id"] not in ("", "null")
                and re.fullmatch(r"[0-9a-f]{64}", a["sha256"]), "runner_artifact_invalid")
    require(config["artifacts"]["release_lock"]["sha256"] == config["release_lock_sha256"]
            and all(re.fullmatch(r"[0-9a-f]{64}", config[k]) for k in ("plan_sha256", "ca_sha256"))
            and config["asg_name"] == f"adp-dev-gitlab-job-{p['group_id']}"
            and config["asg_arn"].startswith(f"arn:aws:autoscaling:{region}:{account}:autoScalingGroup:")
            and config["asg_arn"].endswith(":autoScalingGroupName/" + config["asg_name"])
            and re.fullmatch(r"lt-[0-9a-f]{8,17}", config["launch_template_id"])
            and re.fullmatch(r"ami-[0-9a-f]{8,17}", config["ami_id"])
            and re.fullmatch(r"vpc-[0-9a-f]{8,17}", config["vpc_id"]), "runner_infrastructure_binding_invalid")
    require(set(config["smoke"]) == {"branch", "commit", "job", "artifact"}
            and re.fullmatch(r"[0-9a-f]{40}", config["smoke"]["commit"])
            and re.fullmatch(r"[A-Za-z0-9/_-]+", config["smoke"]["branch"])
            and config["smoke"]["job"] == "adp_sso_smoke" and config["smoke"]["artifact"] == "adp-sso-smoke.json",
            "runner_smoke_binding_invalid")
    for key in ("admin_secret_arn", "ca_secret_arn"):
        require(config[key].startswith(f"arn:aws:secretsmanager:{region}:{account}:secret:adp/dev/"), "runner_secret_binding_invalid")
    require(p["token_secret_arn"].startswith(f"arn:aws:secretsmanager:{region}:{account}:secret:adp/dev/gitlab-runner/{p['group_id']}-"),
            "runner_token_binding_invalid")
    require(type(config["developer_user_id"]) is int and config["developer_user_id"] > 0
            and config["developer_user_id"] != config["admin_user_id"], "runner_developer_invalid")
    for name, expected in (("LIFECYCLE_SOURCE", config["source_revision"]), ("LIFECYCLE_OPERATION", p["operation_id"]),
                           ("LIFECYCLE_GROUP", str(p["group_id"])), ("LIFECYCLE_PLAN", config["plan_sha256"])):
        require(not env.get(name) or env[name] == expected, "runner_lifecycle_binding_mismatch")
    return config


def command(args, *, timeout=900):
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    require(result.returncode == 0, "runner_command_failed")
    return result.stdout


def terraform(directory, *args, allowed=(0,)):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TF_CLI_ARGS", "TF_VAR_"))}
    env.update(TF_DATA_DIR=str(directory / "terraform-data"), TF_IN_AUTOMATION="true", TF_WORKSPACE="default")
    result = subprocess.run(["terraform", f"-chdir={RUNNERS}", *args], capture_output=True, timeout=1800, env=env)
    if result.returncode not in allowed:
        failure = directory / "terraform-failure.json"
        if not failure.exists():
            write_private(failure, canonical({"phase": args[0], "exit_code": result.returncode,
                "stdout_base64": base64.b64encode(result.stdout).decode(),
                "stderr_base64": base64.b64encode(result.stderr).decode()}))
    require(result.returncode in allowed, "runner_terraform_failed")
    return result


def retain_failure(config, directory, session, env):
    """Keep provider output private, encrypted, and immutable before cleanup."""
    path = directory / "terraform-failure.json"
    if not path.exists():
        return
    repository, run, attempt = (int(env[name]) for name in
        ("GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
    require(min(repository, run, attempt) > 0, "runner_diagnostic_identity_invalid")
    key = f"gitlab-runner-diagnostics/repos/{repository}/runs/{run}/attempts/{attempt}/terraform-failure.json"
    bucket = f"adp-{config['environment']}-deployment-evidence-{config['account_id']}"
    owner = {"ExpectedBucketOwner": config["account_id"]}
    body = path.read_bytes()
    s3 = session.client("s3")
    result = s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
        ServerSideEncryption="AES256", IfNoneMatch="*", **owner)
    version = result.get("VersionId")
    require(version not in (None, "", "null"), "runner_diagnostic_version_missing")
    actual = s3.get_object(Bucket=bucket, Key=key, VersionId=version, **owner)
    try:
        require(actual["Body"].read(len(body) + 1) == body and actual["VersionId"] == version
                and actual["ServerSideEncryption"] == "AES256", "runner_diagnostic_readback_failed")
    finally:
        actual["Body"].close()


def reviewed_image_change(config, resource):
    """Only replace the bound job image/boot script and advance its empty ASG."""
    if resource.get("change", {}).get("actions") != ["update"]:
        return False
    before, after = resource["change"].get("before"), resource["change"].get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    group = str(config["product"]["group_id"])
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    def contains_unknown(value):
        # Terraform retains nested block/list structure even when every leaf
        # is known (for example [{"security_groups": [False]}]).
        if isinstance(value, dict):
            return any(contains_unknown(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_unknown(item) for item in value)
        return value is True

    unknown = {key for key, value in resource["change"].get("after_unknown", {}).items() if contains_unknown(value)}
    if resource.get("type") == "aws_launch_template":
        return (resource.get("address") == f'aws_launch_template.job["{group}"]'
                and before.get("id") == after.get("id") == config["launch_template_id"]
                and after.get("image_id") == config["ami_id"]
                and not before.get("iam_instance_profile") and not after.get("iam_instance_profile")
                and isinstance(after.get("user_data"), str) and bool(after["user_data"])
                and unknown <= {"latest_version", "default_version"}
                and changed <= {"image_id", "user_data", "latest_version", "default_version"})
    if resource.get("type") == "aws_autoscaling_group":
        templates = after.get("launch_template", [])
        return (resource.get("address") == f'aws_autoscaling_group.job["{group}"]'
                and before.get("arn") == after.get("arn") == config["asg_arn"]
                and before.get("name") == after.get("name") == config["asg_name"]
                and before.get("desired_capacity") == after.get("desired_capacity") == 0
                and len(templates) == 1 and templates[0].get("id") == config["launch_template_id"]
                and unknown <= {"launch_template"}
                and changed <= {"launch_template"})
    return False


def reviewed_connector_change(config, plan, resource):
    """Rebind existing SSH/cache rules to the discovered cluster nodes."""
    group = str(config["product"]["group_id"])
    connector = resource.get("address") == f'aws_vpc_security_group_ingress_rule.manager_connector["{group}"]'
    cache = resource.get("address") == "aws_vpc_security_group_ingress_rule.cache_manager"
    if (resource.get("type") != "aws_vpc_security_group_ingress_rule"
            or not (connector or cache)
            or resource.get("change", {}).get("actions") != ["update"]):
        return False
    before, after = resource["change"].get("before"), resource["change"].get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    values = {item["address"]: item.get("values", {})
              for item in plan.get("planned_values", {}).get("root_module", {}).get("resources", [])}
    # Terraform stores data read during refresh in prior_state; deferred reads
    # appear in planned_values. The approved plan binds either representation.
    discovered = {item["address"]: item.get("values", {}) for item in
                  plan.get("prior_state", {}).get("values", {}).get("root_module", {}).get("resources", [])
                  if item.get("mode") == "data"}
    nodes = values.get("data.aws_security_group.eks_nodes", discovered.get("data.aws_security_group.eks_nodes", {}))
    template = values.get(f'aws_launch_template.job["{group}"]', {})
    interfaces = template.get("network_interfaces", [])
    destination = values.get("aws_security_group.cache_endpoint", {}).get("id") if cache else (
        interfaces[0]["security_groups"][0] if len(interfaces) == 1
        and len(interfaces[0].get("security_groups", [])) == 1 else None)
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    return (changed == {"referenced_security_group_id"}
            and not any(resource["change"].get("after_unknown", {}).values())
            and re.fullmatch(r"sgr-[0-9a-f]{8,17}", after.get("id", "")) is not None
            and re.fullmatch(r"sg-[0-9a-f]{8,17}", nodes.get("id", "")) is not None
            and nodes.get("vpc_id") == config["vpc_id"]
            and after.get("referenced_security_group_id") == nodes["id"]
            and template.get("id") == config["launch_template_id"]
            and isinstance(destination, str)
            and after.get("security_group_id") == destination
            and after.get("ip_protocol") == "tcp"
            and after.get("from_port") == after.get("to_port") == (443 if cache else 22)
            and not any(after.get(key) for key in ("cidr_ipv4", "cidr_ipv6", "prefix_list_id")))


def activation_plan(config, plan):
    require(plan["terraform_version"] == "1.14.6", "runner_terraform_version_mismatch")
    changes = [x for x in plan["resource_changes"] if x.get("mode") == "managed" and x["change"]["actions"] != ["no-op"]]
    require(all((x["type"] in {"helm_release", "kubernetes_config_map_v1"} and x["change"]["actions"] in (["create"], ["update"]))
                or reviewed_image_change(config, x)
                or reviewed_connector_change(config, plan, x)
                for x in changes), "runner_activation_requires_reconciled_foundation")
    variables = {k: v["value"] for k, v in plan["variables"].items()}
    require(variables["manager_activation_enabled"] is True and variables["runtime_iam_policies_enabled"] is True
            and set(variables["groups"]) == {str(config["product"]["group_id"])}, "runner_activation_plan_invalid")
    return variables


def prepare(config, directory, session):
    for name, artifact in config["artifacts"].items():
        write_private(directory / name, download(session.client("s3"), artifact, config["account_id"]))
    lock_dir = RUNNERS / ".terraform"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    write_private(lock_dir / "runners-release-lock.json", (directory / "release_lock").read_bytes())
    restore_plan_lock(directory, RUNNERS)
    write_private(directory / "backend.json", canonical(config["backend"]))
    terraform(directory, "init", "-input=false", "-reconfigure", "-lockfile=readonly", f"-backend-config={directory / 'backend.json'}")
    plan = json.loads(terraform(directory, "show", "-json", str(directory / "tfplan")).stdout)
    spec = importlib.util.spec_from_file_location("runner_plan_guard", ROOT / "scripts/runner-stack-plan-guard.py")
    guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
    guard.check_saved_plan(plan, "apply", config["account_id"], config["region"], config["backend"]["bucket"],
                           config["plan_sha256"], config["release_lock_sha256"])
    variables = activation_plan(config, plan)
    write_private(directory / "variables.json", canonical(variables))
    import boto3
    credentials = session.client("sts").assume_role(RoleArn=config["exporter_role_arn"],
        RoleSessionName=f"gitlab-runner-ca-{int(os.environ['GITHUB_RUN_ID'])}", DurationSeconds=900)["Credentials"]
    exporter = boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"], region_name=config["region"])
    identity(exporter, config["exporter_role_arn"])
    ca = exporter.client("secretsmanager").get_secret_value(SecretId=config["ca_secret_arn"], VersionId=config["ca_version"])
    require(ca["VersionId"] == config["ca_version"] and hashlib.sha256(ca["SecretString"].encode()).hexdigest() == config["ca_sha256"],
            "runner_ca_mismatch")
    write_private(directory / "ca.pem", ca["SecretString"].encode())
    return plan


class InlineIntegration:
    """Qualify in this protected job; dispatching another apply would deadlock."""
    def __init__(self, config, directory, session, admin, lock):
        self.config, self.directory, self.session, self.admin, self.lock = config, directory, session, admin, lock
        self.registration = None
        self.started = datetime.now(timezone.utc)
        path = directory / "kubeconfig"
        command(["aws", "eks", "update-kubeconfig", "--name", config["cluster"], "--region", config["region"], "--kubeconfig", str(path)])
        self.kubeconfig = path

    def kube(self, *args):
        result = command(["kubectl", "--kubeconfig", str(self.kubeconfig), *args], timeout=300)
        return json.loads(result) if args[-2:] == ("-o", "json") else None

    def aws(self, service, operation, **values):
        return getattr(self.session.client(service), operation.replace("-", "_"))(**values)

    async def drained(self, runner_id):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            jobs = []
            for status in ("running", "pending", "preparing", "waiting_for_resource", "canceling"):
                jobs += await self.admin.pages(f"runners/{runner_id}/jobs", status=status)
            group = self.session.client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=[self.config["asg_name"]])["AutoScalingGroups"]
            require(len(group) == 1 and group[0]["AutoScalingGroupARN"] == self.config["asg_arn"], "runner_asg_mismatch")
            instances = self.session.client("ec2").describe_instances(Filters=[
                {"Name": "tag:Component", "Values": ["gitlab-job"]},
                {"Name": "tag:Environment", "Values": [self.config["environment"]]},
                {"Name": "tag:GroupId", "Values": [str(self.config["product"]["group_id"])]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}])["Reservations"]
            if not jobs and not group[0]["Instances"] and group[0]["DesiredCapacity"] == 0 and not any(r["Instances"] for r in instances):
                return True
            await asyncio.sleep(5)
        return False

    async def switch(self, runner_id):
        details = await self.admin.request("GET", f"runners/{runner_id}")
        require(details.get("paused") is True and details.get("runner_type") == "group_type", "paused_runner_required")
        require(await self.drained(runner_id), "runner_not_drained_before_switch")
        before = terraform(self.directory, "plan", "-input=false", "-no-color", "-detailed-exitcode",
                           f"-var-file={self.directory / 'variables.json'}", allowed=(0, 2))
        if before.returncode == 2:
            # Terraform rejects a stale saved plan. Never regenerate an apply plan.
            self.apply_manager()
        self.registration = rollout(self.kube, str(self.config["product"]["group_id"]), self.config["product"]["operation_id"],
                                    self.config["source_revision"], self.config["account_id"], runner_id, call=self.aws)
        post = terraform(self.directory, "plan", "-input=false", "-no-color", "-detailed-exitcode",
                         f"-var-file={self.directory / 'variables.json'}", allowed=(0, 2))
        require(post.returncode == 0, "runner_post_apply_drift")
        # Do not reopen admissions until the reviewed private image is live.
        observe_infrastructure(self.config, self.session, self.lock)

    def apply_manager(self):
        group = str(self.config["product"]["group_id"])
        namespace = f"gitlab-runner-{group}"
        pods = self.kube("get", "pods", "-n", namespace, "-l", "app=gitlab-runner", "-o", "json")["items"]
        if not pods:
            return terraform(self.directory, "apply", "-input=false", "-no-color", str(self.directory / "tfplan"))
        # Helm waits for Recreate. Let it publish the reviewed configuration
        # before finishing the old pod's shutdown, so the replacement cannot
        # start with the broken configuration. The caller already proved idle.
        with ThreadPoolExecutor(max_workers=1) as executor:
            apply = executor.submit(terraform, self.directory, "apply", "-input=false", "-no-color", str(self.directory / "tfplan"))
            while not apply.done():
                deployment = self.kube("get", "deployment", "gitlab-runner", "-n", namespace, "-o", "json")
                containers = {c["name"]: c for c in deployment["spec"]["template"]["spec"]["containers"]}
                home = {v["name"]: v.get("value") for v in containers["gitlab-runner"].get("env", [])}.get("HOME")
                guard = self.kube("get", "configmap", "manager-prestart", "-n", namespace, "-o", "json")
                digest = hashlib.sha256(guard["data"]["prepare_manager.py"].encode()).hexdigest()
                if (home == "/home/gitlab-runner" and containers["manager-guard"].get("readinessProbe")
                        and digest == self.lock["manager_guard_sha256"]):
                    recover_terminating_manager(self.kube, group, self.config["account_id"])
                time.sleep(1)
            return apply.result()

    async def manager_uses(self, runner_id):
        group = self.config["product"]["group_id"]
        namespace = f"gitlab-runner-{group}"
        # Node consolidation can replace a pod after the initial rollout check.
        self.kube("rollout", "status", "deployment/gitlab-runner", "-n", namespace, "--timeout=240s")
        d = self.kube("get", "deployment", "gitlab-runner", "-n", namespace, "-o", "json")
        require(d["status"].get("observedGeneration", 0) >= d["metadata"]["generation"]
                and all(d["status"].get(k) == 1 for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")), "runner_manager_not_ready")
        parameter = self.session.client("ssm").get_parameter(Name=f"/adp/dev/gitlab-runners/{group}/registration-receipt")["Parameter"]
        annotations = d["spec"]["template"]["metadata"].get("annotations", {})
        require(annotations.get("adp.aws-e/runner-operation") == self.config["product"]["operation_id"]
                and annotations.get("adp.aws-e/source-revision") == self.config["source_revision"], "runner_manager_source_mismatch")
        started = stamp(annotations["adp.aws-e/rollout-started-at"])
        pods = self.kube("get", "pods", "-n", namespace, "-l", "app=gitlab-runner", "-o", "json")["items"]
        require(len(pods) == 1, "runner_single_manager_pod_required")
        pod = pods[0]
        require(not pod["metadata"].get("deletionTimestamp")
                and pod["spec"]["serviceAccountName"] == "gitlab-runner"
                and pod["status"]["phase"] == "Running"
                and all(c["ready"] for c in pod["status"]["containerStatuses"]), "runner_manager_pod_not_ready")
        owner = pod["metadata"]["ownerReferences"]
        require(len(owner) == 1 and owner[0]["kind"] == "ReplicaSet" and owner[0].get("controller"), "runner_manager_owner_mismatch")
        replica = self.kube("get", "replicaset", owner[0]["name"], "-n", namespace, "-o", "json")
        require(replica["metadata"]["uid"] == owner[0]["uid"]
                and any(o.get("controller") and o["kind"] == "Deployment" and o["uid"] == d["metadata"]["uid"]
                        for o in replica["metadata"]["ownerReferences"]), "runner_manager_owner_mismatch")
        started = max(started, stamp(pod["metadata"]["creationTimestamp"]))
        reg = fresh_registration(parameter, runner_id, started, datetime.now(timezone.utc))
        heartbeat = self.session.client("ssm").get_parameter(Name=f"/adp/dev/gitlab-runners/{group}/manager-alive")["Parameter"]
        require(heartbeat["Value"] == "active" and 0 <= (datetime.now(timezone.utc) - heartbeat["LastModifiedDate"]).total_seconds() <= 120,
                "runner_heartbeat_stale")
        # This endpoint returns the entire list and does not support pagination.
        managers = await self.admin.request("GET", f"runners/{runner_id}/managers")
        self.manager_history = verify_current_manager(managers, reg, self.lock["chart"]["runner_version"],
                                                      parameter["LastModifiedDate"], datetime.now(timezone.utc))
        current = self.kube("get", "pods", "-n", namespace, "-l", "app=gitlab-runner", "-o", "json")["items"]
        require(len(current) == 1 and current[0]["metadata"]["uid"] == pod["metadata"]["uid"]
                and not current[0]["metadata"].get("deletionTimestamp"), "runner_manager_changed_during_verification")
        self.registration = reg
        return True

    async def refresh_finished_manager(self, runner_id):
        """Keep a finished registration while applying reviewed source maintenance."""
        detail = await self.admin.request("GET", f"runners/{runner_id}")
        require(type(detail.get("paused")) is bool and detail.get("runner_type") == "group_type",
                "runner_finished_identity_invalid")
        namespace = f"gitlab-runner-{self.config['product']['group_id']}"
        deployment = self.kube("get", "deployment", "gitlab-runner", "-n", namespace, "-o", "json")
        annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
        require(annotations.get("adp.aws-e/runner-operation") == self.config["product"]["operation_id"],
                "runner_manager_operation_mismatch")
        changed = annotations.get("adp.aws-e/source-revision") != self.config["source_revision"]
        # A newly reviewed source may recover the same owned operation after
        # an interrupted update. A same-source operator pause stays closed.
        require(not detail["paused"] or changed, "runner_finished_but_paused")
        if changed:
            if not detail["paused"]:
                await self.admin.request("PUT", f"runners/{runner_id}", json={"paused": True})
            # switch independently verifies the pause and drain before mutation.
            # A failed rollout deliberately leaves admission paused for recovery.
            await self.switch(runner_id)
        await self.manager_uses(runner_id)
        if changed:
            await self.admin.request("PUT", f"runners/{runner_id}", json={"paused": False})


def observe_infrastructure(config, session, lock):
    ec2 = session.client("ec2")
    cluster = session.client("eks").describe_cluster(name=config["cluster"])["cluster"]
    require(cluster["status"] == "ACTIVE" and cluster["resourcesVpcConfig"]["vpcId"] == config["vpc_id"], "runner_cluster_vpc_mismatch")
    groups = session.client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=[config["asg_name"]])["AutoScalingGroups"]
    require(len(groups) == 1 and groups[0]["AutoScalingGroupARN"] == config["asg_arn"], "runner_asg_mismatch")
    group = groups[0];lt = group["LaunchTemplate"]
    require(lt["LaunchTemplateId"] == config["launch_template_id"], "runner_launch_template_mismatch")
    template = ec2.describe_launch_template_versions(LaunchTemplateId=lt["LaunchTemplateId"], Versions=[lt["Version"]])["LaunchTemplateVersions"][0]["LaunchTemplateData"]
    require(template["ImageId"] == config["ami_id"] and template["NetworkInterfaces"] and all(x.get("AssociatePublicIpAddress") is False for x in template["NetworkInterfaces"])
            and template["MetadataOptions"]["HttpTokens"] == "required", "runner_private_host_mismatch")
    subnets = ec2.describe_subnets(SubnetIds=group["VPCZoneIdentifier"].split(","))["Subnets"]
    require(subnets and all(x["VpcId"] == config["vpc_id"] and x["MapPublicIpOnLaunch"] is False for x in subnets), "runner_private_subnet_mismatch")
    images = ec2.describe_images(ImageIds=[config["ami_id"]], Owners=[config["account_id"]])["Images"]
    require(len(images) == 1 and images[0]["State"] == "available" and images[0]["Public"] is False
            and lock["host_ami"][config["region"]]["image_id"] == config["ami_id"], "runner_qualified_ami_mismatch")
    return {"same-vpc-private-asg": {"vpc_id": config["vpc_id"], "asg_arn": config["asg_arn"], "public_ip": False},
            "qualified-image-lock": {"release_lock_sha256": config["release_lock_sha256"], "ami_id": config["ami_id"]}}


async def pipeline(config, admin, runner_id, directory):
    p, smoke = config["product"]["project_id"], config["smoke"]
    branch = await admin.request("GET", f"projects/{p}/repository/branches/{quote(smoke['branch'], safe='')}")
    require(branch["commit"]["id"] == smoke["commit"], "runner_smoke_commit_mismatch")
    existing = await admin.pages(f"projects/{p}/pipelines", ref=smoke["branch"], sha=smoke["commit"], source="api")
    matches = []
    for item in existing:
        values = await admin.request("GET", f"projects/{p}/pipelines/{item['id']}/variables")
        if any(x.get("key") == "ADP_VERIFICATION_ID" and x.get("value") == config["correlation"] for x in values):
            matches.append(item)
    require(len(matches) <= 1, "runner_pipeline_ambiguous")
    if matches:
        run = matches[0]
    else:
        write_private(directory / "pipeline-intent.json", canonical({"correlation": config["correlation"], "commit": smoke["commit"]}))
        run = await admin.request("POST", f"projects/{p}/pipeline", json={"ref": smoke["branch"], "variables": [
            {"key": "ADP_VERIFICATION_ID", "value": config["correlation"], "variable_type": "env_var"}]})
    require(type(run.get("id")) is int and run.get("sha") == smoke["commit"], "runner_pipeline_binding_mismatch")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        current = await admin.request("GET", f"projects/{p}/pipelines/{run['id']}")
        if current["status"] == "success": break
        require(current["status"] not in {"failed", "canceled", "skipped"}, "runner_smoke_pipeline_failed")
        await asyncio.sleep(5)
    require(current["status"] == "success", "runner_smoke_pipeline_timeout")
    jobs = await admin.pages(f"projects/{p}/pipelines/{run['id']}/jobs")
    require(len(jobs) == 1 and jobs[0].get("name") == smoke["job"] and jobs[0]["status"] == "success"
            and jobs[0].get("runner", {}).get("id") == runner_id and jobs[0]["commit"]["id"] == smoke["commit"], "runner_smoke_job_mismatch")
    artifact = await admin.request("GET", f"projects/{p}/jobs/{jobs[0]['id']}/artifacts/{smoke['artifact']}")
    require(artifact.get("CI_COMMIT_SHA") == smoke["commit"] and str(artifact.get("CI_PIPELINE_ID")) == str(run["id"])
            and str(artifact.get("CI_JOB_ID")) == str(jobs[0]["id"]) and artifact.get("ADP_VERIFICATION_ID") == config["correlation"], "runner_smoke_artifact_mismatch")
    return {"project_id": p, "commit": smoke["commit"], "status": "success", "artifact_verified": True}, {"pipeline_id": run["id"], "job_id": jobs[0]["id"]}


async def activate(config, directory, session, plan):
    sys.path.insert(0, str(ROOT.parents[1] / "gateway"))
    from src.gitlab.admin_client import GitlabAdmin
    lock = json.loads((directory / "release_lock").read_bytes())
    observations = {}
    token = session.client("secretsmanager").get_secret_value(SecretId=config["admin_secret_arn"])["SecretString"]
    require(isinstance(token, str) and token.startswith("glpat-"), "runner_admin_credential_invalid")
    registrar = registrar_module();p = config["product"]
    async with GitlabAdmin(config["runner_url"], token, ca_file=directory / "ca.pem") as admin:
        require(admin.owner_id == config["admin_user_id"], "runner_admin_identity_mismatch")
        await registrar.verify_product_scope(admin, p["tenant_id"], p["group_id"], p["product_id"], p["product_group_id"], p["project_id"])
        store = registrar.AwsRunnerStore(session.client("secretsmanager"), session.client("ssm"), config["environment"],
            p["tenant_id"], p["group_id"], config["instance_url"], config["runner_url"], p["tag_list"], p["access_level"])
        lifecycle = registrar.RunnerLifecycle(admin, store, p["tenant_id"], p["group_id"], p["tag_list"], p["access_level"])
        integration = InlineIntegration(config, directory, session, admin, lock)
        record = store.read_record()
        if record and record.get("phase") == "finished":
            require(record.get("operation_id") == p["operation_id"] and record.get("group_id") == p["group_id"], "runner_finished_operation_mismatch")
            runner_id = record["new_runner_id"]
            await integration.refresh_finished_manager(runner_id)
        else:
            result = await lifecycle.qualify(p["operation_id"], integration)
            runner_id = result["runner_id"]
        # Read the bound replacement image after the paused/drained update.
        # A stale or mismatched launch template must fail before product CI.
        observations.update(observe_infrastructure(config, session, lock))
        observations["eks-manager-ready"] = {"group_id": p["group_id"], "ready_replicas": 1, "live_managers": 1}
        await registrar.enable_project_ci(admin, p["tenant_id"], p["group_id"], p["product_id"], p["product_group_id"], p["project_id"], config["developer_user_id"])
        observations["project-ci-enabled"] = {"project_id": p["project_id"], "enabled": True, "shared_runners": False,
            "developer_user_id": config["developer_user_id"], "developer_access": 30}
        assigned = await admin.pages(f"projects/{p['project_id']}/runners")
        require(any(x.get("id") == runner_id and x.get("runner_type") == "group_type" for x in assigned), "runner_project_assignment_missing")
        observations["adp-project-runner-assignment"] = {"project_id": p["project_id"], "group_id": p["group_id"], "assigned": True}
        observations["legal-pipeline-ran"], details = await pipeline(config, admin, runner_id, directory)
        require(await integration.drained(runner_id), "runner_idle_cleanup_timeout")
        observations["idle-scale-to-zero"] = {"asg_arn": config["asg_arn"], "desired_capacity": 0, "instances": 0, "active_jobs": 0}
        details.update(runner_id=runner_id, manager_system_id=integration.registration["system_id"],
                       **integration.manager_history)
    return observations, details


def publish(config, env, s3, observations, details):
    require(set(observations) == LIVE_CHECKS, "seven_runner_checks_required")
    binding = {"schema_version": 1, "source_revision": config["source_revision"], "workflow_run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]), "account_id": config["account_id"], "region": config["region"],
        "correlation": config["correlation"], "config_sha256": digest(config)}
    bucket = f"adp-dev-deployment-evidence-{config['account_id']}";owner = {"ExpectedBucketOwner": config["account_id"]}
    require(s3.get_bucket_versioning(Bucket=bucket, **owner).get("Status") == "Enabled", "runner_versioned_evidence_required")
    checks = {}
    for name in sorted(LIVE_CHECKS):
        key = f"gitlab-runners/repos/{int(env['GITHUB_REPOSITORY_ID'])}/runs/{binding['workflow_run_id']}/attempts/{binding['run_attempt']}/{name}.json"
        body = canonical({**binding, "check": name, "observed": observations[name], "details": details})
        result = s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", ServerSideEncryption="AES256", IfNoneMatch="*", **owner)
        version = result.get("VersionId");require(version and version != "null", "runner_versioned_evidence_required")
        actual = s3.get_object(Bucket=bucket, Key=key, VersionId=version, **owner)
        try:require(actual["Body"].read(len(body)+1) == body and actual["VersionId"] == version, "runner_evidence_readback_failed")
        finally:actual["Body"].close()
        checks[name] = {"verdict": "pass", "observed": observations[name], "evidence_ref": f"s3://{bucket}/{key}?versionId={quote(version, safe='')}"}
    receipt = {**binding, "checks": checks};verify(receipt, env, config["account_id"], config["source_revision"])
    return receipt


def main():
    parser = argparse.ArgumentParser();parser.parse_args()
    logging.disable(logging.CRITICAL);os.umask(0o077)
    config = directory = session = None
    try:
        import boto3
        config = validate(json.loads(os.environ["ADP_RUNNER_CONFIG_JSON"]), os.environ)
        session = boto3.Session(region_name=config["region"]);identity(session, config["registrar_role_arn"])
        directory = Path(os.environ["RUNNER_TEMP"]) / "gitlab-runner-activation";directory.mkdir(mode=0o700)
        plan = prepare(config, directory, session)
        observations, details = asyncio.run(activate(config, directory, session, plan))
        receipt = publish(config, os.environ, session.client("s3"), observations, details)
        write_private(directory / "receipt.json", canonical(receipt))
        with open(os.environ["GITHUB_ENV"], "a") as target:target.write(f"ADP_RUNNER_RECEIPT={directory / 'receipt.json'}\n")
    except Exception as error:
        if config is not None and directory is not None and session is not None:
            try:
                retain_failure(config, directory, session, os.environ)
            except Exception:
                print("runner_diagnostic_retention_failed", file=sys.stderr)
        code = str(error) if type(error) is ValueError and re.fullmatch(r"[a-z_]{3,96}", str(error)) else "runner_activation_failed"
        print(code, file=sys.stderr);return 1
    print("Seven live runner checks verified");return 0


if __name__ == "__main__":
    raise SystemExit(main())
