"""Plan/apply the adapter foundations from installer-resolved target inputs.

This is an operator/installer operation, separate from the protected image and
workload publisher. It neither registers provider apps nor changes GitHub setup.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ("deployment-infra", "infra", "foundation-infra")
VARIABLES = {
    "aws_account_id",
    "aws_region",
    "environment",
    "state_bucket",
    "cluster_name",
    "namespace",
    "oidc_provider_arn",
    "runtime_role_name",
    "atlassian_mcp_identity_enabled",
    "private_edge_enabled",
    "observation_runtime",
    "runtime_verifier_role_arn",
    "runtime_verifier_group",
    "source_kms_key_arn",
}


class BootstrapError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise BootstrapError(reason)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_json(path):
    content = Path(path).read_bytes()
    require(len(content) <= 20 * 1024**2, "input_too_large")
    return json.loads(content)


def write_private(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(content)


def configuration(value):
    require(isinstance(value, dict) and set(value) == {"backend_bucket", "variables"}, "config_fields_invalid")
    variables = value["variables"]
    require(
        isinstance(variables, dict)
        and VARIABLES
        - {"source_kms_key_arn", "atlassian_mcp_identity_enabled", "private_edge_enabled", "observation_runtime"}
        <= variables.keys()
        <= VARIABLES,
        "variables_invalid",
    )
    require(
        all(
            isinstance(value, str)
            for name, value in variables.items()
            if name not in {"atlassian_mcp_identity_enabled", "private_edge_enabled", "observation_runtime"}
        ),
        "variables_must_be_strings",
    )
    variables = {
        "source_kms_key_arn": "",
        "atlassian_mcp_identity_enabled": False,
        "private_edge_enabled": False,
        "observation_runtime": None,
        **variables,
    }
    require(type(variables["atlassian_mcp_identity_enabled"]) is bool, "identity_activation_must_be_boolean")
    require(type(variables["private_edge_enabled"]) is bool, "private_edge_activation_must_be_boolean")
    require(
        variables["observation_runtime"] is None or isinstance(variables["observation_runtime"], dict),
        "observation_runtime_invalid",
    )
    if variables["observation_runtime"] is not None:
        # Terraform materializes optional object fields as null. Preserve the
        # same canonical inputs in the saved-plan target check and fresh apply.
        variables["observation_runtime"] = {
            "assertion_key_arn": None,
            "release": None,
            **variables["observation_runtime"],
        }
    account, region, environment = (variables[k] for k in ("aws_account_id", "aws_region", "environment"))
    require(
        re.fullmatch(r"[0-9]{12}", account)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        and environment in {"dev", "prod"},
        "target_invalid",
    )
    for bucket in (value["backend_bucket"], variables["state_bucket"]):
        require(isinstance(bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket), "bucket_invalid")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", variables["cluster_name"]), "cluster_invalid")
    require(
        re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", variables["namespace"])
        and variables["namespace"] not in {"default", "kube-system", "kube-public", "kube-node-lease"},
        "namespace_invalid",
    )
    require(re.fullmatch(r"[A-Za-z0-9_+=,.@-]{1,64}", variables["runtime_role_name"]), "runtime_role_invalid")
    require(
        re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/-]+", variables["runtime_verifier_role_arn"])
        and variables["runtime_verifier_role_arn"] != f"arn:aws:iam::{account}:role/{variables['runtime_role_name']}",
        "verifier_role_invalid",
    )
    require(variables["runtime_verifier_group"] == f"adp:runtime-verifier-{environment}", "verifier_group_invalid")
    require(
        re.fullmatch(
            rf"arn:aws:iam::{account}:oidc-provider/oidc\.eks\.{region}\.amazonaws\.com/id/[A-Za-z0-9]+",
            variables["oidc_provider_arn"],
        ),
        "oidc_provider_invalid",
    )
    require(
        not variables["source_kms_key_arn"]
        or re.fullmatch(rf"arn:aws:kms:{region}:{account}:key/[A-Za-z0-9-]+", variables["source_kms_key_arn"]),
        "source_key_invalid",
    )
    backend = {
        "bucket": value["backend_bucket"],
        "key": f"tools/adapters/{environment}.tfstate",
        "region": region,
        "encrypt": True,
        "use_lockfile": True,
        "allowed_account_ids": [account],
    }
    return {"backend": backend, "variables": variables}


def sources(root=ROOT):
    result = {}
    for module in MODULES:
        require(not list((root / module).glob("*.tf.json")), "terraform_json_override_requires_review")
        files = sorted((root / module).glob("*.tf"))
        require(files, "terraform_source_missing")
        for path in files:
            require(not path.is_symlink(), "terraform_source_symlink")
            result[f"{module}/{path.name}"] = path.read_bytes()
    return result


def source_digest(files):
    return digest(encoded({name: digest(content) for name, content in files.items()}))


def check_plan(plan, config):
    require(
        isinstance(plan, dict) and plan.get("errored") is not True and plan.get("complete") is not False,
        "plan_incomplete",
    )
    values = {name: item.get("value") for name, item in plan.get("variables", {}).items()}
    require(values == config["variables"], "saved_plan_target_mismatch")
    changes = []
    for item in plan.get("resource_changes", []):
        actions = item.get("change", {}).get("actions", [])
        require(actions and set(actions) <= {"no-op", "read", "create", "update"}, "destructive_plan_refused")
        require(
            not item.get("previous_address") and not item.get("change", {}).get("importing"),
            "state_migration_requires_review",
        )
        if actions not in (["no-op"], ["read"]):
            changes.append({"address": item["address"], "actions": actions})
    return changes


class Bootstrap:
    def __init__(self, config, work_dir, *, terraform="terraform", env=None, source_root=ROOT):
        self.config = configuration(config)
        self.private_edge_explicit = "private_edge_enabled" in config["variables"]
        self.work = Path(work_dir).absolute()
        require(not self.work.is_symlink(), "work_directory_symlink")
        self.terraform = terraform
        self.source_root = source_root
        self.env = {
            k: v
            for k, v in (os.environ if env is None else env).items()
            if not k.startswith(("TF_VAR_", "TF_CLI_ARGS", "KUBE_"))
            and k not in {"KUBECONFIG", "TF_WORKSPACE", "TF_DATA_DIR", "TF_CLI_CONFIG_FILE"}
        }
        self.env.update(
            AWS_PAGER="",
            AWS_REGION=self.config["variables"]["aws_region"],
            AWS_DEFAULT_REGION=self.config["variables"]["aws_region"],
            TF_IN_AUTOMATION="true",
            TF_DATA_DIR=str(self.work / "terraform-data"),
            TF_CLI_CONFIG_FILE=str(self.work / "terraform.rc"),
        )

    def run(self, args, *, timeout=1200):
        result = subprocess.run(args, env=self.env, capture_output=True, timeout=timeout, check=False)
        # Keep target configuration/provider errors in the private plan directory.
        with (self.work / "commands.log").open("ab") as log:
            log.write(encoded({"command": args, "exit_code": result.returncode}))
            log.write(result.stdout + result.stderr + b"\n")
        require(result.returncode == 0, "command_failed_see_private_log")
        require(len(result.stdout) <= 20 * 1024**2, "command_response_too_large")
        return result.stdout

    def aws(self, *args):
        return json.loads(
            self.run(["aws", *args, "--region", self.env["AWS_REGION"], "--output", "json"], timeout=60) or b"{}"
        )

    def target(self):
        account = self.config["variables"]["aws_account_id"]
        require(self.aws("sts", "get-caller-identity").get("Account") == account, "account_mismatch")
        for bucket in {self.config["backend"]["bucket"], self.config["variables"]["state_bucket"]}:
            self.aws("s3api", "head-bucket", "--bucket", bucket, "--expected-bucket-owner", account)
            require(
                self.aws("s3api", "get-bucket-versioning", "--bucket", bucket, "--expected-bucket-owner", account).get(
                    "Status"
                )
                == "Enabled",
                "bucket_versioning_required",
            )
            public = self.aws(
                "s3api", "get-public-access-block", "--bucket", bucket, "--expected-bucket-owner", account
            )
            require(
                all(
                    public.get("PublicAccessBlockConfiguration", {}).get(key) is True
                    for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
                ),
                "private_bucket_required",
            )

    def tf(self, *args):
        return self.run([self.terraform, f"-chdir={self.work / 'source/deployment-infra'}", *args])

    def initialize(self):
        version = json.loads(self.run([self.terraform, "version", "-json"]))["terraform_version"]
        require(
            re.fullmatch(r"1\.[0-9]+\.[0-9]+", version) and int(version.split(".")[1]) >= 10,
            "terraform_1_10_or_newer_required",
        )
        self.tf("init", "-input=false", "-no-color", "-reconfigure", f"-backend-config={self.work / 'backend.json'}")
        self.tf("validate", "-no-color")

    def retain_private_edge(self, plan):
        """An omitted opt-in preserves the exact ingress already owned by this state."""
        if self.private_edge_explicit or self.config["variables"]["private_edge_enabled"]:
            return False
        existing = [
            item["change"]["before"]
            for item in plan.get("resource_changes", [])
            if item.get("address") == "module.foundation.kubernetes_ingress_v1.adapter_private[0]"
            and item.get("mode") == "managed"
            and isinstance(item.get("change", {}).get("before"), dict)
        ]
        if not existing:
            return False
        require(len(existing) == 1, "private_edge_state_ambiguous")
        metadata = existing[0].get("metadata", [])
        require(
            len(metadata) == 1
            and metadata[0].get("namespace") == self.config["variables"]["namespace"]
            and metadata[0].get("name") == "tool-adapters-private",
            "private_edge_state_target_mismatch",
        )
        self.config["variables"]["private_edge_enabled"] = True
        return True

    def plan(self):
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not any(self.work.iterdir()), "new_plan_directory_required")
        self.work.chmod(0o700)
        write_private(self.work / "commands.log", b"")
        write_private(self.work / "terraform.rc", b"disable_checkpoint = true\n")
        self.target()  # Identity and bucket ownership precede Terraform initialization.
        files = sources(self.source_root)
        for name, content in files.items():
            path = self.work / "source" / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(path, content)
        write_private(self.work / "backend.json", encoded(self.config["backend"]))
        write_private(self.work / "variables.tfvars.json", encoded(self.config["variables"]))
        self.initialize()

        def prepare():
            self.tf(
                "plan",
                "-input=false",
                "-no-color",
                "-lock-timeout=120s",
                f"-var-file={self.work / 'variables.tfvars.json'}",
                f"-out={self.work / 'foundation.tfplan'}",
            )
            return self.tf("show", "-json", str(self.work / "foundation.tfplan"))

        raw = prepare()
        if self.retain_private_edge(json.loads(raw)):
            # This file was created privately above; rewriting retains its mode.
            (self.work / "variables.tfvars.json").write_bytes(encoded(self.config["variables"]))
            raw = prepare()
        changes = check_plan(json.loads(raw), self.config)
        write_private(self.work / "plan.json", raw)
        manifest = {
            "schema_version": 1,
            "config": self.config,
            "source_sha256": source_digest(files),
            "plan_sha256": digest((self.work / "foundation.tfplan").read_bytes()),
            "changes": changes,
        }
        write_private(self.work / "manifest.json", encoded(manifest))
        return {
            "phase": "planned",
            "work_dir": str(self.work),
            "state_key": self.config["backend"]["key"],
            "changes": changes,
        }

    def apply(self):
        require(self.work.is_dir() and self.work.stat().st_mode & 0o077 == 0, "private_plan_directory_required")
        manifest = read_json(self.work / "manifest.json")
        self.retain_private_edge(read_json(self.work / "plan.json"))
        require(
            manifest.get("schema_version") == 1 and manifest.get("config") == self.config, "plan_configuration_changed"
        )
        require(
            manifest["source_sha256"]
            == source_digest(sources(self.source_root))
            == source_digest(sources(self.work / "source")),
            "plan_source_changed",
        )
        require(digest((self.work / "foundation.tfplan").read_bytes()) == manifest["plan_sha256"], "plan_file_changed")
        require(
            read_json(self.work / "backend.json") == self.config["backend"]
            and read_json(self.work / "variables.tfvars.json") == self.config["variables"],
            "plan_inputs_changed",
        )
        self.target()
        self.initialize()
        check_plan(json.loads(self.tf("show", "-json", str(self.work / "foundation.tfplan"))), self.config)
        self.tf("apply", "-input=false", "-no-color", "-lock-timeout=120s", str(self.work / "foundation.tfplan"))
        outputs = json.loads(self.tf("output", "-json"))
        variables = self.config["variables"]
        account, region, environment = (variables[k] for k in ("aws_account_id", "aws_region", "environment"))
        check_name = f"adp-tools-{environment}-adapters-foundation-check"
        expected = {
            "repository_name": f"adp-tools-{environment}-adapters",
            "build_project_name": f"adp-tools-{environment}-adapters-build",
            "foundation_check_project_name": check_name,
            "foundation_check_role_arn": f"arn:aws:iam::{account}:role/{check_name}",
            "foundation_check_source_prefix": f"codebuild/src/{check_name}/",
            "foundation_check_artifact_prefix": f"codebuild/artifacts/{check_name}/",
            "namespace": variables["namespace"],
            "runtime_role_arn": f"arn:aws:iam::{account}:role/{variables['runtime_role_name']}",
            "runtime_verifier_role_arn": variables["runtime_verifier_role_arn"],
            "runtime_verifier_group": variables["runtime_verifier_group"],
            "verified_cluster_arn": f"arn:aws:eks:{region}:{account}:cluster/{variables['cluster_name']}",
        }
        require(
            all(outputs.get(key, {}).get("value") == value for key, value in expected.items()),
            "foundation_outputs_mismatch",
        )
        write_private(self.work / "outputs.json", encoded(outputs))
        return {
            "phase": "foundation_applied",
            "work_dir": str(self.work),
            "state_key": self.config["backend"]["key"],
            "runtime_verified": False,
        }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--terraform", default="terraform")
    args = parser.parse_args()
    try:
        bootstrap = Bootstrap(read_json(args.config), args.work_dir, terraform=args.terraform)
        print(json.dumps(getattr(bootstrap, args.action)(), sort_keys=True))
    except (BootstrapError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(
            f"Adapter foundation refused: {error if isinstance(error, BootstrapError) else type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
