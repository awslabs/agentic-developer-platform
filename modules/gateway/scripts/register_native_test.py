#!/usr/bin/env python3
"""Prepare an offline native registration plan or apply its exact reviewed hash.

Run with the gateway environment and PYTHONPATH=modules/gateway. Preparation
reads only local explicit evidence files. Apply is a separate operator action
using the active AWS profile and the exact protected registration role.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from src.agentauth.native_test_atlassian_registration import AtlassianQuoteInputs, compile_atlassian_contracts
from src.agentauth.native_test_configuration import NativeTestAllInDeployment
from src.agentauth.native_test_enrollment import FixtureDesignation, NativeTestStore
from src.agentauth.native_test_registration import (
    NativeRegistrationPlan,
    StatelessQuoteInputs,
    compile_stateless_contracts,
    register_initial_plan,
    registration_plan,
    runtime_environment,
)
from src.agentauth.test_run_admission_store import canonical, digest


def prepare(path):
    value = json.loads(path.read_text())
    if set(value) != {"quote_inputs", "evidence_files", "designation", "deployment", "account_id", "region", "registration_role_arn"}:
        raise ValueError("Explicit registration inputs required")
    atlassian = value["quote_inputs"].get("schema_version") == "2.0"
    inputs = (AtlassianQuoteInputs if atlassian else StatelessQuoteInputs).model_validate(value["quote_inputs"])
    evidence = {sha: Path(location).read_bytes() for sha, location in value["evidence_files"].items()}
    compiled = (compile_atlassian_contracts if atlassian else compile_stateless_contracts)(inputs, evidence=evidence)
    if "effects" in value["deployment"] or "effect_coverage_digest" in value["deployment"]:
        raise ValueError("Compiler owns the exact contract registration")
    deployment = NativeTestAllInDeployment.model_validate(
        {**value["deployment"], "effects": compiled["effects"], "effect_coverage_digest": compiled["effect_coverage_digest"]}
    )
    return registration_plan(
        inputs=inputs,
        evidence=evidence,
        designation=FixtureDesignation.model_validate(value["designation"]),
        deployment=deployment,
        account_id=value["account_id"],
        region=value["region"],
        registration_role_arn=value["registration_role_arn"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("prepare")
    plan.add_argument("--inputs", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--runtime-output", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expected-plan-sha256", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.inputs)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as destination:
            destination.write(canonical(result.model_dump()) + "\n")
        fd = os.open(args.runtime_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as destination:
            destination.write(canonical({"data": runtime_environment(result)}) + "\n")
        print(json.dumps({"plan_sha256": digest(result.model_dump()), "registered": False}))
        return
    import boto3
    from botocore.config import Config

    result = NativeRegistrationPlan.model_validate_json(args.plan.read_bytes())
    config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
    session = boto3.Session(region_name=result.region)
    store = NativeTestStore(table_name=result.table, client=session.client("dynamodb", config=config))
    receipt = register_initial_plan(
        result, expected_plan_sha256=args.expected_plan_sha256, store=store, sts_client=session.client("sts", config=config)
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
