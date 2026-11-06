"""Fresh, versioned partial state and unused EIP observations; no writes."""

import hashlib
import json

from .partial_apply_plan import EIP, require, state_inventory


def read_snapshot(session, backend, target, *, expected=None):
    key = (
        f"{target['environment']}/modules/superplane-workspaces/v2/"
        f"{target['org_id']}/{target['workspace_id']}/terraform.tfstate"
    )
    bucket = backend["bucket"]
    region = backend["region"]
    owner = target["account_id"]
    s3 = session.client("s3", region_name=region)
    ddb = session.client("dynamodb", region_name=region)
    require(
        s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=owner).get("Status")
        == "Enabled",
        "backend versioning disabled",
    )
    lock_key = bucket + "/" + key

    def unlocked():
        require(
            not ddb.get_item(
                TableName=backend["lock_table"],
                Key={"LockID": {"S": lock_key}},
                ConsistentRead=True,
            ).get("Item"),
            "backend is locked",
        )

    unlocked()
    response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
    body = response["Body"]
    try:
        raw = body.read((64 << 20) + 1)
    finally:
        body.close()
    require(0 < len(raw) <= 64 << 20, "backend state size")
    version = response.get("VersionId")
    require(
        isinstance(version, str) and version not in {"", "null"},
        "backend version unavailable",
    )
    checksum = (
        ddb.get_item(
            TableName=backend["lock_table"],
            Key={"LockID": {"S": lock_key + "-md5"}},
            ConsistentRead=True,
        )
        .get("Item", {})
        .get("Digest", {})
        .get("S")
    )
    require(
        checksum == hashlib.md5(raw, usedforsecurity=False).hexdigest(),
        "backend checksum inconsistent",
    )
    state = json.loads(raw)
    inventory = state_inventory(state, target)
    observed = {
        "bucket": bucket,
        "key": key,
        "region": region,
        "lock_table": backend["lock_table"],
        "version_id": version,
        "sha256": hashlib.sha256(raw).hexdigest(),
        **inventory,
    }
    if expected is not None:
        require(observed == expected, "partial state changed since owner observation")
    # Only the exact tainted, unassociated address may be replaced. Never make a
    # fresh plan silently claim that a used address can be discarded.
    ec2 = session.client("ec2", region_name=target["aws_region"])
    identifier = inventory["resources"][EIP]["id"]
    addresses = ec2.describe_addresses(AllocationIds=[identifier])["Addresses"]
    require(len(addresses) == 1, "EIP observation ambiguous")
    address = addresses[0]
    tags = {t["Key"]: t["Value"] for t in address.get("Tags", [])}
    require(
        address.get("AllocationId") == identifier and address.get("Domain") == "vpc",
        "EIP identity changed",
    )
    require(
        tags.get("OrgId") == target["org_id"]
        and tags.get("WorkspaceId") == target["workspace_id"],
        "EIP ownership changed",
    )
    require(
        not any(
            address.get(k)
            for k in [
                "AssociationId",
                "NetworkInterfaceId",
                "InstanceId",
                "PrivateIpAddress",
            ]
        ),
        "EIP is in use",
    )
    eip = next(
        r
        for r in state["resources"]
        if r.get("mode") == "managed"
        and r.get("type") == "aws_eip"
        and r.get("name") == "nat"
    )["instances"][0]["attributes"]
    require(
        eip.get("public_ip") == address.get("PublicIp")
        and isinstance(address.get("PublicIp"), str),
        "EIP public address changed",
    )
    require(
        s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner).get(
            "VersionId"
        )
        == version,
        "backend version changed during read",
    )
    unlocked()
    return observed
