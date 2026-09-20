#!/usr/bin/env python3
"""Prepare Anthropic account access and verify the shipped runtime defaults.

Uses AWS CLI v2 and Python's standard library. Registration data is supplied by
an operator, never invented. --verify performs bounded, paid provider calls;
it does not submit registrations or accept Marketplace agreements.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


class AccessError(RuntimeError):
    pass


def foundation_id(model):
    return re.sub(r"^(global|us|eu|apac)\.", "", model)


def validate_models(models):
    if any(
        not isinstance(m, str)
        or not re.fullmatch(
            r"(?:(?:global|us|eu|apac)\.)?anthropic\.[a-zA-Z0-9.:-]+", m
        )
        for m in models
    ):
        raise AccessError(
            "Model checks require explicit Anthropic foundation or inference-profile IDs."
        )


def runtime_models(root=ROOT):
    """Read execution sources so changing a default changes deployment checks."""
    tree = ast.parse(
        (root / "modules/agent-factory/agent-worker-image/entrypoint.py").read_text()
    )
    worker = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "effective_model" for t in node.targets
        ):
            for call in ast.walk(node.value):
                if (
                    isinstance(call, ast.Call)
                    and ast.unparse(call.func) == "os.environ.get"
                    and len(call.args) >= 2
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == "ANTHROPIC_MODEL"
                    and isinstance(call.args[1], ast.Constant)
                ):
                    worker.append(call.args[1].value)
    chat = (root / "modules/agent-factory/agent/k8s/chat-scaledjob.yaml").read_text()
    defaults = re.findall(
        r'^  (?:ANTHROPIC_MODEL|LCM_SUMMARY_MODEL):\s*"([^"\n]+)"\s*$', chat, re.M
    )
    if len(worker) != 1 or len(defaults) != 2:
        raise AccessError(
            "Cannot identify worker/chat runtime defaults; update the default reader before deploying."
        )
    models = list(dict.fromkeys(worker + defaults))
    validate_models(models)
    return models


class AWS:
    def __init__(self, region):
        self.region = region

    def call(self, service, operation, *args):
        command = [
            "aws",
            service,
            operation,
            *args,
            "--region",
            self.region,
            "--output",
            "json",
            "--no-cli-pager",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "60",
        ]
        # Do not retry paid requests invisibly or echo registration bodies.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "AWS_MAX_ATTEMPTS": "1"},
        )
        if result.returncode:
            match = re.search(r"\((\w+(?:Exception)?)\)", result.stderr)
            code = match.group(1) if match else "AWSCLIError"
            raise AccessError(
                f"{operation}: {code}. Check account permissions, model/region support and organization Marketplace policy."
            )
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError as exc:
            raise AccessError(f"{operation}: AWS CLI returned invalid JSON") from exc


def availability(aws, model):
    return aws.call(
        "bedrock",
        "get-foundation-model-availability",
        "--model-id",
        foundation_id(model),
    )


def unavailable(row):
    expected = {
        "authorizationStatus": "AUTHORIZED",
        "entitlementAvailability": "AVAILABLE",
        "regionAvailability": "AVAILABLE",
    }
    failures = [
        f"{key}={row.get(key, 'missing')}"
        for key, wanted in expected.items()
        if row.get(key) != wanted
    ]
    agreement = row.get("agreementAvailability", {}).get("status", "missing")
    if agreement != "AVAILABLE":
        failures.append(f"agreementAvailability={agreement}")
    return failures


def registration_file(path):
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    if not 10 <= len(raw) <= 16384:
        raise AccessError("Anthropic registration JSON must contain 10–16384 bytes.")
    form = json.loads(raw)
    fields = {
        "companyName",
        "companyWebsite",
        "intendedUsers",
        "industryOption",
        "otherIndustryOption",
        "useCases",
    }
    if not isinstance(form, dict) or fields - form.keys():
        raise AccessError(
            "Registration JSON needs companyName, companyWebsite, intendedUsers, industryOption, otherIndustryOption and useCases."
        )
    if any(not form[k] for k in fields - {"otherIndustryOption"}):
        raise AccessError(
            "Registration fields must contain your actual organization and use-case details."
        )
    return path


def register_if_needed(aws, required, path):
    # Effective authorization also covers organizations that registered centrally;
    # an empty per-account GetUseCase response is NOT a reason to overwrite it.
    if all(
        availability(aws, m).get("authorizationStatus") == "AUTHORIZED"
        for m in required
    ):
        return
    try:
        registered = aws.call("bedrock", "get-use-case-for-model-access").get(
            "formData"
        )
    except AccessError as exc:
        if "ResourceNotFoundException" not in str(exc):
            raise
        registered = None
    if registered:
        # CLI returns this blob base64-encoded; validate without displaying it.
        if base64.b64decode(registered).strip():
            print(
                "Anthropic registration already exists; checking model access without replacing it.",
                flush=True,
            )
            return
    if not path:
        raise AccessError(
            "Anthropic authorization is unavailable and no first-use registration was returned. "
            "Provide --use-case-file <organization.json> or ADP_BEDROCK_USE_CASE_FILE; "
            "deploy.sh accepts --anthropic-use-case <organization.json>. No infrastructure was started by this check."
        )
    form = registration_file(path)
    aws.call(
        "bedrock", "put-use-case-for-model-access", "--form-data", f"fileb://{form}"
    )
    print("Anthropic first-use registration submitted.", flush=True)


def prepare(aws, required, requested, form, wait_seconds):
    register_if_needed(aws, required, form)
    if requested:
        discovered = requested
    else:
        try:
            rows = aws.call(
                "bedrock", "list-foundation-models", "--by-provider", "anthropic"
            ).get("modelSummaries", [])
            discovered = [
                r["modelId"]
                for r in rows
                if r.get("modelLifecycle", {}).get("status") == "ACTIVE"
            ]
        except AccessError as exc:
            print(
                f"WARNING: Discovery unavailable; checking required defaults only ({exc}).",
                flush=True,
            )
            discovered = []
    required_ids = {foundation_id(m) for m in required}
    models = list(dict.fromkeys([foundation_id(m) for m in required + discovered]))
    for model in models:
        try:
            row = availability(aws, model)
            if row.get("regionAvailability") != "AVAILABLE":
                raise AccessError(
                    f"{model}: regionAvailability={row.get('regionAvailability', 'missing')}"
                )
            if row.get("agreementAvailability", {}).get("status") != "AVAILABLE":
                offers = aws.call(
                    "bedrock",
                    "list-foundation-model-agreement-offers",
                    "--model-id",
                    model,
                ).get("offers", [])
                if not offers or not offers[0].get("offerToken"):
                    raise AccessError(
                        f"{model}: no Marketplace agreement offer available"
                    )
                aws.call(
                    "bedrock",
                    "create-foundation-model-agreement",
                    "--model-id",
                    model,
                    "--offer-token",
                    offers[0]["offerToken"],
                )
                print(f"{model}: Marketplace agreement requested.", flush=True)
        except AccessError as exc:
            if model in required_ids:
                raise
            print(f"WARNING: Optional model {model}: {exc}", flush=True)
    check_required(aws, required, wait_seconds)


def check_required(aws, required, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        failures = []
        for model in required:
            reasons = unavailable(availability(aws, model))
            if reasons:
                failures.append(f"{model}: {', '.join(reasons)}")
        if not failures:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AccessError(
                "Required model access is not ready: " + "; ".join(failures)
            )
        print("Waiting for required model access: " + "; ".join(failures), flush=True)
        time.sleep(min(10, remaining))
    for model in required:
        if foundation_id(model) != model:
            profile = aws.call(
                "bedrock",
                "get-inference-profile",
                "--inference-profile-identifier",
                model,
            )
            if profile.get("status") != "ACTIVE":
                raise AccessError(
                    f"Required inference profile {model} is not ACTIVE in {aws.region}."
                )
        print(
            f"{model}: agreement, authorization, entitlement, region and profile ready.",
            flush=True,
        )


def verify_invocations(aws, models):
    # Exactly one short request per distinct default; no tool loop, SDK retry or
    # blanket sweep of the catalogue. Failure stops deployment before success.
    with tempfile.TemporaryDirectory(prefix="adp-model-check-") as directory:
        body = Path(directory) / "request.json"
        body.write_text(
            json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                }
            )
        )
        for model in models:
            response = Path(directory) / "response.json"
            response.unlink(missing_ok=True)
            aws.call(
                "bedrock-runtime",
                "invoke-model",
                "--model-id",
                model,
                "--content-type",
                "application/json",
                "--accept",
                "application/json",
                "--body",
                f"fileb://{body}",
                str(response),
            )
            result = json.loads(response.read_text())
            if result.get("type") != "message" or not any(
                c.get("type") == "text" and c.get("text", "").strip()
                for c in result.get("content", [])
            ):
                raise AccessError(
                    f"{model}: invocation returned no model text; default-model verification failed."
                )
            print(
                f"{model}: bounded Bedrock invocation passed (output limit 8 tokens).",
                flush=True,
            )
    print(
        "Default models verified with the deployment identity; agent/gateway end-to-end acceptance is separate.",
        flush=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "models",
        nargs="*",
        help="Additional required model IDs; runtime defaults are always checked",
    )
    parser.add_argument(
        "--use-case-file", default=os.environ.get("ADP_BEDROCK_USE_CASE_FILE")
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--wait-seconds", type=int, default=180)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--verify",
        action="store_true",
        help="Check defaults and invoke each once; no registration/subscription changes",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only access checks; no inference or mutations",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show required defaults without calling AWS",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 600:
        parser.error("--wait-seconds must be between 0 and 600")
    requested = args.models or os.environ.get("BEDROCK_MODELS", "").split()
    validate_models(requested)
    required = list(dict.fromkeys(runtime_models() + requested))
    print("Required default models: " + ", ".join(required), flush=True)
    if args.dry_run:
        return 0
    aws = AWS(args.region)
    if args.check or args.verify:
        check_required(aws, required, args.wait_seconds)
        if args.verify:
            verify_invocations(aws, required)
    else:
        prepare(aws, required, requested, args.use_case_file, args.wait_seconds)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AccessError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Bedrock readiness failed: {exc}", file=sys.stderr)
        sys.exit(1)
