import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import publish
import intent_ledger


PILOT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
TABLE = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")


def settings():
    names = ("ADP_DEVLAKE_PILOT_ID", "ADP_DEVLAKE_LEDGER_TABLE", "ADP_DEVLAKE_ACCOUNT",
             "ADP_DEVLAKE_REGION", "ADP_DEVLAKE_FINALIZE_ROLE", "ADP_DEVLAKE_PUBLISH_ROLE",
             "ADP_DEVLAKE_CONNECTION_LABEL")
    values = [os.environ.get(name, "") for name in names]
    pilot, table, account, region, finalizer_role, publisher_role, label = values
    if (not PILOT.fullmatch(pilot) or not TABLE.fullmatch(table) or
            not re.fullmatch(r"[0-9]{12}", account) or
            not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region) or
            any(not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", role)
                for role in (finalizer_role, publisher_role)) or not label):
        raise ValueError("protected pilot, ledger and role configuration is required")
    return pilot, table, account, region, finalizer_role, publisher_role, label


def aws(label, *arguments):
    return json.loads(publish.broker(label, "aws", *arguments, "--output", "json"))


def authenticate(account, role, label):
    identity = aws(label, "sts", "get-caller-identity")
    if (identity["Account"] != account or
            not identity["Arn"].startswith(f"arn:aws:sts::{account}:assumed-role/{role}/")):
        raise ValueError("selected account and exact finalizer or publisher role are required")


def protected_table(table, account, region, label):
    details = aws(label, "dynamodb", "describe-table", "--region", region, "--table-name", table)["Table"]
    if (details["TableArn"] != f"arn:aws:dynamodb:{region}:{account}:table/{table}" or
            details["TableStatus"] != "ACTIVE" or
            details["KeySchema"] != [{"AttributeName": "pilot_id", "KeyType": "HASH"}] or
            details.get("SSEDescription", {}).get("Status") != "ENABLED"):
        raise ValueError("protected ledger table identity, key or encryption differs")
    backups = aws(label, "dynamodb", "describe-continuous-backups", "--region", region,
                  "--table-name", table)["ContinuousBackupsDescription"]
    if backups["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] != "ENABLED":
        raise ValueError("protected ledger requires point-in-time recovery")


def timestamp(value):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ledger time must include a timezone")
    return parsed


def revision():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=publish.ROOT, text=True).strip()


def load(pilot, table, region, label):
    result = aws(label, "dynamodb", "get-item", "--region", region, "--table-name", table,
                 "--consistent-read", "--key", json.dumps({"pilot_id": {"S": pilot}}))
    item = result.get("Item", {})
    if set(item) != {"pilot_id", "record"} or item["pilot_id"] != {"S": pilot}:
        raise ValueError("registered pilot is absent from protected ledger")
    return item["record"]["S"], json.loads(item["record"]["S"])


def validate(record, pilot, account, region, now, allow_expired=False):
    if (record["pilot_id"] != pilot or pilot != "devlake-" + record["finalizer_revision"] or
            record["account"] != account or record["region"] != region or
            not record["operator"] or record["finalizer_revision"] != revision() or
            not 60 <= record["poll_seconds"] <= record["deadline_seconds"] <= 86400 or
            (not allow_expired and timestamp(record["expires_at"]) <=
             now + datetime.timedelta(seconds=record["deadline_seconds"])) or
            set(record["artifacts"]) - {"api", "worker"} or not record["artifacts"]):
        raise ValueError("registered pilot, finalizer revision or bounded expiry differs")
    request = record.get("finalization_requested")
    if request and (request["reason"] not in ("abort", "failure", "expiry") or
                    timestamp(request["at"]) > now):
        raise ValueError("terminal request differs from protected ledger")
    for component, artifact in record["artifacts"].items():
        publish.verify_target(artifact["repository_uri"], account, region, "devlake-poc/" + component)
        if (not publish.DIGEST.fullmatch(artifact["digest"]) or
                artifact["tag"] != record["finalizer_revision"] + "-" + component or
                artifact["state"] not in ("registered", "pending", "accepted", "held", "cleaned")):
            raise ValueError("registered artifact intent has an invalid exact name or digest")
        if artifact["state"] == "held" and (artifact.get("hold", {}).get("operator") != record["operator"] or
                                             timestamp(artifact["hold"]["approved_at"]) > now):
            raise ValueError("held artifact lacks registered operator approval")
        receipt = artifact.get("receipt")
        if receipt and (artifact["state"] not in ("held", "cleaned") or
                        not request or receipt.get("pilot_id") != pilot or
                        receipt.get("component") != component or
                        receipt.get("repository_uri") != artifact["repository_uri"] or
                        receipt.get("tag") != artifact["tag"] or
                        receipt.get("digest") != artifact["digest"] or
                        receipt.get("checks") != ["batch-get-image", "list-images"] or
                        receipt.get("outcome") != ("retained" if artifact["state"] == "held" else "absent") or
                        timestamp(receipt["verified_at"]) > now):
            raise ValueError("artifact cleanup receipt differs from registered intent")
        if artifact["state"] == "cleaned" and not receipt:
            raise ValueError("cleaned artifact lacks independent absence receipt")
        cleanup = artifact.get("cleanup", {"attempts": 0})
        if not isinstance(cleanup["attempts"], int) or not 0 <= cleanup["attempts"] <= 3:
            raise ValueError("artifact cleanup attempt bound differs")
    if "optional_resources" in record:
        intent_ledger.validate(record["optional_resources"], account, region, record["artifacts"])
    if "tool_sources" in record:
        import tool_sources
        tool_sources.validate(record["tool_sources"], record)


