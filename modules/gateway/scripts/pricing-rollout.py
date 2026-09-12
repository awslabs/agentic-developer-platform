#!/usr/bin/env python3
"""Quiesce pricing before code/infra updates; verify seed and finalize afterward.

Uses existing AWS CLI and kubectl credentials. It never prints tokens or database
connection strings. A failed step leaves the refresh schedule disabled.
"""

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def command(args, *, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result.stdout


def aws(args, *parts, missing_ok=False):
    result = subprocess.run(
        ["aws", *parts, "--region", args.region, "--output", "json", "--cli-connect-timeout", "10", "--cli-read-timeout", "240"],
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode:
        if missing_ok and ("ResourceNotFoundException" in result.stderr or "ResourceNotFound" in result.stderr):
            return None
        raise RuntimeError(f"AWS {parts[0]} {parts[1]} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def ready_pods(args):
    deployment = json.loads(command(["kubectl", "get", "deployment/bedrockgateway", "-n", args.namespace, "-o", "json"]))
    image = next(c["image"] for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "bedrockgateway")
    if deployment["spec"].get("replicas", 1) < 1:
        raise RuntimeError("Gateway has no requested serving replicas")
    if args.expected_image and image != args.expected_image:
        raise RuntimeError("Gateway deployment does not use the expected release image")
    pods = json.loads(command(["kubectl", "get", "pods", "-n", args.namespace, "-l", "app=bedrockgateway", "-o", "json"]))["items"]
    selected = []
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp"):
            continue
        conditions = pod.get("status", {}).get("conditions", [])
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
            continue
        containers = pod["spec"]["containers"]
        if any(c["name"] == "bedrockgateway" and c["image"] == image for c in containers):
            selected.append(pod["metadata"]["name"])
    if not selected or len(selected) < deployment["spec"].get("replicas", 1):
        raise RuntimeError("Not all required gateway replicas are Ready on the release image")
    return sorted(selected), image


SEED_PROBE = r"""
import asyncio, json
from sqlalchemy import text
from pricing_policy import load_snapshot
from src.shared.database import get_engine
from src.chat_logging.config import get_chat_logging_settings

async def main():
    engine = get_engine()
    async with engine.connect() as connection:
        pointer = (await connection.execute(text("SELECT * FROM model_pricing_active WHERE singleton"))).mappings().one()
        assert pointer["consumers_enabled"] and pointer["current_generation_id"] is not None, "Pricing not activated"
        generation = (await connection.execute(
            text("SELECT * FROM model_pricing_generations WHERE generation_id=:gid"),
            {"gid": pointer["current_generation_id"]},
        )).mappings().one()
        assert generation["status"] == "validated", "Unvalidated pricing generation"
        rows = (await connection.execute(
            text("SELECT * FROM model_pricing_rates_v2 WHERE generation_id=:gid"),
            {"gid": pointer["current_generation_id"]},
        )).mappings().all()
        keys = {(r["model_id"], r["geography"], r["service_tier"], r["context_tier"], r["region"]) for r in rows}
        required = {tuple(key) for key in generation["required_variants"]}
        assert required and required <= keys, "Incomplete active generation"
        snapshot = load_snapshot()
        bundled = {(r.model_id, r.geography, r.service_tier, r.context_tier, r.region) for r in snapshot.rates}
        assert bundled <= keys, "Active pricing omits bundled variants"
        assert len({r["model_id"] for r in rows}) >= 12, "OpenAI model coverage incomplete"
        revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        print(json.dumps({
            "generation_id": pointer["current_generation_id"], "pointer_revision": pointer["pointer_revision"],
            "variants": len(keys), "snapshot_version": snapshot.snapshot_version,
            "alembic_revision": revision, "refresh_paused": pointer["refresh_paused"],
            "chat_logging_enabled": get_chat_logging_settings().chat_logging_enabled,
        }))
    await engine.dispose()
asyncio.run(main())
"""


def verify_seed(args, *, migrate=False):
    pods, image = ready_pods(args)
    if migrate:
        command(
            ["kubectl", "exec", "-n", args.namespace, pods[0], "-c", "bedrockgateway", "--", "env", "PYTHONPATH=/app", "alembic", "upgrade", "head"]
        )
    evidence = []
    for pod in pods:
        output = command(
            ["kubectl", "exec", "-i", "-n", args.namespace, pod, "-c", "bedrockgateway", "--", "env", "PYTHONPATH=/app", "python", "-"],
            input_text=SEED_PROBE,
        )
        evidence.append({"pod": pod, "pricing": json.loads(output)})
    print(json.dumps({"image": image, "replicas": evidence}))
    return evidence


def quiesce(args):
    rule = f"bedrockgw-{args.environment}-pricing-refresh-schedule"
    if aws(args, "events", "describe-rule", "--name", rule, missing_ok=True) is not None:
        aws(args, "events", "disable-rule", "--name", rule)
    config = aws(args, "lambda", "get-function-configuration", "--function-name", f"bedrockgw-{args.environment}-pricing-refresh", missing_ok=True)
    if config:
        delay = config["Timeout"] + 5
        print(f"Refresh disabled; allowing {delay}s for an old invocation to drain", flush=True)
        # Keep long drains observable without changing the required old-timeout
        # bound. Disabling the rule does not cancel an in-flight invocation.
        while delay:
            interval = min(delay, 30)
            time.sleep(interval)
            delay -= interval
            if delay:
                print(f"Waiting for old refresh invocation: {delay}s remaining", flush=True)


def verify_lambda_code(args, function):
    """Compare normalized deployed ZIP contents with this reviewed checkout."""
    deployed = aws(args, "lambda", "get-function", "--function-name", function)
    config = deployed["Configuration"]
    assert config["State"] == "Active" and config["LastUpdateStatus"] == "Successful", f"{function} is not ready"
    spec = importlib.util.spec_from_file_location("pricing_archive_builder", Path(__file__).with_name("build-budget-lambda-archives.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    short_name = function.removeprefix(f"bedrockgw-{args.environment}-")
    expected = builder.manifest(Path(__file__).resolve().parents[1], short_name)
    # Lambda's signed download URL is kept out of commands, logs and exceptions.
    try:
        with urllib.request.urlopen(deployed["Code"]["Location"], timeout=30) as response:
            payload = response.read(10 * 1024 * 1024 + 1)
    except Exception:
        raise RuntimeError(f"Could not download {function} code for release verification") from None
    assert len(payload) <= 10 * 1024 * 1024, "Lambda archive exceeds verification size bound"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())), "Duplicate deployed ZIP members"
        assert set(archive.namelist()) == set(expected), f"{function} archive manifest differs from this release"
        assert all(archive.read(name) == path.read_bytes() for name, path in expected.items()), f"{function} source differs from this release"
    return config


def verify_queue(args, arn):
    parts = arn.split(":")
    assert len(parts) == 6 and parts[2] == "sqs" and parts[4] == args.account_id, "Unexpected pricing failure queue"
    url = aws(args, "sqs", "get-queue-url", "--queue-name", parts[5], "--queue-owner-aws-account-id", parts[4])["QueueUrl"]
    attributes = aws(args, "sqs", "get-queue-attributes", "--queue-url", url, "--attribute-names", "All")["Attributes"]
    assert attributes["QueueArn"] == arn
    assert int(attributes["MessageRetentionPeriod"]) == 1209600, "Pricing failure retention is not 14 days"
    assert attributes.get("SqsManagedSseEnabled") == "true", "Pricing queue encryption is not configured"


def verify_alarm_routes(args):
    names = [f"bedrockgw-{args.environment}-pricing-{suffix}" for suffix in ("full-refresh-missing", "oldest-source")]
    alarms = aws(args, "cloudwatch", "describe-alarms", "--alarm-names", *names)["MetricAlarms"]
    assert {alarm["AlarmName"] for alarm in alarms} == set(names), "Required pricing freshness alarms are missing"
    topics = set()
    for alarm in alarms:
        assert alarm["ActionsEnabled"] and alarm["AlarmActions"], "Pricing alarm has no enabled notification destination"
        assert alarm["TreatMissingData"] == "breaching", "Pricing freshness alarm does not detect missing measurements"
        topics.update(alarm["AlarmActions"])
    for topic in sorted(topics):
        subscriptions = []
        token = None
        while True:
            extra = ("--next-token", token) if token else ()
            page = aws(args, "sns", "list-subscriptions-by-topic", "--topic-arn", topic, *extra)
            subscriptions.extend(page["Subscriptions"])
            token = page.get("NextToken")
            if not token:
                break
        assert any(s["SubscriptionArn"].startswith("arn:") for s in subscriptions), "Pricing alarm topic has no confirmed subscription"
        if topic.endswith(f":bedrockgw-{args.environment}-pricing-alarms"):
            inboxes = [s["Endpoint"] for s in subscriptions if s["Protocol"] == "sqs" and s["SubscriptionArn"].startswith("arn:")]
            assert inboxes, "Default pricing alarm inbox subscription is missing"
            for inbox in inboxes:
                verify_queue(args, inbox)


def finalize(args):
    rule_name = f"bedrockgw-{args.environment}-pricing-refresh-schedule"
    existing_rule = aws(args, "events", "describe-rule", "--name", rule_name, missing_ok=True)
    if existing_rule is not None:
        aws(args, "events", "disable-rule", "--name", rule_name)
    evidence = verify_seed(args)
    logging_states = {item["pricing"]["chat_logging_enabled"] for item in evidence}
    assert len(logging_states) == 1, "Gateway replicas disagree on chat logging configuration"
    if logging_states == {False}:
        print("Pricing seed verified; scheduled refresh remains disabled because chat logging is disabled")
        return
    assert existing_rule is not None, (
        "Pricing is enabled but its schedule is missing; apply reviewed budget-lambda infrastructure and rerun finalization"
    )
    if any(item["pricing"]["refresh_paused"] for item in evidence):
        raise RuntimeError("Refresh is explicitly paused; resolve rollback before finalizing")
    function = f"bedrockgw-{args.environment}-pricing-refresh"
    config = verify_lambda_code(args, function)
    verify_lambda_code(args, f"bedrockgw-{args.environment}-budget-usage-tracker")
    assert config["Timeout"] >= 180, "Pricing infrastructure timeout has not been applied"
    asynchronous = aws(args, "lambda", "get-function-event-invoke-config", "--function-name", function)
    assert asynchronous["MaximumRetryAttempts"] == 2 and asynchronous["MaximumEventAgeInSeconds"] == 3600
    execution_queue = asynchronous["DestinationConfig"]["OnFailure"]["Destination"]
    rule = aws(args, "events", "describe-rule", "--name", rule_name)
    assert rule["ScheduleExpression"] == "cron(0 6 * * ? *)"
    targets = aws(args, "events", "list-targets-by-rule", "--rule", rule_name)["Targets"]
    target = next(t for t in targets if t["Arn"] == config["FunctionArn"])
    assert target["RetryPolicy"] == {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}
    assert target["DeadLetterConfig"]["Arn"] != execution_queue
    verify_queue(args, target["DeadLetterConfig"]["Arn"])
    verify_queue(args, execution_queue)
    verify_alarm_routes(args)
    with tempfile.TemporaryDirectory() as temporary:
        payload = Path(temporary) / "refresh.json"
        result = aws(
            args,
            "lambda",
            "invoke",
            "--function-name",
            function,
            "--invocation-type",
            "RequestResponse",
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            "{}",
            str(payload),
        )
        if result.get("FunctionError") or result.get("StatusCode") != 200:
            raise RuntimeError("Immediate pricing refresh failed; inspect Lambda logs. Schedule remains disabled.")
        refresh = json.loads(payload.read_text())
        assert refresh.get("status") == "published" and not refresh.get("partial"), "Refresh did not report a full publication"
        print(json.dumps({"immediate_refresh": refresh}))
    after = verify_seed(args)
    assert after[0]["pricing"]["pointer_revision"] > evidence[0]["pricing"]["pointer_revision"], "Refresh did not publish a new generation"
    for item in after:
        assert item["pricing"]["generation_id"] == refresh["generation_id"], "Active generation differs from the verified refresh"
        assert item["pricing"]["pointer_revision"] == refresh["pointer_revision"], "Active pointer differs from the verified refresh"
        assert item["pricing"]["variants"] == refresh["variants"], "Refresh coverage differs from the active generation"
    try:
        aws(args, "events", "enable-rule", "--name", rule_name)
        assert aws(args, "events", "describe-rule", "--name", rule_name)["State"] == "ENABLED"
    except Exception:
        aws(args, "events", "disable-rule", "--name", rule_name)
        raise
    print("Pricing generation published; daily 06:00 UTC refresh enabled")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("quiesce", "migrate", "verify-seed", "finalize"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT", "dev"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--namespace", default="adp-gateway")
    parser.add_argument("--expected-image")
    args = parser.parse_args()
    try:
        assert aws(args, "sts", "get-caller-identity")["Account"] == args.account_id, "AWS account mismatch"
        if args.phase == "finalize" and not args.expected_image:
            raise RuntimeError("Finalization requires --expected-image from the reviewed release")
        if args.phase == "quiesce":
            quiesce(args)
        elif args.phase == "finalize":
            finalize(args)
        else:
            verify_seed(args, migrate=args.phase == "migrate")
    except (AssertionError, RuntimeError, KeyError, StopIteration, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        print(f"Pricing rollout failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
