"""Observe an empty backend and, optionally, its stopped planner's exact lock."""

import json
from datetime import datetime

from .provider_authority import require


def observe(session, backend, target, *, pod_name, acquired_at, stopped_at):
    bucket, region = backend["bucket"], backend["region"]
    key = f"{target['environment']}/modules/superplane-workspaces/v2/{target['org_id']}/{target['workspace_id']}/terraform.tfstate"
    account = target["account_id"]
    s3 = session.client("s3", region_name=region)
    require(
        s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=account).get(
            "Status"
        )
        == "Enabled"
    )
    versions = s3.list_object_versions(
        Bucket=bucket, Prefix=key, ExpectedBucketOwner=account, MaxKeys=1
    )
    require(
        not versions.get("Versions")
        and not versions.get("DeleteMarkers")
        and not versions.get("IsTruncated")
    )
    ddb = session.client("dynamodb", region_name=region)
    table = backend["lock_table"]
    require(
        ddb.describe_table(TableName=table)["Table"]["TableArn"]
        == f"arn:aws:dynamodb:{region}:{account}:table/{table}"
    )
    path = bucket + "/" + key
    require(
        not ddb.get_item(
            TableName=table, Key={"LockID": {"S": path + "-md5"}}, ConsistentRead=True
        ).get("Item")
    )
    item = ddb.get_item(
        TableName=table, Key={"LockID": {"S": path}}, ConsistentRead=True
    ).get("Item")
    lock, raw = None, None
    if item:
        require(set(item) == {"LockID", "Info"} and item["LockID"] == {"S": path})
        raw = item["Info"]["S"]
        lock = json.loads(raw)
        created = datetime.fromisoformat(lock["Created"])
        require(
            set(lock)
            == {"ID", "Operation", "Info", "Who", "Version", "Created", "Path"}
            and isinstance(lock["ID"], str)
            and 1 <= len(lock["ID"]) <= 128
            and lock["Operation"] == "OperationTypePlan"
            and lock["Version"] == "1.9.8"
            and lock["Info"] == ""
            and lock["Who"] == "@" + pod_name
            and lock["Path"] == path
            and created.tzinfo is not None
            and acquired_at <= created <= stopped_at
        )
    return {
        "bucket": bucket,
        "key": key,
        "region": region,
        "lock_table": table,
        "versioning": "Enabled",
        "versions_absent": True,
        "delete_markers_absent": True,
        "checksum_absent": True,
        "plan_lock": lock,
        "plan_lock_info": raw,
    }