def get_registered(role, allow_expired=False, label_override=None):
    pilot, table, account, region, finalizer_role, publisher_role, label = settings()
    if label_override is not None:
        if role != "finalizer" or not label_override:
            raise ValueError("intent updates require the selected finalizer connection")
        label = label_override
    authenticate(account, finalizer_role if role == "finalizer" else publisher_role, label)
    protected_table(table, account, region, label)
    previous, record = load(pilot, table, region, label)
    validate(record, pilot, account, region, datetime.datetime.now(datetime.timezone.utc), allow_expired)
    return (pilot, table, account, region, finalizer_role, publisher_role, label), previous, record



def register(path):
    pilot, table, account, region, finalizer_role, _, label = settings()
    authenticate(account, finalizer_role, label)
    protected_table(table, account, region, label)
    record = json.loads(path.read_text())
    if set(record) != {"pilot_id", "account", "region", "operator", "finalizer_revision",
                       "expires_at", "poll_seconds", "deadline_seconds", "artifacts", "probes"}:
        raise ValueError("registration requires exact protected ledger fields")
    if record["probes"] or any(set(artifact) != {"repository_uri", "digest", "tag", "state"} or
                               artifact["state"] != "registered" for artifact in record["artifacts"].values()):
        raise ValueError("new registration may not assert probes or accepted artifacts")
    now = datetime.datetime.now(datetime.timezone.utc)
    validate(record, pilot, account, region, now)
    if timestamp(record["expires_at"]) > now + datetime.timedelta(days=30):
        raise ValueError("pilot expiry exceeds the bounded registration period")
    for component, artifact in record["artifacts"].items():
        repository = "devlake-poc/" + component
        observed = aws(label, "ecr", "batch-get-image", "--region", region,
                       "--repository-name", repository, "--image-ids", "imageTag=" + artifact["tag"])
        if (observed["images"] or observed["failures"] !=
                [{"imageId": {"imageTag": artifact["tag"]}, "failureCode": "ImageNotFound"}]):
            raise ValueError("registered immutable tag already exists or lookup failed")
        inventory = aws(label, "ecr", "list-images", "--region", region,
                        "--repository-name", repository, "--filter", "tagStatus=TAGGED")
        if inventory.get("nextToken") or any(image.get("imageTag") == artifact["tag"]
                                             for image in inventory["imageIds"]):
            raise ValueError("registered immutable tag appears in ECR inventory")
    aws(label, "dynamodb", "put-item", "--region", region, "--table-name", table,
        "--condition-expression", "attribute_not_exists(pilot_id)",
        "--item", json.dumps({"pilot_id": {"S": pilot},
                              "record": {"S": json.dumps(record, sort_keys=True)}}))


def save(scope, previous, record):
    pilot, table, _, region, _, _, label = scope
    aws(label, "dynamodb", "update-item", "--region", region, "--table-name", table,
        "--key", json.dumps({"pilot_id": {"S": pilot}}),
        "--condition-expression", "#record = :previous",
        "--update-expression", "SET #record = :next",
        "--expression-attribute-names", json.dumps({"#record": "record"}),
        "--expression-attribute-values", json.dumps({":previous": {"S": previous},
                                                    ":next": {"S": json.dumps(record, sort_keys=True)}}))


