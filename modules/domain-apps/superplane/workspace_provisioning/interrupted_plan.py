"""Qualification of a stopped initial read-only plan, never an apply retry.

The reviewed source calls only initialization and planning before creating a
proposal. The complete retained tree must still have an empty review directory.
Source/pod/volume provenance, expired grants, an empty versioned backend and a
fresh human decision are separate mandatory owner/Gateway checks.
"""

import hashlib
from pathlib import Path

from .planning_boundary import verify_complete_cache

KIND = "interrupted-read-only-plan-v1"
QUALIFIED_MODULE_SHA256 = (
    "268130ff25e1ce9909e346df99838c2ac9fc25c71d23afddc7d8c2236f79bb5e"
)
# Sources reviewed together: exclusive directory creation, fixed recipe,
# isolated process environment, and bounded credential bridge.
QUALIFIED_SOURCE = {
    "runtime.py": "04ae9dceaecbf10fbfc7508969823ce7db88a31d18844b981b6b991a44fbfd05",
    "authority.py": "c6dcd903052465697261f05d9da268be5011192357a2124db7f034df2f6c3b38",
    "terraform.py": "6f62db65368528ec3310d8f5c538a7f219445a86965e36682dc1b59daba69306",
    "process.py": "c95be08f81b1faf384adb55f3bf30bcbd935c7046fd56e2d492e048c12e795d2",
    "credential_bridge.py": "bda32b4c7b3582eabed4e0778bd306cd8f65b8845541b53ea00d570a1245e8ea",
    "runtime_config.py": "75d35dbb135fe86ef30a87e4a8791fde3dc5b17afef7a1f3d4c3822850096f06",
    "_data/workspaces/scripts/prepare_workspace_plan.py": "1735fa33bb949e12ff3894755b597cfb9d7777998175a37e354081b0df5e9525",
    "_data/workspaces/scripts/workspace_backend.py": "87d86e11ddd512c1e7d4e74b511cd4281608d7471e39a8645cbc90cdb043f996",
    "_data/workspaces/scripts/workspace_kms.py": "8c47ef76abf5c2db1f497be2e2a6a9b5291beabfec538f97b49a5b253aa625d0",
}


def verify_source(root):
    root = Path(root)
    actual = {}
    for name in QUALIFIED_SOURCE:
        path = (
            (root.parent / "infra/workspaces" / name.removeprefix("_data/workspaces/"))
            if name.startswith("_data/")
            else root / name
        )
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != QUALIFIED_SOURCE:
        raise ValueError("interrupted planning source is not qualified")
    return actual


def verify_retained(entries, source, documents, *, target, backend):
    import json

    source = Path(source)
    files = [
        p
        for p in source.iterdir()
        if p.is_file()
        and (p.suffix in {".tf", ".json"} or p.name == ".terraform.lock.hcl")
    ]
    files += [
        p
        for p in (source / "scripts").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    inventory = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(files)
    }
    if (
        hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        != QUALIFIED_MODULE_SHA256
    ):
        raise ValueError(
            "interrupted planning module is not the reviewed read-only recipe"
        )
    return verify_complete_cache(
        entries, source, documents, target=target, backend=backend
    )


def release_plan_lock(session, config, proof, *, verify):
    """Release only the old plan lock sealed in a freshly admitted retry receipt.

    The current provider intent and lease already exist. Gateway has independently
    proved expiry of every old grant/session before admitting this distinct key.
    A changed lock, state checksum or new backend version refuses the deletion.
    """
    import json

    from harness_jobs.interrupted_plan import valid_proof

    verify()
    if not valid_proof(proof):
        raise ValueError("fresh interrupted-plan receipt required")
    owner = proof["owner_evidence"]
    observed = proof["preparation_boundary"]["backend_observation"]
    backend = config["backend"]
    key = f"{config['environment']}/modules/superplane-workspaces/v2/{owner['org_id']}/{owner['workspace_id']}/terraform.tfstate"
    if (
        any(observed.get(k) != backend[k] for k in ("bucket", "region", "lock_table"))
        or observed.get("key") != key
    ):
        raise ValueError("recorded backend differs from current policy")
    s3 = session.client("s3", region_name=backend["region"])
    versions = s3.list_object_versions(
        Bucket=backend["bucket"],
        Prefix=key,
        ExpectedBucketOwner=owner["account_id"],
        MaxKeys=1,
    )
    if (
        versions.get("Versions")
        or versions.get("DeleteMarkers")
        or versions.get("IsTruncated")
    ):
        raise ValueError("workspace backend changed after inspection")
    client = session.client("dynamodb", region_name=backend["region"])
    path = backend["bucket"] + "/" + key
    for suffix in ("-md5", ""):
        item = client.get_item(
            TableName=backend["lock_table"],
            Key={"LockID": {"S": path + suffix}},
            ConsistentRead=True,
        ).get("Item")
        if suffix:
            if item:
                raise ValueError("workspace state checksum now exists")
            continue
        lock = observed.get("plan_lock")
        if lock is None:
            if item:
                raise ValueError("a new workspace lock exists")
            return
        expected = {"LockID": {"S": path}, "Info": {"S": observed["plan_lock_info"]}}
        if item != expected or json.loads(item["Info"]["S"]) != lock:
            raise ValueError("original plan lock changed")
        if (
            lock.get("Operation") != "OperationTypePlan"
            or lock.get("Who") != "@" + owner["source_pod_name"]
            or lock.get("Path") != path
        ):
            raise ValueError("lock does not belong to the stopped initial planner")
        verify()
        client.delete_item(
            TableName=backend["lock_table"],
            Key={"LockID": expected["LockID"]},
            ConditionExpression="#info = :expected",
            ExpressionAttributeNames={"#info": "Info"},
            ExpressionAttributeValues={":expected": expected["Info"]},
        )
        verify()
        if client.get_item(
            TableName=backend["lock_table"],
            Key={"LockID": {"S": path}},
            ConsistentRead=True,
        ).get("Item"):
            raise ValueError("workspace lock is present after conditional release")
