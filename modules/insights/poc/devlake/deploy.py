import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import admission
import finalize
import intent_ledger
import mode
import publish


ROOT = Path(__file__).resolve().parents[4]
INFRA = Path(__file__).with_name("infra")
REQUIRED = {
    "enabled", "connectors_enabled", "environment", "aws_account_id", "aws_region",
    "deployment_label", "deployment_role", "migration_label", "migration_role",
    "cell_id", "existing_db_endpoint", "existing_db_resource_id", "database_name",
    "migration_username", "writer_db_username", "reader_db_username", "ca_path", "ca_configmap_name", "worker_role_boundary_arn",
    "api_receipt", "worker_receipt",
}


def selected_config(path):
    if path is None:
        mode.render(False, False)
        return None
    config_path = Path(path).resolve()
    if config_path.is_relative_to(ROOT) or config_path.stat().st_mode & 0o077:
        raise ValueError("optional deployment configuration must be private and outside the repository")
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
        raise ValueError("optional deployment selections must be explicit booleans")
    mode.render(config.get("connectors_enabled", False), config["enabled"])
    if set(config) - REQUIRED:
        raise ValueError("unknown optional collection configuration field")
    if not config["enabled"]:
        return None
    if (set(config) != REQUIRED or
            config["environment"] not in ("dev", "prod") or
            not re.fullmatch(r"[0-9]{12}", config["aws_account_id"]) or
            not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", config["aws_region"]) or
            not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", config["deployment_role"]) or
            not all(isinstance(config[role], str) and re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", config[role])
                    for role in ("migration_username", "writer_db_username", "reader_db_username")) or
            len({config["migration_username"], config["writer_db_username"], config["reader_db_username"]}) != 3 or
            not config["deployment_label"]):
        raise ValueError("collection selection or approved deployment target is invalid")
    return config


def checked_install(config):
    identity = json.loads(publish.broker(config["deployment_label"], "aws", "sts",
                                         "get-caller-identity", "--output", "json"))
    account = config["aws_account_id"]
    role = config["deployment_role"]
    if (identity.get("Account") != account or
            not identity.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/")):
        raise ValueError("selected deployment role differs from approved account or role")
    target = dict(host=config["existing_db_endpoint"], database=config["database_name"],
                  cell=config["cell_id"], resource_id=config["existing_db_resource_id"],
                  username=config["migration_username"], ca_path=config["ca_path"],
                  region=config["aws_region"], account=account, role=config["migration_role"],
                  label=config["migration_label"])
    images = admission.installation_preflight(admission.load_receipt(config["api_receipt"]),
                                              admission.load_receipt(config["worker_receipt"]), target)
    return {
        "enabled": True,
        "connectors_enabled": True,
        "environment": config["environment"],
        "aws_account_id": account,
        "aws_region": config["aws_region"],
        "worker_image": images["worker"],
        "writer_db_username": config["writer_db_username"],
        "schema_manifest_sha256": hashlib.sha256(admission.SCHEMA.read_bytes()).hexdigest(),
        "cell_id": config["cell_id"],
        "existing_db_endpoint": config["existing_db_endpoint"],
        "existing_db_resource_id": config["existing_db_resource_id"],
        "ca_configmap_name": config["ca_configmap_name"],
        "worker_role_boundary_arn": config["worker_role_boundary_arn"],
    }


def terraform_run(config, variables, plan_path, apply=False, expected_plan_sha256=None,
                  infra=INFRA, state_key=None):
    plan = Path(plan_path).resolve()
    if (plan.is_relative_to(ROOT) or plan.parent.stat().st_mode & 0o077 or
            (apply and (not plan.is_file() or plan.stat().st_mode & 0o077))):
        raise ValueError("reviewed Terraform plan must be private and outside the repository")
    if apply and (not expected_plan_sha256 or
                  not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) or
                  hashlib.sha256(plan.read_bytes()).hexdigest() != expected_plan_sha256):
        raise ValueError("reviewed Terraform plan digest differs")
    if not apply and plan.exists():
        raise ValueError("plan output must not overwrite an earlier plan")
    if apply:
        intent_ledger.require_cleanup_authority()
    environment = {**os.environ, **{
        "TF_VAR_" + key: value if isinstance(value, str) else json.dumps(value)
        for key, value in variables.items()}}
    label = config["deployment_label"]

    def run(*arguments):
        try:
            return subprocess.run(["adp-cred", "assume", "--service", "aws", "--label", label,
                            "--exec", "terraform", "-chdir=" + str(infra), *arguments],
                           env=environment, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            raise ValueError("selected optional Terraform operation failed; inspect private operator evidence") from None

    backend = ("-backend-config=bucket=adp-terraform-state-" + config["aws_account_id"],
               "-backend-config=key=" + (state_key or config["environment"] + "/modules/insights/devlake/cells/" + config["cell_id"] + "/terraform.tfstate"),
               "-backend-config=region=" + config["aws_region"], "-backend-config=encrypt=true",
               "-backend-config=dynamodb_table=adp-terraform-locks")
    run("init", "-input=false", "-reconfigure", *backend)
    if apply:
        try:
            result = run("show", "-json", str(plan))
            plan_variables = json.loads(result.stdout)["variables"]
            if any(plan_variables[key]["value"] != value for key, value in variables.items()):
                raise ValueError("reviewed Terraform plan targets different configuration")
        except (KeyError, TypeError, json.JSONDecodeError, OSError, subprocess.CalledProcessError):
            raise ValueError("reviewed Terraform plan cannot be verified") from None
        run("apply", "-input=false", str(plan))
    else:
        plan.touch(mode=0o600, exist_ok=False)
        try:
            run("plan", "-input=false", "-out=" + str(plan))
        except ValueError:
            plan.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser(description="Optional, private DevLake collection installation")
    parser.add_argument("--config", help="private operator configuration outside the repository")
    parser.add_argument("--plan-file", help="private reviewed Terraform plan outside the repository")
    parser.add_argument("--apply-reviewed-plan", action="store_true")
    parser.add_argument("--expected-plan-sha256", help="operator-reviewed SHA-256 of the private plan")
    args = parser.parse_args()
    try:
        config = selected_config(args.config)
        if config is None:
            print("optional collection disabled; no infrastructure operation")
            return
        if not args.plan_file:
            raise ValueError("selected collection requires an operator plan path")
        variables = checked_install(config)
        intent = intent_ledger.collection(config, variables)
        intent_ledger.register(finalize, "collection:" + config["cell_id"], intent)
        terraform_run(config, variables, args.plan_file, apply=args.apply_reviewed_plan,
                      expected_plan_sha256=args.expected_plan_sha256, state_key=intent["state_key"])
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