def event_gate(mode):
    if mode not in ("scheduled", "manual") or os.environ.get("GITHUB_REPOSITORY") != "aws-e/adp":
        raise ValueError("finalizer probes require protected scheduled or manual execution")
    expected_event = "schedule" if mode == "scheduled" else "workflow_dispatch"
    if (os.environ.get("GITHUB_EVENT_NAME") != expected_event or
            os.environ.get("GITHUB_REF") != "refs/heads/main" or
            os.environ.get("ADP_DEVLAKE_FINALIZER_REVISION") != revision()):
        raise ValueError("finalizer mode or revision differs from protected workflow event")


def probe(mode):
    event_gate(mode)
    scope, previous, record = get_registered("finalizer")
    if record.get("finalization_requested"):
        raise ValueError("terminal pilot cannot receive a publication probe")
    _, _, account, region, finalizer_role, _, label = scope
    for component, artifact in record["artifacts"].items():
        name = "devlake-poc/" + component
        repository = aws(label, "ecr", "describe-repositories", "--region", region,
                         "--repository-names", name)["repositories"]
        if (len(repository) != 1 or repository[0]["repositoryUri"] != artifact["repository_uri"] or
                repository[0]["registryId"] != account or
                repository[0]["imageTagMutability"] != "IMMUTABLE"):
            raise ValueError("finalizer repository differs from registered intent")
        simulation = aws(label, "iam", "simulate-principal-policy", "--policy-source-arn",
                         f"arn:aws:iam::{account}:role/{finalizer_role}",
                         "--action-names", "ecr:BatchDeleteImage", "ecr:BatchGetImage",
                         "ecr:ListImages", "ecr:DescribeImages",
                         "--resource-arns", f"arn:aws:ecr:{region}:{account}:repository/{name}")
        decisions = simulation["EvaluationResults"]
        resource = f"arn:aws:ecr:{region}:{account}:repository/{name}"
        if (simulation.get("IsTruncated") or len(decisions) != 4 or
                set(result["EvalActionName"] for result in decisions) !=
                {"ecr:BatchDeleteImage", "ecr:BatchGetImage", "ecr:ListImages", "ecr:DescribeImages"} or
                any(result["EvalDecision"] != "allowed" or result["EvalResourceName"] != resource
                    for result in decisions)):
            raise ValueError("finalizer artifact cleanup authority not verified")
    record["probes"][mode] = {"revision": revision(), "role": finalizer_role,
                               "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save(scope, previous, record)


def preflight(repository_uri, digest, component):
    scope, _, record = get_registered("publisher")
    if record.get("finalization_requested"):
        raise ValueError("terminal pilot cannot authorize publication")
    if os.environ.get("GITHUB_ACTOR") != record["operator"]:
        raise ValueError("publication requires the registered operator")
    _, _, _, _, finalizer_role, _, _ = scope
    if component not in record["artifacts"]:
        raise ValueError("artifact is not registered in protected ledger")
    artifact = record["artifacts"][component]
    if (artifact["repository_uri"] != repository_uri or artifact["digest"] != digest or
            artifact["state"] not in ("registered", "pending", "accepted")):
        raise ValueError("artifact destination, digest or active intent differs from protected ledger")
    now = datetime.datetime.now(datetime.timezone.utc)
    for mode in ("scheduled", "manual"):
        evidence = record["probes"].get(mode, {})
        if (evidence.get("revision") != record["finalizer_revision"] or
                evidence.get("role") != finalizer_role or not evidence.get("at") or
                not 0 <= (now - timestamp(evidence["at"])).total_seconds() <=
                2 * record["poll_seconds"]):
            raise ValueError("recent scheduled and manual finalizer probes are required")


def main():
    parser = argparse.ArgumentParser(description="Probe protected DevLake artifact finalization")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--mode", choices=("scheduled", "manual"))
    selection.add_argument("--register", type=Path)
    parser.add_argument("--action", choices=("probe", "abort", "failure", "hold"), default="probe")
    parser.add_argument("--component", choices=("api", "worker"))
    args = parser.parse_args()
    if args.register:
        if args.action != "probe" or args.component:
            parser.error("registration does not accept a terminal action")
        register(args.register)
    else:
        if (args.component is None) != (args.action != "hold"):
            parser.error("hold requires one registered component; other actions accept none")
        if args.action == "probe" and args.mode == "manual":
            event_gate(args.mode)
        from finalize_artifacts import run
        run(args.mode, args.action, args.component)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        print(f"DevLake finalization refused: {error}", file=sys.stderr)
        sys.exit(1)
