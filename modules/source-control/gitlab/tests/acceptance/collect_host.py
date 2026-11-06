#!/usr/bin/env python3
"""Retain trusted host records through EC2's read-only console API.

This collects evidence, not an acceptance verdict. Run repeatedly during the
owned qualification while instances still exist. No SSH ingress, job profile,
SSM agent, secret or upload credential is added to a job host.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import host_journal as journal
import host_observation as host
import ipv6_metadata


def parse_console(console_output, *, expected_hash=None):
    expected_hash = expected_hash or journal.source_hash()
    raw = console_output.encode()
    host.require(len(raw) <= 1024 * 1024, "console_output_too_large")
    # EC2 console chunks can include UTC timestamps inside an otherwise intact
    # line. Strip only complete, valid provider timestamps before decoding the
    # envelope; preserve the original byte limit and all record validation.
    def timestamp_chunk(match):
        try:
            datetime.fromisoformat(match[1].decode("ascii"))
        except ValueError:
            return match[0]
        return b""

    framed = re.sub(rb"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6})\]", timestamp_chunk, raw)
    records = []
    for line in framed.splitlines():
        if not line.startswith(journal.PREFIX.encode()):
            continue
        payload = base64.b64decode(line[len(journal.PREFIX):], validate=True)
        host.require(len(payload) <= 12288, "console_record_too_large")
        row = json.loads(payload)
        host.require(set(row) == {"schema_version", "boot_id", "stream_id", "sequence", "observer_sha256",
                                  "observed_at_unix_ns", "kind", "data"}
                     and row["schema_version"] == 1 and row["observer_sha256"] == expected_hash,
                     "unreviewed_observer_record")
        host.require(re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", row["boot_id"])
                     and re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", row["stream_id"])
                     and type(row["sequence"]) is int and 1 <= row["sequence"] <= 2048
                     and type(row["observed_at_unix_ns"]) is int and row["observed_at_unix_ns"] > 0,
                     "observer_record_identity_invalid")
        host.require(row["kind"] in {"stream_started", "heartbeat", "container_event",
                     "container_observation", "observation_incomplete", "stream_incomplete"},
                     "observer_record_kind_invalid")
        records.append(row)
    host.require(records, "host_console_records_not_available")
    return records


def merge_records(previous, incoming):
    merged = {}
    for row in previous + incoming:
        key = (row["boot_id"], row["stream_id"], row["sequence"])
        host.require(key not in merged or merged[key] == row, "host_history_conflict_or_restart")
        merged[key] = row
    streams = {}
    for row in merged.values():
        streams.setdefault((row["boot_id"], row["stream_id"]), []).append(row)
    rows, problems = [], []
    for part in sorted(streams.values(), key=lambda part: min(row["observed_at_unix_ns"] for row in part)):
        part.sort(key=lambda row: row["sequence"])
        if (part[0]["kind"] != "stream_started"
                or [row["sequence"] for row in part] != list(range(1, part[-1]["sequence"] + 1))):
            problems.append("missing_history")
        if any(right["observed_at_unix_ns"] < left["observed_at_unix_ns"] for left, right in zip(part, part[1:])):
            problems.append("host_clock_moved_backwards")
        if any(row["kind"] in {"observation_incomplete", "stream_incomplete"} for row in part):
            problems.append("incomplete_host_observation")
        if rows and (part[0]["boot_id"] == rows[-1]["boot_id"]
                     and (part[0]["observed_at_unix_ns"] - rows[-1]["observed_at_unix_ns"] > 60 * 10**9
                          or part[0]["observed_at_unix_ns"] < rows[-1]["observed_at_unix_ns"])):
            problems.append("restart_observation_gap")
        rows.extend(part)
    return rows, sorted(set(problems))


def verified_instance(fixtures, instance_id, ec2):
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [instance for reservation in response["Reservations"] for instance in reservation["Instances"]]
    host.require(len(instances) == 1 and instances[0]["InstanceId"] == instance_id, "host_inventory_missing")
    instance = instances[0]
    host.require(instance["VpcId"] == fixtures["vpc_id"]
                 and instance["SubnetId"] in fixtures["private_subnet_ids"]
                 and instance["ImageId"] == fixtures["ami_id"]
                 and {sg["GroupId"] for sg in instance["SecurityGroups"]} == set(fixtures["job_security_group_ids"])
                 and not instance.get("PublicIpAddress") and not instance.get("IamInstanceProfile")
                 and not any(interface.get("Ipv6Addresses") for interface in instance.get("NetworkInterfaces", [])),
                 "job_host_scope_or_credentials_wrong")
    tags = {row["Key"]: row["Value"] for row in instance.get("Tags", [])}
    host.require(tags.get("Component") == "gitlab-job" and tags.get("Environment") == "dev"
                 and tags.get("GroupId") in {str(fixtures["group_a"]), str(fixtures["group_b"])},
                 "job_host_ownership_wrong")
    return {"instance_id": instance_id, "vpc_id": instance["VpcId"], "subnet_id": instance["SubnetId"],
            "ami_id": instance["ImageId"], "group_id": int(tags["GroupId"]),
            "security_group_ids": sorted(sg["GroupId"] for sg in instance["SecurityGroups"]),
            "instance_profile": None, "public_ip": None}


def collect(fixtures, instance_id, session, previous=None):
    host.require(session.client("sts").get_caller_identity()["Account"] == fixtures["account_id"], "wrong_account")
    ec2 = session.client("ec2")
    instance = verified_instance(fixtures, instance_id, ec2)
    scope = {"run_id": fixtures["run_id"], "source_revision": fixtures["source_revision"],
             "account_id": fixtures["account_id"], "instance": instance, "observer_sha256": journal.source_hash()}
    host.require(previous is None or previous.get("scope") == scope, "retained_host_scope_changed")
    metadata = (ipv6_metadata.collect(fixtures, instance_id, ec2, session.client("autoscaling"))
                if ipv6_metadata.enabled(fixtures) else None)
    result = ec2.get_console_output(InstanceId=instance_id, Latest=True)
    host.require(result.get("InstanceId") == instance_id and isinstance(result.get("Output"), str),
                 "provider_console_not_available")
    incoming = parse_console(result["Output"])
    for record in incoming:
        if record["kind"] in {"container_event", "container_observation", "observation_incomplete"}:
            data = record["data"]
            label = "a" if instance["group_id"] == fixtures["group_a"] else "b"
            host.require(data.get("project_id") == fixtures["project_" + label]
                         and data.get("source_revision") == fixtures["source_revision"],
                         "host_observation_outside_owned_source_and_project")
    records, problems = merge_records((previous or {}).get("records", []), incoming)
    # Preserve AWS's request identity and response time without retaining the
    # rest of the boot console (which can contain unrelated private output).
    snapshot = {"observed_at": datetime.now(timezone.utc).isoformat(),
                "aws_request_id": result.get("ResponseMetadata", {}).get("RequestId"),
                "provider_timestamp": result["Timestamp"].isoformat(),
                "console_sha256": hashlib.sha256(result["Output"].encode()).hexdigest(),
                "record_sha256s": [hashlib.sha256(json.dumps(record, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest() for record in incoming],
                "problems": problems}
    if metadata is not None:
        snapshot["ipv6_metadata"] = metadata
    host.require(snapshot["aws_request_id"], "provider_request_identity_missing")
    return {"scope": scope, "records": records,
            "snapshots": (previous or {}).get("snapshots", []) + [snapshot],
            "history_status": "incomplete" if problems else "contiguous_so_far",
            "acceptance": "requires_gitlab_correlation_and_all_qualification_checks"}


def private_file(path):
    info = path.lstat()
    host.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o600, "private_operator_file_required")
    return json.loads(path.read_text())


def main():
    import boto3
    from jsonschema import Draft202012Validator
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fixtures = private_file(args.fixtures)
        Draft202012Validator(json.loads(Path(__file__).with_name("fixtures.schema.json").read_text()),
                             format_checker=Draft202012Validator.FORMAT_CHECKER).validate(fixtures)
        host.require(re.fullmatch(r"i-[0-9a-f]{8,17}", args.instance_id), "instance_id_invalid")
        session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
        identity = session.client("sts").get_caller_identity()
        role = re.fullmatch(r"arn:aws:iam::" + fixtures["account_id"] + r":role/([A-Za-z0-9+=,.@_/-]+)", args.expected_role)
        host.require(role and re.fullmatch(r"arn:aws:sts::" + fixtures["account_id"] + r":assumed-role/"
                     + re.escape(role[1].split("/")[-1]) + r"/[^/]+", identity["Arn"]), "operator_role_wrong")
        info = args.output.parent.lstat()
        host.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o700, "private_output_directory_required")
        import fcntl
        descriptor = os.open(str(args.output) + ".lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = private_file(args.output) if args.output.exists() else None
            result = collect(fixtures, args.instance_id, session, previous)
            temporary = args.output.with_name(args.output.name + ".tmp")
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(result, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, args.output)
        print(json.dumps({"history_status": result["history_status"], "record_count": len(result["records"]),
                          "acceptance": result["acceptance"]}))
        return 0 if result["history_status"] == "contiguous_so_far" else 1
    except Exception:
        print(json.dumps({"collection": "incomplete", "reason": "trusted_host_collection_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
