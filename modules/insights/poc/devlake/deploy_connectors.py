import argparse
import ipaddress
import json
import re
from pathlib import Path

import admission
import deploy
import finalize
import intent_ledger
import mode
import publish


INFRA = deploy.ROOT / "modules/connectors/devlake/infra"
REQUIRED = {"enabled", "environment", "aws_account_id", "aws_region", "deployment_label",
            "deployment_role", "api_receipt", "api_role_boundary_arn", "provider_egress_cidrs"}


def selected_config(path):
    if path is None:
        mode.render(False)
        return None
    config_path = Path(path).resolve()
    if config_path.is_relative_to(deploy.ROOT) or config_path.stat().st_mode & 0o077:
        raise ValueError("connector configuration must be private and outside the repository")
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict):
        raise ValueError("connector selection must be an explicit boolean")
    mode.render(config.get("enabled"))
    if set(config) - REQUIRED:
        raise ValueError("unknown optional connector configuration field")
    if not config["enabled"]:
        return None
    if (set(config) != REQUIRED or config["environment"] not in ("dev", "prod") or
            not intent_ledger.ACCOUNT.fullmatch(config["aws_account_id"]) or
            not intent_ledger.REGION.fullmatch(config["aws_region"]) or
            not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", config["deployment_role"]) or
            not config["deployment_label"]):
        raise ValueError("connector selection or approved target is invalid")
    boundary = config["api_role_boundary_arn"]
    if (not isinstance(boundary, str) or not re.fullmatch(
            rf"arn:aws:iam::{config['aws_account_id']}:policy/[A-Za-z0-9_/-]+", boundary) or
            not isinstance(config["provider_egress_cidrs"], list)):
        raise ValueError("connector boundary or provider egress differs")
    try:
        for cidr in config["provider_egress_cidrs"]:
            if not isinstance(cidr, str):
                raise ValueError
            ipaddress.ip_network(cidr, strict=True)
    except ValueError:
        raise ValueError("connector boundary or provider egress differs") from None
    return config


def checked_install(config):
    identity = json.loads(publish.broker(config["deployment_label"], "aws", "sts",
                                         "get-caller-identity", "--output", "json"))
    account = config["aws_account_id"]
    role = config["deployment_role"]
    if (identity.get("Account") != account or
            not identity.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/")):
        raise ValueError("selected connector deployment role differs from approved account or role")
    image = admission.image_preflight(admission.load_receipt(config["api_receipt"]))["api"]
    role_arn = f"arn:aws:iam::{account}:role/adp-{config['environment']}-devlake-connectors-api"
    variables = {"enabled": True, "environment": config["environment"],
                 "aws_account_id": account, "aws_region": config["aws_region"],
                 "api_image": image,
                 "deployment_role_boundaries": {role_arn: config["api_role_boundary_arn"]},
                 "provider_egress_cidrs": config["provider_egress_cidrs"]}
    return variables


def main():
    parser = argparse.ArgumentParser(description="Optional, private DevLake connector installation")
    parser.add_argument("--config", help="private operator configuration outside the repository")
    parser.add_argument("--plan-file", help="private reviewed Terraform plan outside the repository")
    parser.add_argument("--apply-reviewed-plan", action="store_true")
    parser.add_argument("--expected-plan-sha256", help="operator-reviewed SHA-256 of the private plan")
    args = parser.parse_args()
    try:
        config = selected_config(args.config)
        if config is None:
            print("optional connectors disabled; no infrastructure operation")
            return
        if not args.plan_file:
            raise ValueError("selected connectors require an operator plan path")
        variables = checked_install(config)
        intent = intent_ledger.connectors(config, variables["api_image"])
        intent_ledger.register(finalize, "connectors", intent)
        deploy.terraform_run(config, variables, args.plan_file, apply=args.apply_reviewed_plan,
                             expected_plan_sha256=args.expected_plan_sha256, infra=INFRA,
                             state_key=intent["state_key"])
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
