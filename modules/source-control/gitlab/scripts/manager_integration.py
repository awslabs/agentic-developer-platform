"""Qualify a paused registration through the existing protected apply workflow."""
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

RUNNERS = Path(__file__).resolve().parents[1] / "runners"
sys.path.insert(0, str(RUNNERS / "scripts"))
from activate_manager import fresh_registration, stamp
from deployment_guard import check_identity
from manager_identity import verify_current_manager


class IntegrationError(ValueError):
    pass


def require(condition):
    if not condition:
        raise IntegrationError("paused manager integration not verified")


def private_json(path):
    path = Path(path)
    metadata = path.stat()
    require(not path.is_symlink() and metadata.st_uid == os.geteuid()
            and metadata.st_mode & 0o077 == 0 and metadata.st_size <= 65536)
    return json.loads(path.read_text())


def gh(method, path, payload=None):
    args = ["gh", "api", "--method", method, path]
    if payload is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=False, timeout=45)
    require(result.returncode == 0)
    return json.loads(result.stdout) if result.stdout.strip() else None


class ManagerIntegration:
    def __init__(self, config, sdk, admin, *, group_id, operation_id, region,
                 runner_version, github=gh, clock=time.monotonic, pause=asyncio.sleep):
        require(set(config) == {"account_id", "expected_registrar_role_arn", "region", "source_revision",
                                "group_id", "operation_id", "evidence_path", "adp_correlation",
                                "adp_definition_revision", "adp_contract_digest", "approved_plan_sha256"})
        require(config["group_id"] == group_id and type(group_id) is int and group_id > 0
                and config["operation_id"] == operation_id and config["region"] == region
                and re.fullmatch(r"[0-9a-f]{40}", config["source_revision"]) is not None
                and re.fullmatch(r"[0-9]{12}", config["account_id"]) is not None
                and isinstance(config["adp_correlation"], str) and 0 < len(config["adp_correlation"]) <= 512
                and re.fullmatch(r"[0-9a-f]{40}", config["adp_definition_revision"]) is not None
                and re.fullmatch(r"[0-9a-f]{64}", config["adp_contract_digest"]) is not None
                and re.fullmatch(r"[0-9a-f]{64}", config["approved_plan_sha256"]) is not None)
        check_identity(sdk.client("sts").get_caller_identity(), config["expected_registrar_role_arn"])
        require(config["expected_registrar_role_arn"].split(":")[4] == config["account_id"])
        self.config, self.admin, self.sdk = config, admin, sdk
        self.github, self.clock, self.pause, self.runner_version = github, clock, pause, runner_version
        self.path = Path(config["evidence_path"])
        require(self.path.is_absolute() and not self.path.is_symlink())
        parent = self.path.parent.stat()
        require(parent.st_uid == os.geteuid() and parent.st_mode & 0o077 == 0)
        self.lock = self.path.with_suffix(".lock").open("a")
        os.fchmod(self.lock.fileno(), 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise IntegrationError("manager operation already active") from None

    def close(self):
        self.lock.close()

    def retain(self, receipt, *, initial=False):
        if initial:
            with self.path.open("x") as output:
                os.chmod(self.path, 0o600)
                json.dump(receipt, output, indent=2)
        else:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(receipt, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def receipt(self, runner_id):
        receipt = private_json(self.path)
        require(all(receipt.get(key) == self.config[key] for key in
                    ("source_revision", "operation_id", "group_id", "account_id"))
                and receipt.get("runner_id") == runner_id)
        return receipt

    async def drained(self, runner_id):
        group = str(self.config["group_id"])
        deadline = self.clock() + 240
        while self.clock() < deadline:
            jobs = []
            for status in ("running", "pending", "preparing", "waiting_for_resource", "canceling"):
                rows = await self.admin.pages(f"runners/{runner_id}/jobs", status=status)
                require(isinstance(rows, list))
                jobs.extend(rows)
            name = f"adp-dev-gitlab-job-{group}"
            groups = self.sdk.client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=[name])["AutoScalingGroups"]
            require(len(groups) == 1 and groups[0]["AutoScalingGroupName"] == name)
            tags = {entry["Key"]: entry["Value"] for entry in groups[0]["Tags"]}
            require(all(tags.get(key) == value for key, value in
                        (("Component", "gitlab-job"), ("Environment", "dev"), ("GroupId", group))))
            instances, token, seen = [], None, set()
            while True:
                query = {"Filters": [{"Name": "tag:Component", "Values": ["gitlab-job"]},
                                     {"Name": "tag:Environment", "Values": ["dev"]},
                                     {"Name": "tag:GroupId", "Values": [group]},
                                     {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}]}
                if token:
                    query["NextToken"] = token
                page = self.sdk.client("ec2").describe_instances(**query)
                instances.extend(item for reservation in page["Reservations"] for item in reservation["Instances"])
                token = page.get("NextToken")
                if not token:
                    break
                require(token not in seen)
                seen.add(token)
            if not jobs and not instances and not groups[0]["Instances"] and groups[0]["DesiredCapacity"] == 0:
                return True
            await self.pause(10)
        return False

    async def switch(self, runner_id):
        config = self.config
        require(type(runner_id) is int and runner_id > 0)
        details = await self.admin.request("GET", f"runners/{runner_id}")
        require(details.get("id") == runner_id and details.get("paused") is True
                and details.get("runner_type") == "group_type" and details.get("locked") is True
                and details.get("run_untagged") is False
                and f"adp-group-{config['group_id']}" in details.get("tag_list", []))
        associated = await self.admin.pages(f"groups/{config['group_id']}/runners")
        require(isinstance(associated, list) and any(item.get("id") == runner_id for item in associated))
        title = f"ADP deployment {config['adp_correlation']}"
        if not self.path.exists():
            head = self.github("GET", "repos/aws-e/adp/git/ref/heads/main")["object"]["sha"]
            require(head == config["source_revision"])
            existing = self.github("GET", "repos/aws-e/adp/actions/workflows/gitlab-runners-apply.yml/runs?event=workflow_dispatch&branch=main&per_page=100")
            require(not any(run.get("display_title") == title for run in existing["workflow_runs"]))
            receipt = {key: config[key] for key in ("source_revision", "operation_id", "group_id", "account_id")}
            receipt.update(runner_id=runner_id, status="dispatch_intent", created_at=datetime.now(timezone.utc).isoformat())
            self.retain(receipt, initial=True)
            # Persist intent before submission. An ambiguous error is reconciled,
            # never followed by a blind second dispatch.
            self.github("POST", "repos/aws-e/adp/actions/workflows/gitlab-runners-apply.yml/dispatches", {
                "ref": "main", "inputs": {"source_revision": config["source_revision"],
                    "account_id": config["account_id"], "environment": "dev", "region": config["region"],
                    "activation_operation_id": config["operation_id"], "activation_group_id": str(config["group_id"]),
                    "approved_plan_sha256": config["approved_plan_sha256"], "adp_correlation": config["adp_correlation"],
                    "adp_source_revision": config["source_revision"], "adp_definition_revision": config["adp_definition_revision"],
                    "adp_contract_digest": config["adp_contract_digest"]}})
        receipt = self.receipt(runner_id)
        deadline = self.clock() + 600
        while self.clock() < deadline:
            if receipt.get("workflow_run_id"):
                run = self.github("GET", f"repos/aws-e/adp/actions/runs/{receipt['workflow_run_id']}")
            else:
                response = self.github("GET", "repos/aws-e/adp/actions/workflows/gitlab-runners-apply.yml/runs?event=workflow_dispatch&branch=main&per_page=100")
                matches = [run for run in response["workflow_runs"] if run.get("display_title") == title]
                require(len(matches) <= 1)
                run = matches[0] if matches else None
            if run is not None:
                require(run["head_sha"] == config["source_revision"] and run["head_branch"] == "main"
                        and run["event"] == "workflow_dispatch" and run["display_title"] == title
                        and stamp(run["created_at"]) >= stamp(receipt["created_at"]).replace(microsecond=0)
                        and type(run["id"]) is int)
                receipt.update(workflow_run_id=run["id"], status=run["status"],
                               run_started_at=run.get("run_started_at"))
                self.retain(receipt)
                if run["status"] == "completed":
                    receipt["conclusion"] = run["conclusion"]
                    self.retain(receipt)
                    require(run["conclusion"] == "success")
                    return
            await self.pause(10)
        raise IntegrationError("protected apply pending; reconcile the recorded workflow, do not redispatch")

    async def manager_uses(self, runner_id):
        receipt = self.receipt(runner_id)
        require(receipt.get("conclusion") == "success" and receipt.get("status") == "completed")
        run = self.github("GET", f"repos/aws-e/adp/actions/runs/{receipt['workflow_run_id']}")
        require(run.get("status") == "completed" and run.get("conclusion") == "success"
                and run.get("head_sha") == self.config["source_revision"] and run.get("head_branch") == "main"
                and run.get("event") == "workflow_dispatch"
                and run.get("display_title") == f"ADP deployment {self.config['adp_correlation']}"
                and run.get("run_started_at") == receipt.get("run_started_at"))
        group = self.config["group_id"]
        ssm = self.sdk.client("ssm")
        parameter = ssm.get_parameter(Name=f"/adp/dev/gitlab-runners/{group}/registration-receipt", WithDecryption=False)["Parameter"]
        registration = fresh_registration(parameter, runner_id, stamp(receipt["run_started_at"]), datetime.now(timezone.utc))
        heartbeat = ssm.get_parameter(Name=f"/adp/dev/gitlab-runners/{group}/manager-alive", WithDecryption=False)["Parameter"]
        now = datetime.now(timezone.utc)
        modified = heartbeat["LastModifiedDate"]
        if isinstance(modified, str):
            modified = stamp(modified)
        require(heartbeat["Value"] == "active" and 0 <= (now - modified).total_seconds() <= 120)
        managers = await self.admin.request("GET", f"runners/{runner_id}/managers")
        now = datetime.now(timezone.utc)
        try:
            receipt["manager_history"] = verify_current_manager(managers, registration, self.runner_version,
                                                                parameter["LastModifiedDate"], now)
        except (ValueError, KeyError, TypeError):
            require(False)
        receipt["verified_manager"] = registration
        receipt["manager_verified_at"] = now.isoformat()
        self.retain(receipt)
        return True
