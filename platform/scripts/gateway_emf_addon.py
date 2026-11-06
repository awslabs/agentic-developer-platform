#!/usr/bin/env python3
"""Prepare or submit a scoped CloudWatch addon update from private snapshots."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import stat
import uuid
import tempfile
from pathlib import Path


ADDON_NAME = "amazon-cloudwatch-observability"
EXTRA_FILE = "gateway-emf.conf"
ROUTING_FILE = Path(__file__).resolve().parents[1] / "infra/modules/eks" / EXTRA_FILE
CONFIG_PATH = ("containerLogs", "fluentBit", "config", "extraFiles")
IDENTITY_FIELDS = ("clusterName", "addonName", "addonVersion", "serviceAccountRoleArn")
MAX_RECORD_BYTES = 32768


def addon_configuration(snapshot):
    addon = snapshot.get("addon") if isinstance(snapshot, dict) else None
    if not isinstance(addon, dict) or addon.get("addonName") != ADDON_NAME:
        raise ValueError("Expected a describe-addon snapshot for the CloudWatch observability addon")
    raw = addon.get("configurationValues") or "{}"
    if not isinstance(raw, str):
        raise ValueError("Addon configurationValues must be a JSON string")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Addon configurationValues must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Addon configurationValues must be a JSON object")
    return value


def extra_files(configuration, create):
    current = configuration
    for key in CONFIG_PATH:
        if key not in current:
            if not create:
                return None
            current[key] = {}
        if not isinstance(current[key], dict):
            raise ValueError("Unsupported addon configuration at " + ".".join(CONFIG_PATH))
        current = current[key]
    return current


def prepare(before, baseline, rollback=False):
    before_addon = before.get("addon") if isinstance(before, dict) else None
    baseline_addon = baseline.get("addon") if isinstance(baseline, dict) else None
    if not isinstance(before_addon, dict) or not isinstance(baseline_addon, dict):
        raise ValueError("Both inputs must be complete describe-addon snapshots")
    for field in IDENTITY_FIELDS:
        if not before_addon.get(field) or before_addon[field] != baseline_addon.get(field):
            raise ValueError("Addon target, version or collector identity differs from the pinned baseline")
    current = addon_configuration(before)
    expected = addon_configuration(baseline)
    if not rollback and baseline_addon.get("modifiedAt") != before_addon.get("modifiedAt"):
        raise ValueError("Addon changed since the pinned baseline")
    if not rollback and current != expected:
        raise ValueError("Addon configuration differs from the pinned baseline; inspect concurrent changes")
    routing = ROUTING_FILE.read_text()
    files = extra_files(current, create=not rollback)
    if rollback:
        baseline_files = extra_files(expected, create=False)
        if baseline_files is not None and EXTRA_FILE in baseline_files:
            raise ValueError("Baseline already owns the gateway routing file; refuse to remove it")
        if files is None or files.get(EXTRA_FILE) != routing:
            raise ValueError("Gateway routing file is absent or changed; refuse rollback")
        del files[EXTRA_FILE]
        for position in range(len(CONFIG_PATH) - 1, -1, -1):
            keys = CONFIG_PATH[:position + 1]
            parent = current
            original = expected
            for key in keys[:-1]:
                parent = parent[key]
                original = original.get(key, {}) if isinstance(original, dict) else {}
            if parent[keys[-1]] or (isinstance(original, dict) and keys[-1] in original):
                break
            del parent[keys[-1]]
    elif EXTRA_FILE in files and files[EXTRA_FILE] != routing:
        raise ValueError("Gateway routing file already exists with different contents")
    elif not rollback:
        files[EXTRA_FILE] = routing
    return current


def broker_aws(label, *arguments):
    command = ["adp-cred", "assume", "--service", "aws", "--label", label,
               "--exec", "aws", *arguments, "--output", "json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("AWS operation timed out; reconcile addon state before retrying") from exc
    if result.returncode:
        raise RuntimeError("AWS operation failed; reconcile addon state before retrying")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS response is invalid; reconcile addon state before retrying") from exc


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_revision():
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(["git", "-C", str(root), "rev-parse", "HEAD"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    output, _ = process.communicate(timeout=10)
    if process.returncode or not re.fullmatch(r"[a-f0-9]{40,64}", output.strip()):
        raise ValueError("Reviewed source revision is unavailable")
    return output.strip()


def save_record(path, record, create=False):
    parent = os.stat(path.parent, follow_symlinks=False)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
        raise ValueError("Operation record requires a private directory")
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("Operation record exceeds the private size limit")
    if create:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    else:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_record(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_RECORD_BYTES:
            raise ValueError("Operation record must be a bounded private regular file")
        record = json.loads(source.read())
    if record.get("schema") != 1 or not record.get("clientRequestToken"):
        raise ValueError("Unsupported operation record")
    return record


def verify_operator(label, account, assumed_role, run):
    role = re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):sts::([0-9]{12}):assumed-role/[^/]+", assumed_role)
    if role is None or role[2] != account:
        raise ValueError("Pin the exact expected account and assumed operator role")
    identity = run(label, "sts", "get-caller-identity")
    if identity.get("Account") != account or identity.get("Arn", "").rsplit("/", 1)[0] != assumed_role:
        raise ValueError("Selected AWS connection is not the approved account and operator role")


def readback(record, label, request, run):
    try:
        actual = run(label, *request)
        addon = actual["addon"]
        result = {"kind": "readback", "observedAt": now_utc(), "status": addon.get("status"),
                  "modifiedAt": addon.get("modifiedAt"),
                  "configurationSha256": fingerprint(addon_configuration(actual)),
                  "addonVersion": addon.get("addonVersion"),
                  "collectorRole": addon.get("serviceAccountRoleArn")}
        result["targetMatches"] = (addon.get("clusterName") == record["target"]["cluster"]
                                   and addon.get("addonVersion") == record["target"]["addonVersion"]
                                   and addon.get("serviceAccountRoleArn") == record["target"]["collectorRole"])
        result["matchesCandidate"] = result["configurationSha256"] == record["candidate"]["configurationSha256"]
    except (KeyError, TypeError, ValueError, RuntimeError):
        result = {"kind": "unavailable", "observedAt": now_utc()}
    record["readback"] = result
    return result


def recover_addon(baseline, receipt, action, label, account, assumed_role, region, cluster, run=broker_aws):
    record = read_record(receipt)
    target = record["target"]
    expected = {"account": account, "assumedRole": assumed_role, "credentialLabel": label,
                "region": region, "cluster": cluster, "addonName": ADDON_NAME,
                "addonVersion": baseline["addon"]["addonVersion"],
                "collectorRole": baseline["addon"]["serviceAccountRoleArn"]}
    source = {"revision": source_revision(),
              "routingSha256": hashlib.sha256(ROUTING_FILE.read_bytes()).hexdigest()}
    if (record["action"] != action or record["baselineSha256"] != fingerprint(baseline)
            or target != expected or record["source"] != source):
        raise ValueError("Recovery arguments or source differ from the recorded operation")
    verify_operator(label, account, assumed_role, run)
    if record["state"] == "intent":
        record["state"] = "unknown"
        record["result"] = {"kind": "unknown", "reason": "interrupted-before-acknowledgement"}
        save_record(receipt, record)
    update_id = record.get("result", {}).get("updateId")
    if update_id:
        try:
            update = run(label, "eks", "describe-update", "--cluster-name", cluster,
                         "--name", ADDON_NAME, "--update-id", update_id, "--region", region)
            status = update["update"].get("status")
            record["updateReadback"] = {"kind": "readback", "observedAt": now_utc(), "status": status}
        except (KeyError, TypeError, RuntimeError):
            record["updateReadback"] = {"kind": "unavailable", "observedAt": now_utc()}
    else:
        record["updateReadback"] = {"kind": "unavailable", "reason": "update-id-unavailable",
                                    "observedAt": now_utc()}
    request = ("eks", "describe-addon", "--cluster-name", cluster,
               "--addon-name", ADDON_NAME, "--region", region)
    readback(record, label, request, run)
    save_record(receipt, record)
    return record


def update_addon(baseline, rollback, label, account, assumed_role, region, cluster, run=broker_aws, receipt=None):
    if receipt is None:
        raise ValueError("A private operation record path is required before an addon mutation")
    receipt = Path(receipt)
    verify_operator(label, account, assumed_role, run)
    request = ("eks", "describe-addon", "--cluster-name", cluster,
               "--addon-name", ADDON_NAME, "--region", region)
    before = run(label, *request)
    if before.get("addon", {}).get("clusterName") != cluster or before["addon"].get("status") != "ACTIVE":
        raise ValueError("Target addon is missing or not ACTIVE; refuse to change it")
    candidate = prepare(before, baseline, rollback=rollback)
    if candidate == addon_configuration(before):
        raise ValueError("No routing delta; refuse to restart an already configured addon")
    refreshed = run(label, *request)
    keys = (*IDENTITY_FIELDS, "modifiedAt", "status")
    if (any(refreshed.get("addon", {}).get(key) != before["addon"].get(key) for key in keys)
            or addon_configuration(refreshed) != addon_configuration(before)):
        raise ValueError("Addon changed before update; re-pin and inspect concurrent edits")
    operation = {
        "schema": 1, "state": "intent", "action": "revert" if rollback else "apply",
        "clientRequestToken": str(uuid.uuid4()), "createdAt": now_utc(),
        "target": {"account": account, "assumedRole": assumed_role, "credentialLabel": label,
                   "region": region, "cluster": cluster, "addonName": ADDON_NAME,
                   "addonVersion": before["addon"]["addonVersion"],
                   "collectorRole": before["addon"]["serviceAccountRoleArn"]},
        "source": {"revision": source_revision(),
                   "routingSha256": hashlib.sha256(ROUTING_FILE.read_bytes()).hexdigest()},
        "baselineSha256": fingerprint(baseline),
        "before": {"configurationSha256": fingerprint(addon_configuration(before)),
                   "modifiedAt": before["addon"].get("modifiedAt"), "status": before["addon"].get("status")},
        "candidate": {"configurationSha256": fingerprint(candidate)},
        "result": {"kind": "intent"},
    }
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "configuration.json"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(candidate, separators=(",", ":")))
        save_record(receipt, operation, create=True)
        try:
            result = run(label, "eks", "update-addon", "--cluster-name", cluster,
                         "--addon-name", ADDON_NAME, "--region", region,
                         "--configuration-values", "file://" + str(config),
                         "--resolve-conflicts", "NONE", "--client-request-token", operation["clientRequestToken"])
            update = result.get("update", {})
            if not update.get("id"):
                raise RuntimeError("Update response has no ID")
        except RuntimeError as exc:
            operation["state"] = "unknown"
            operation["result"] = {"kind": "unknown", "reason": "response-unavailable"}
            save_record(receipt, operation)
            readback(operation, label, request, run)
            save_record(receipt, operation)
            raise RuntimeError("Update acknowledgement unknown; read-only recovery required; "
                               "matching configuration is not proof of success") from exc
        operation["state"] = "submitted"
        operation["result"] = {"kind": "submitted", "updateId": update["id"],
                               "status": update.get("status")}
        save_record(receipt, operation)
        readback(operation, label, request, run)
        save_record(receipt, operation)
        return result



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "rollback", "apply", "revert", "recover"))
    parser.add_argument("--before", type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--expected-action", choices=("apply", "revert"))
    parser.add_argument("--aws-label")
    parser.add_argument("--expected-account")
    parser.add_argument("--expected-assumed-role")
    parser.add_argument("--region")
    parser.add_argument("--cluster")
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    if args.action in ("prepare", "rollback"):
        if args.before is None or args.output is None:
            parser.error("Offline preparation requires --before and --output")
        candidate = prepare(json.loads(args.before.read_text()), baseline,
                            rollback=args.action == "rollback")
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(candidate, separators=(",", ":")) + "\n")
    else:
        if not all((args.aws_label, args.expected_account, args.expected_assumed_role, args.region, args.cluster)):
            parser.error("Live update requires --aws-label, --expected-account, --expected-assumed-role, --region and --cluster")
        if args.receipt is None:
            parser.error("Live update requires --receipt in a protected directory")
        if args.action == "recover":
            if args.expected_action is None:
                parser.error("Recovery requires --expected-action apply or revert")
            recover_addon(baseline, args.receipt, args.expected_action, args.aws_label,
                          args.expected_account, args.expected_assumed_role, args.region, args.cluster)
        else:
            update_addon(baseline, args.action == "revert", args.aws_label, args.expected_account,
                         args.expected_assumed_role, args.region, args.cluster, receipt=args.receipt)


if __name__ == "__main__":
    main()
