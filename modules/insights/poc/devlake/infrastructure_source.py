"""Measure selected live IAM, database, lineage and artifact-cleanup sources.

Active and terminal observations come from separate protected dispatches. Neither
is a pilot criterion receipt; source jobs cannot assert scenario verdicts.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import finalize
import finalize_artifacts
import migration_receipt
import publish
from provider_profile_source import private_json


DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def selected_role(account, role, label, broker=publish.broker):
    if (not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account) or
            not isinstance(role, str) or not migration_receipt.ROLE.fullmatch(role) or
            not isinstance(label, str) or not label):
        raise ValueError("selected source account and role are required")
    identity = json.loads(broker(label, "aws", "sts", "get-caller-identity", "--output", "json"))
    if (identity.get("Account") != account or
            not identity.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/")):
        raise ValueError("selected source account or exact role differs")


def iam_policy(config, aws=finalize.aws):
    required = ("account", "role", "label", "region", "worker_role", "resource_id",
                "writer_username", "forbidden_username")
    if not isinstance(config, dict) or any(not config.get(field) for field in required):
        raise ValueError("selected IAM scope and denial probe are required")
    selected_role(config["account"], config["role"], config["label"])
    account, region, resource = (config[field] for field in ("account", "region", "resource_id"))
    if (not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region) or
            not migration_receipt.RESOURCE_ID.fullmatch(resource) or
            any(not migration_receipt.ROLE.fullmatch(config[field]) for field in
                ("worker_role", "writer_username", "forbidden_username")) or
            config["writer_username"] == config["forbidden_username"]):
        raise ValueError("IAM probe resources differ from selected worker and database")
    resources = [f"arn:aws:rds-db:{region}:{account}:dbuser:{resource}/{config[field]}"
                 for field in ("writer_username", "forbidden_username")]
    response = aws(config["label"], "iam", "simulate-principal-policy", "--policy-source-arn",
                   f"arn:aws:iam::{account}:role/{config['worker_role']}",
                   "--action-names", "rds-db:connect", "--resource-arns", *resources)
    results = response.get("EvaluationResults")
    if (not isinstance(results, list) or len(results) != 2 or response.get("IsTruncated") or
            any(not isinstance(row, dict) or row.get("EvalActionName") != "rds-db:connect"
                for row in results)):
        raise ValueError("IAM policy simulation did not deny the unselected database user")
    decisions = {row.get("EvalResourceName"): row.get("EvalDecision") for row in results}
    if (set(decisions) != set(resources) or decisions[resources[0]] != "allowed" or
            decisions[resources[1]] not in ("implicitDeny", "explicitDeny")):
        raise ValueError("IAM policy simulation did not deny the unselected database user")
    return {"scope": "policy-simulation-only", "selected_role_verified": True,
            "denied_resource_sha256": hashlib.sha256(resources[1].encode()).hexdigest()}


def s3_document(source, key, version, limit, broker=publish.broker):
    if (not isinstance(key, str) or not key or not isinstance(version, str) or
            not version or version in ("null", "latest")):
        raise ValueError("lineage requires exact S3 object keys and version IDs")
    label, region, bucket = (source[field] for field in ("label", "region", "bucket"))
    base = ("aws", "s3api")
    metadata = json.loads(broker(label, *base, "head-object", "--region", region,
                                 "--bucket", bucket, "--key", key, "--version-id", version, "--output", "json"))
    if metadata.get("VersionId") != version or not 0 < metadata.get("ContentLength", 0) <= limit:
        raise ValueError("lineage object version or size differs")
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "observation.json"
        receipt = json.loads(broker(label, *base, "get-object", "--region", region,
                                    "--bucket", bucket, "--key", key, "--version-id", version,
                                    str(destination), "--output", "json"))
        if receipt.get("VersionId") != version or destination.stat().st_size != metadata["ContentLength"]:
            raise ValueError("lineage object was not read from selected version")
        raw = destination.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("lineage object is invalid JSON") from error
    if not isinstance(document, dict):
        raise ValueError("lineage object is not a document")
    return document, hashlib.sha256(raw).hexdigest()


def lineage(config, read=s3_document):
    required = ("account", "role", "label", "region", "bucket", "tenant_id", "project_id",
                "dataset_version", "pointer_version", "snapshot_version", "policy_version", "quality_version")
    if not isinstance(config, dict) or any(not config.get(field) for field in required):
        raise ValueError("versioned lineage source and selected role are required")
    from modules.insights import results
    try:
        for field in ("tenant_id", "project_id", "dataset_version"):
            results._identifier(config[field])
    except results.ReadError as error:
        raise ValueError("selected lineage identifiers are invalid") from error
    selected_role(config["account"], config["role"], config["label"])
    prefix = f"results/v1/{config['tenant_id']}/{config['project_id']}/"
    pointer, _ = read(config, prefix + "published.json", config["pointer_version"], 4096)
    version = config["dataset_version"]
    if (pointer.get("schema_version") != "results-publication/1.0" or
            pointer.get("dataset_version") != version or
            any(not isinstance(pointer.get(field), str) or not DIGEST.fullmatch(pointer[field])
                for field in ("snapshot_sha256", "policy_sha256", "quality_sha256"))):
        raise ValueError("published lineage pointer differs from selected dataset")
    objects = {}
    for kind in ("snapshot", "policy", "quality"):
        if not config.get(kind + "_version"):
            raise ValueError("pinned lineage object version is missing")
        digest = pointer[kind + "_sha256"]
        plural = {"snapshot": "snapshots", "policy": "policies", "quality": "quality"}[kind]
        document, observed_digest = read(config, prefix + f"datasets/{version}/{plural}/{digest}.json",
                                         config[kind + "_version"], 1024 * 1024)
        if observed_digest != digest:
            raise ValueError("pinned lineage object digest differs")
        objects[kind] = document
    snapshot, policy, quality = (objects[name] for name in ("snapshot", "policy", "quality"))
    try:
        dependencies = results.policy_dependencies(policy, config["tenant_id"], config["project_id"])
    except results.ReadError as error:
        raise ValueError("published lineage policy is invalid") from error
    required_policies = {item["ref"] for envelope in policy["envelopes"]
                         for item in envelope["required_policies"]}
    try:
        snapshot_digest = results.snapshot_digest(snapshot)
    except results.ReadError as error:
        raise ValueError("published lineage snapshot is invalid") from error
    if (snapshot.get("status") != "complete" or snapshot.get("dataset_version") != version or
            quality.get("dataset_version") != version or quality.get("coverage") != "complete" or
            quality.get("cost_basis") != "actual_charge" or
            not {quality.get("source_scope"), quality.get("evidence_scope")} <= required_policies or
            policy.get("snapshot_sha256") != snapshot_digest or
            not set(results.snapshot_references(snapshot)) <= dependencies):
        raise ValueError("published lineage does not include qualified source dependencies")
    return {"pointer_version_sha256": hashlib.sha256(config["pointer_version"].encode()).hexdigest(),
            "snapshot_sha256": pointer["snapshot_sha256"], "policy_sha256": pointer["policy_sha256"],
            "quality_sha256": pointer["quality_sha256"], "dependency_count": len(dependencies)}


def finalized_cleanup(revision, read=finalize.get_registered, inspect=finalize_artifacts.inspect_image):
    scope, _, record = read("finalizer", allow_expired=True)
    if (record.get("finalizer_revision") != revision or record.get("optional_resources") or
            not record.get("finalization_requested") or not record.get("finalization_complete")):
        raise ValueError("terminal cleanup is incomplete or optional resources lack a qualified finalizer")
    states = {}
    for component, artifact in record["artifacts"].items():
        observed = inspect(scope, component, artifact)
        if (artifact["state"] == "cleaned" and observed is None and
                artifact.get("receipt", {}).get("outcome") == "absent"):
            states[component] = "absent"
        elif (artifact["state"] == "held" and observed == artifact["digest"] and
              artifact.get("receipt", {}).get("outcome") == "retained"):
            states[component] = "held"
        else:
            raise ValueError("registered artifact cleanup differs from independent ECR readback")
    return {"scope": "registered-artifacts-only", "states": states,
            "pilot_id": scope[0], "account_id": scope[2], "region": scope[3]}


def collect(config, revision, phase):
    if config.get("source_revision") != revision or not revision:
        raise ValueError("source revision does not match selected release")
    if phase == "active":
        db = config.get("database")
        if not isinstance(db, dict) or any(not db.get(field) for field in
                                           ("host", "database", "cell", "resource_id", "username",
                                            "ca_path", "region", "account", "role", "label")):
            raise ValueError("live database and selected IAM role are required")
        iam = config.get("iam")
        if not isinstance(iam, dict) or any(db[field] != iam[field] for field in
                                            ("account", "region", "resource_id")):
            raise ValueError("database and IAM policy resource pins differ")
        observations = {"iam": iam_policy(iam),
                        "database": {"schema_manifest_sha256": migration_receipt.verify_migrated(
                            migration_receipt.readback, **db), "scope": "IAM-and-TLS-migration-readback"},
                        "lineage": lineage(config.get("lineage"))}
    elif phase == "finalized":
        observations = {"cleanup": finalized_cleanup(revision)}
    else:
        raise ValueError("unknown infrastructure source phase")
    return {"scope": "live-source-measurement", "source_revision": revision,
            "phase": phase, "observed_at": datetime.now(timezone.utc).isoformat(),
            "observations": observations}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read bounded live infrastructure sources")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--phase", required=True, choices=("active", "finalized"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if private_json(args.manifest).get("source_revision") != args.revision:
            raise ValueError("campaign manifest differs from source revision")
        result = collect(private_json(args.config), args.revision, args.phase)
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(result, output, sort_keys=True)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"FAIL infrastructure source: {error if isinstance(error, ValueError) else 'source unavailable or invalid'}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
