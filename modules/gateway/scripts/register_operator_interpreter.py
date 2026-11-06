#!/usr/bin/env python3
"""Prepare an operator Interpreter plan or register its exact reviewed digest.

Run with the gateway environment and PYTHONPATH=modules/gateway. Prepare reads
local quote_inputs, evidence_files, deployment, account_id, region,
registration_role_arn and current_environment (the existing ConfigMap data).
The compiler supplies deployment effects, effect_coverage_digest and
contract_sha256; omit those three fields from deployment inputs.

Apply is a separate explicit operator action under the protected registration
role. It creates no user, designation, consent, approval, capability or run.
An unconfirmed apply requires read-only reconciliation, never an automatic retry.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from src.agentauth.native_operator_registration import NativeOperatorRegistrationPlan, register_initial_plan, registration_plan, runtime_environment
from src.agentauth.native_test_configuration import NativeOperatorDeployment
from src.agentauth.native_test_enrollment import NativeTestStore
from src.agentauth.native_test_interpreter_registration import InterpreterQuoteInputs, compile_interpreter_contracts
from src.agentauth.test_run_admission_store import canonical, digest


def prepare(path):
    value = json.loads(path.read_text())
    if set(value) != {"quote_inputs", "evidence_files", "deployment", "account_id", "region", "registration_role_arn", "current_environment"}:
        raise ValueError("Explicit operator registration inputs required")
    inputs = InterpreterQuoteInputs.model_validate(value["quote_inputs"])
    evidence = {sha: Path(location).read_bytes() for sha, location in value["evidence_files"].items()}
    compiled = compile_interpreter_contracts(inputs, evidence=evidence)
    if {"effects", "effect_coverage_digest", "contract_sha256"} & value["deployment"].keys():
        raise ValueError("Compiler owns the exact operator contract registration")
    deployment = NativeOperatorDeployment.model_validate(
        {
            **value["deployment"],
            "effects": compiled["effects"],
            "effect_coverage_digest": compiled["effect_coverage_digest"],
            "contract_sha256": compiled["binding_sha256"],
        }
    )
    plan = registration_plan(
        inputs=inputs,
        evidence=evidence,
        deployment=deployment,
        account_id=value["account_id"],
        region=value["region"],
        registration_role_arn=value["registration_role_arn"],
    )
    return plan, runtime_environment(plan, current_environment=value["current_environment"])


def write_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as destination:
        destination.write(canonical(value) + "\n")
        destination.flush()
        os.fsync(destination.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--inputs", type=Path, required=True)
    prepare_command.add_argument("--output", type=Path, required=True)
    prepare_command.add_argument("--runtime-output", type=Path, required=True)
    apply_command = commands.add_parser("apply")
    apply_command.add_argument("--plan", type=Path, required=True)
    apply_command.add_argument("--expected-plan-sha256", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        plan, environment = prepare(args.inputs)
        write_new(args.output, plan.model_dump())
        write_new(args.runtime_output, {"data": environment})
        print(json.dumps({"plan_sha256": digest(plan.model_dump()), "registered": False}))
        return
    import boto3
    from botocore.config import Config

    plan = NativeOperatorRegistrationPlan.model_validate_json(args.plan.read_bytes())
    config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
    session = boto3.Session(region_name=plan.region)
    receipt = register_initial_plan(
        plan,
        expected_plan_sha256=args.expected_plan_sha256,
        store=NativeTestStore(table_name=plan.table, client=session.client("dynamodb", config=config)),
        sts_client=session.client("sts", config=config),
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
