"""Install the adapter foundations and enable their existing release identity.

Both states are planned before either is applied. The automation plan may only
enable the adapter flag on an already provisioned tools publisher; all other
authority and resource ownership must remain unchanged.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .bootstrap import Bootstrap, BootstrapError, digest, encoded, read_json, require, source_digest, write_private

REPO = Path(__file__).resolve().parents[3]
AUTOMATION = "platform/automation-infra"
SOURCE_PATHS = (
    AUTOMATION,
    "modules/domain-apps/superplane/infra/automation/build-dispatch",
    "modules/domain-apps/superplane/infra/automation/skypilot-deployment",
    "modules/gateway/infra/deployment-evidence",
    "modules/agent-factory/infra/modules/runner-runtime-policy",
    "modules/domain-apps/cyber/image-builder/windows/register-cape-vm.sh",
)
EXISTING_POLICIES = {
    "aws_iam_role_policy.tools_deployment[0]",
    "aws_iam_role_policy.tools_deployment_ceiling[0]",
    "aws_iam_policy.tools_deployment_resource_ceiling[0]",
}
PUBLICATION_POLICY = "aws_iam_policy.tools_adapters_publication[0]"
PUBLICATION_ATTACHMENT = "aws_iam_role_policy_attachment.tools_adapters_publication[0]"
FOUNDATION_POLICY = "aws_iam_policy.tools_adapters_foundation_check[0]"
FOUNDATION_ATTACHMENT = "aws_iam_role_policy_attachment.tools_adapters_foundation_check[0]"
ADAPTER_POLICIES = {
    PUBLICATION_POLICY: "-tools-adapters-publication",
    FOUNDATION_POLICY: "-tools-adapters-foundation-check",
}
TARGETS = sorted(EXISTING_POLICIES | set(ADAPTER_POLICIES) | {PUBLICATION_ATTACHMENT, FOUNDATION_ATTACHMENT})


def policy(value):
    """IAM treats statement/action/resource/value ordering as insignificant."""
    if isinstance(value, str):
        value = json.loads(value)

    def ordered(item):
        if isinstance(item, list):
            return sorted((ordered(v) for v in item), key=encoded)
        if isinstance(item, dict):
            return {k: ordered(v) for k, v in item.items()}
        return item

    return ordered(value)


def configuration(value):
    require(isinstance(value, dict) and set(value) == {"foundation", "automation"}, "installer_config_fields_invalid")
    foundation = Bootstrap(value["foundation"], "/unused").config
    variables = foundation["variables"]
    automation = value["automation"]
    require(isinstance(automation, dict) and set(automation) == {"backend", "variables"}, "automation_config_invalid")
    backend, inputs = automation["backend"], automation["variables"]
    require(isinstance(backend, dict) and isinstance(inputs, dict), "automation_inputs_invalid")
    require(
        {"bucket", "key", "region", "encrypt"}
        <= backend.keys()
        <= {"bucket", "key", "region", "encrypt", "dynamodb_table", "use_lockfile"},
        "automation_backend_fields_invalid",
    )
    require(
        backend["bucket"] == foundation["backend"]["bucket"]
        and backend["region"] == variables["aws_region"]
        and backend["encrypt"] is True
        and isinstance(backend["key"], str)
        and re.fullmatch(r"[A-Za-z0-9_/-]+[.]tfstate", backend["key"])
        and backend["key"] != foundation["backend"]["key"],
        "automation_backend_target_invalid",
    )
    require(
        backend.get("use_lockfile") is True
        or (
            isinstance(backend.get("dynamodb_table"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", backend["dynamodb_table"])
        ),
        "automation_backend_lock_required",
    )
    require("use_lockfile" not in backend or isinstance(backend["use_lockfile"], bool), "automation_lock_invalid")
    require(
        inputs.get("environment") == variables["environment"]
        and inputs.get("aws_region") == variables["aws_region"]
        and inputs.get("cluster_name") == variables["cluster_name"]
        and inputs.get("enable_tools_deployment") is True
        and isinstance(inputs.get("name_prefix"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,36}", inputs["name_prefix"])
        and isinstance(inputs.get("repository"), str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", inputs["repository"]),
        "automation_variables_target_invalid",
    )
    # The existing publication policy deliberately names this archive bucket.
    # Custom buckets/KMS need separately reviewed publisher grants first.
    require(
        variables["state_bucket"] == f"adp-terraform-state-{variables['aws_account_id']}"
        and not variables["source_kms_key_arn"],
        "publication_archive_grant_mismatch",
    )
    require(
        "enable_tools_adapters_publication" not in inputs
        or isinstance(inputs["enable_tools_adapters_publication"], bool),
        "publication_flag_invalid",
    )
    return {
        "foundation": value["foundation"],
        "automation": {
            "backend": {**backend, "allowed_account_ids": [variables["aws_account_id"]]},
            "variables": {**inputs, "enable_tools_adapters_publication": True},
        },
    }


def sources(root=REPO):
    names = (
        subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--", *SOURCE_PATHS]).decode().split("\0")
    )
    result = {}
    for name in filter(None, names):
        path = root / name
        require(not path.is_symlink() and path.is_file(), "automation_source_invalid")
        result[name] = path.read_bytes()
    require(f"{AUTOMATION}/tools-adapters-deployment.tf" in result, "automation_source_missing")
    # Terraform loads even untracked overrides. Only the copied, tracked source
    # is executed; refuse overrides rather than silently omitting them.
    for directory in SOURCE_PATHS[:-1]:
        for path in (root / directory).glob("*.tf*"):
            require(str(path.relative_to(root)) in result, "automation_untracked_override")
    return result


def check_publication_plan(enabled, disabled, config):
    expected = config["automation"]["variables"]
    for plan, flag in ((enabled, True), (disabled, False)):
        # Terraform marks -target plans incomplete because other state resources
        # were intentionally excluded. Deferred or errored work is still refused.
        require(plan.get("errored") is not True and not plan.get("deferred_changes"), "automation_plan_incomplete")
        values = {k: v.get("value") for k, v in plan.get("variables", {}).items()}
        require(
            all(values.get(k) == v for k, v in {**expected, "enable_tools_adapters_publication": flag}.items()),
            "automation_plan_inputs_changed",
        )
    off = {item["address"]: item for item in disabled.get("resource_changes", [])}
    require(
        set(TARGETS) <= {item["address"] for item in enabled.get("resource_changes", [])},
        "publisher_plan_resources_missing",
    )
    changes = []
    for item in enabled.get("resource_changes", []):
        address, change = item["address"], item["change"]
        actions = change.get("actions")
        require(not item.get("previous_address") and not change.get("importing"), "automation_state_migration_refused")
        if actions in (["no-op"], ["read"]):
            continue
        require(address in TARGETS and actions in (["create"], ["update"]), "unrelated_automation_change")
        before, after = change.get("before"), change.get("after")
        require(isinstance(after, dict), "automation_after_unknown")
        if address in EXISTING_POLICIES:
            require(actions == ["update"] and isinstance(before, dict), "existing_policy_required")
            require(isinstance(after.get("policy"), str), "existing_policy_content_unknown")
            previous = off.get(address, {}).get("change", {}).get("after")
            require(
                isinstance(previous, dict) and policy(before["policy"]) == policy(previous["policy"]),
                "existing_authority_drift",
            )
            require(
                all(before.get(k) == after.get(k) for k in before if k not in {"policy", "default_version_id"}),
                "existing_policy_binding_changed",
            )
        elif address in ADAPTER_POLICIES:
            require(
                after.get("name") == expected["name_prefix"] + ADAPTER_POLICIES[address],
                "publication_policy_name_changed",
            )
        else:
            require(
                after.get("role") == expected["name_prefix"] + "-tools-trusted-deployment", "publication_role_changed"
            )
        changes.append({"address": address, "actions": actions})
    return changes


class Publisher(Bootstrap):
    def __init__(self, config, work_dir, *, terraform="terraform", env=None, repo=REPO):
        super().__init__(config["foundation"], work_dir, terraform=terraform, env=env)
        self.installer_config = config
        self.automation = config["automation"]
        self.repo = repo

    def tf(self, *args):
        return self.run([self.terraform, f"-chdir={self.work / 'source' / AUTOMATION}", *args])

    def target(self):
        super().target()
        variables = self.config["variables"]
        role = self.automation["variables"]["name_prefix"] + "-tools-trusted-deployment"
        arn = f"arn:aws:iam::{variables['aws_account_id']}:role/{role}"
        require(
            self.aws("iam", "get-role", "--role-name", role).get("Role", {}).get("Arn") == arn,
            "existing_publisher_required",
        )
        access = self.aws(
            "eks", "describe-access-entry", "--cluster-name", variables["cluster_name"], "--principal-arn", arn
        ).get("accessEntry", {})
        require(
            access.get("principalArn") == arn
            and access.get("type") == "STANDARD"
            and f"adp:tools-release-{variables['environment']}" in access.get("kubernetesGroups", []),
            "existing_publisher_access_required",
        )

    def plan(self):
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not any(self.work.iterdir()), "new_plan_directory_required")
        self.work.chmod(0o700)
        write_private(self.work / "commands.log", b"")
        write_private(self.work / "terraform.rc", b"disable_checkpoint = true\n")
        self.target()
        files = sources(self.repo)
        for name, content in files.items():
            path = self.work / "source" / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(path, content)
        write_private(self.work / "backend.json", encoded(self.automation["backend"]))
        write_private(self.work / "variables.tfvars.json", encoded(self.automation["variables"]))
        self.initialize()
        # Verify this backend already owns the publisher. A wrong or empty key
        # must not turn a scoped permission update into a second installation.
        state = json.loads(self.tf("state", "pull"))
        account = self.config["variables"]["aws_account_id"]
        role = self.automation["variables"]["name_prefix"] + "-tools-trusted-deployment"
        arn = f"arn:aws:iam::{account}:role/{role}"
        owners = [
            i.get("attributes", {}).get("arn")
            for r in state.get("resources", [])
            if r.get("type") == "aws_iam_role" and r.get("name") == "tools_deployment" and not r.get("module")
            for i in r.get("instances", [])
            if i.get("index_key") == 0
        ]
        require(owners == [arn], "automation_backend_does_not_own_publisher")
        plans = {}
        for label, flag in (("disabled", False), ("enabled", True)):
            # JSON preserves the boolean type in Terraform's plan input record;
            # -var=... records raw CLI strings even for a bool declaration.
            var_file = self.work / (label + ".tfvars.json")
            write_private(
                var_file, encoded({**self.automation["variables"], "enable_tools_adapters_publication": flag})
            )
            self.tf(
                "plan",
                "-input=false",
                "-no-color",
                "-lock-timeout=120s",
                f"-var-file={var_file}",
                *(f"-target={target}" for target in TARGETS),
                f"-out={self.work / (label + '.tfplan')}",
            )
            raw = self.tf("show", "-json", str(self.work / (label + ".tfplan")))
            write_private(self.work / (label + ".json"), raw)
            plans[label] = json.loads(raw)
        changes = check_publication_plan(plans["enabled"], plans["disabled"], self.installer_config)
        write_private(
            self.work / "manifest.json",
            encoded(
                {
                    "schema_version": 1,
                    "config": self.installer_config,
                    "source_sha256": source_digest(files),
                    "files": sorted(files),
                    "plan_sha256": digest((self.work / "enabled.tfplan").read_bytes()),
                    "disabled_sha256": digest((self.work / "disabled.json").read_bytes()),
                    "changes": changes,
                }
            ),
        )
        return {"phase": "publisher_planned", "changes": changes}

    def verify_saved(self):
        require(self.work.is_dir() and self.work.stat().st_mode & 0o077 == 0, "private_plan_directory_required")
        manifest = read_json(self.work / "manifest.json")
        require(
            manifest.get("schema_version") == 1 and manifest.get("config") == self.installer_config,
            "publisher_config_changed",
        )
        copied = {name: (self.work / "source" / name).read_bytes() for name in manifest["files"]}
        for path in (self.work / "source").rglob("*.tf*"):
            require(str(path.relative_to(self.work / "source")) in copied, "publisher_snapshot_override")
        require(
            manifest["source_sha256"] == source_digest(sources(self.repo)) == source_digest(copied),
            "publisher_source_changed",
        )
        require(
            manifest["plan_sha256"] == digest((self.work / "enabled.tfplan").read_bytes()), "publisher_plan_changed"
        )
        require(
            manifest["disabled_sha256"] == digest((self.work / "disabled.json").read_bytes()),
            "publisher_comparison_changed",
        )
        require(
            read_json(self.work / "backend.json") == self.automation["backend"]
            and read_json(self.work / "variables.tfvars.json") == self.automation["variables"]
            and read_json(self.work / "enabled.tfvars.json") == self.automation["variables"]
            and read_json(self.work / "disabled.tfvars.json")
            == {**self.automation["variables"], "enable_tools_adapters_publication": False},
            "publisher_inputs_changed",
        )
        self.target()
        self.initialize()
        plan = json.loads(self.tf("show", "-json", str(self.work / "enabled.tfplan")))
        check_publication_plan(plan, read_json(self.work / "disabled.json"), self.installer_config)
        return plan

    def apply(self):
        plan = self.verify_saved()
        self.tf("apply", "-input=false", "-no-color", "-lock-timeout=120s", str(self.work / "enabled.tfplan"))
        self.target()
        resources = {r["address"]: r for r in plan["resource_changes"]}
        for address in sorted(EXISTING_POLICIES | set(ADAPTER_POLICIES)):
            expected = resources[address]["change"]["after"]
            if address.startswith("aws_iam_role_policy."):
                actual = self.aws(
                    "iam", "get-role-policy", "--role-name", expected["role"], "--policy-name", expected["name"]
                )["PolicyDocument"]
            else:
                arn = f"arn:aws:iam::{self.config['variables']['aws_account_id']}:policy/{expected['name']}"
                version = self.aws("iam", "get-policy", "--policy-arn", arn)["Policy"]["DefaultVersionId"]
                actual = self.aws("iam", "get-policy-version", "--policy-arn", arn, "--version-id", version)[
                    "PolicyVersion"
                ]["Document"]
            require(policy(actual) == policy(expected["policy"]), "publisher_policy_readback_mismatch")
        role = self.automation["variables"]["name_prefix"] + "-tools-trusted-deployment"
        attached = self.aws("iam", "list-attached-role-policies", "--role-name", role).get("AttachedPolicies", [])
        account = self.config["variables"]["aws_account_id"]
        arns = {}
        for address, suffix in ADAPTER_POLICIES.items():
            policy_name = self.automation["variables"]["name_prefix"] + suffix
            arns[address] = f"arn:aws:iam::{account}:policy/{policy_name}"
            require(any(p.get("PolicyArn") == arns[address] for p in attached), "publisher_attachment_missing")
        return {
            "phase": "publisher_enabled",
            "policy_arn": arns[PUBLICATION_POLICY],
            "foundation_check_policy_arn": arns[FOUNDATION_POLICY],
        }


class Installer:
    def __init__(self, config, work_dir, *, terraform="terraform", env=None):
        self.config = configuration(config)
        self.work = Path(work_dir).absolute()
        require(not self.work.is_symlink(), "work_directory_symlink")
        self.foundation = Bootstrap(self.config["foundation"], self.work / "foundation", terraform=terraform, env=env)
        self.publisher = Publisher(self.config, self.work / "publisher", terraform=terraform, env=env)

    def plan(self):
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not any(self.work.iterdir()), "new_plan_directory_required")
        self.work.chmod(0o700)
        write_private(self.work / "config.json", encoded(self.config))
        # All prerequisite and authority checks finish before foundation apply.
        publisher = self.publisher.plan()
        foundation = self.foundation.plan()
        return {
            "phase": "installer_planned",
            "work_dir": str(self.work),
            "foundation": foundation,
            "publisher": publisher,
        }

    def apply(self):
        require(read_json(self.work / "config.json") == self.config, "installer_config_changed")
        self.publisher.verify_saved()
        foundation = self.foundation.apply()
        publisher = self.publisher.apply()
        result = {
            "phase": "adapter_foundations_installed",
            "work_dir": str(self.work),
            "foundation": foundation,
            "publisher": publisher,
            "runtime_verified": False,
        }
        write_private(self.work / "receipt.json", encoded(result))
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "plan", "apply", "install"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    args = parser.parse_args()
    try:
        config = configuration(read_json(args.config))
        variables = config["foundation"]["variables"]
        require(
            (args.account, args.region, args.environment)
            == tuple(variables[k] for k in ("aws_account_id", "aws_region", "environment")),
            "installer_target_mismatch",
        )
        if args.action == "validate":
            print(json.dumps({"phase": "installer_configuration_validated", "runtime_verified": False}))
            return 0
        require(args.work_dir is not None, "work_directory_required")
        installer = Installer(read_json(args.config), args.work_dir, terraform=args.terraform)
        if args.action == "install":
            installer.plan()
            result = installer.apply()
        else:
            result = getattr(installer, args.action)()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BootstrapError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(
            f"Adapter installer refused: {error if isinstance(error, BootstrapError) else type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
