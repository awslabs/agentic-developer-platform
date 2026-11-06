"""Publish a qualified S06 result by advancing one complete S3 pointer."""

import hashlib
import json
import re

from insights import results

from .dataset import SNAPSHOT_LIMIT, _shared, build_candidate


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _immutable(storage, bucket, key, body, *, conflict_message="immutable dataset content differs"):
    try:
        storage.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                           IfNoneMatch="*")
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") not in ("PreconditionFailed", "412"):
            raise
        existing = storage.get_object(Bucket=bucket, Key=key)["Body"].read(len(body) + 1)
        if existing != body:
            raise ValueError(conflict_message) from None


def _read_bytes(storage, bucket, key, limit):
    try:
        response = storage.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None, None
        raise
    body = response["Body"].read(limit + 1)
    if len(body) > limit:
        raise ValueError("published object exceeds reader limit")
    return body, response["ETag"]


def _pending(storage, bucket, key):
    body, etag = _read_bytes(storage, bucket, key, 4096)
    return (json.loads(body) if body is not None else None), etag


def _pinned(storage, bucket, prefix, pointer, kind, digest):
    key = f"{prefix}datasets/{pointer['dataset_version']}/{kind}/{digest}.json"
    body, _ = _read_bytes(storage, bucket, key, SNAPSHOT_LIMIT if kind != "policies" else 256 * 1024)
    if body is None or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("pinned publication object is missing or damaged")
    return json.loads(body)


def _verify_readback(storage, bucket, prefix, pointer, snapshot, policy, quality):
    for kind, digest, expected in (
            ("snapshots", pointer["snapshot_sha256"], snapshot),
            ("policies", pointer["policy_sha256"], policy),
            ("quality", pointer["quality_sha256"], quality)):
        if _pinned(storage, bucket, prefix, pointer, kind, digest) != expected:
            raise ValueError("stored publication differs from pinned input")
    body, _ = _read_bytes(storage, bucket, prefix + "published.json", 4096)
    if body is None or json.loads(body) != pointer:
        raise ValueError("complete publication pointer was not readable")


def publish_candidate(*, storage, bucket, tenant_id, project_id, snapshot,
                      policy, quality, authorize):
    """Store immutable documents, then conditionally replace the complete pointer.

    The caller must supply a fresh authorization callback backed by the current
    source/evidence and Project authority, not retained input metadata.
    """
    results._identifier(tenant_id)
    results._identifier(project_id)
    if not bucket or not callable(authorize):
        raise ValueError("storage and current authority required")
    version = results._identifier(snapshot["dataset_version"])
    if (snapshot.get("status") != "complete" or quality.get("dataset_version") != version
            or quality.get("coverage") != "complete" or quality.get("cost_basis") != "actual_charge"
            or not all(isinstance(snapshot.get(field), list) for field in ("facts", "relationships"))
            or not all(isinstance(snapshot.get(field, []), list) for field in ("allocations", "contributions"))
            or any(not _shared(item) for field in ("facts", "relationships", "allocations", "contributions")
                   for item in snapshot.get(field, []))):
        raise ValueError("complete shared candidate and matching quality required")
    reconstructed, expected_quality = build_candidate(
        facts=snapshot["facts"], relationships=snapshot["relationships"],
        allocations=snapshot.get("allocations", []), tenant_id=tenant_id,
        project_id=project_id, dataset_version=version,
        mapping_versions=snapshot["mapping_versions"], as_of=quality["as_of"],
        source_scope=quality["source_scope"], evidence_scope=quality["evidence_scope"])
    if reconstructed != snapshot or any(quality.get(key) != value
                                      for key, value in expected_quality.items()):
        raise ValueError("published candidate is incomplete or totals differ")
    snapshot_body = _bytes(snapshot)
    policy_body = _bytes(policy)
    quality_body = _bytes(quality)
    if (len(snapshot_body) > SNAPSHOT_LIMIT or len(policy_body) > 256 * 1024
            or len(quality_body) > SNAPSHOT_LIMIT):
        raise ValueError("publication exceeds reader limit")
    dependencies = results.policy_dependencies(policy, tenant_id, project_id)
    if (policy["dataset_version"] != version
            or policy["snapshot_sha256"] != results.snapshot_digest(snapshot)
            or not set(results.snapshot_references(snapshot)) <= dependencies):
        raise ValueError("publication lacks a qualified dependency closure")
    required = {item["ref"] for envelope in policy["envelopes"]
                for item in envelope["required_policies"]}
    if not {quality.get("source_scope"), quality.get("evidence_scope")} <= required:
        raise ValueError("source and evidence scope are not qualified")
    if not authorize(policy):
        raise PermissionError("current publication authority denied")
    prefix = f"results/v1/{tenant_id}/{project_id}/"
    hashes = [hashlib.sha256(body).hexdigest()
              for body in (snapshot_body, policy_body, quality_body)]
    pointer = {"schema_version": "results-publication/1.0", "dataset_version": version,
               "snapshot_sha256": hashes[0], "policy_sha256": hashes[1],
               "quality_sha256": hashes[2]}
    key = prefix + "published.json"
    pending_key = prefix + "pending.json"
    current_body, condition_etag = _read_bytes(storage, bucket, key, SNAPSHOT_LIMIT)
    if current_body is not None:
        previous_document = json.loads(current_body)
        if previous_document.get("dataset_version") == version and previous_document != pointer:
            raise ValueError("dataset version already published with different content")
    _immutable(storage, bucket, f"{prefix}datasets/{version}/manifest.json", _bytes(pointer),
               conflict_message="dataset version already published with different content")
    for kind, body, digest in zip(("snapshots", "policies", "quality"),
                                  (snapshot_body, policy_body, quality_body), hashes):
        _immutable(storage, bucket, f"{prefix}datasets/{version}/{kind}/{digest}.json", body)
    journal = {"schema_version": "results-pending/1.0", "state": "pending", "pointer": pointer}
    existing, journal_etag = _pending(storage, bucket, pending_key)
    completed = {**journal, "state": "completed"}
    if existing not in (None, journal, completed) and (
            not isinstance(existing, dict) or existing.get("state") != "completed"):
        raise ValueError("another pending publication requires reconciliation")
    if existing not in (journal, completed):
        storage.put_object(Bucket=bucket, Key=pending_key, Body=_bytes(journal),
                           ContentType="application/json",
                           **({"IfMatch": journal_etag} if existing is not None else {"IfNoneMatch": "*"}))
    if not authorize(policy):
        raise PermissionError("publication authority changed")
    if current_body is None:
        condition = {"IfNoneMatch": "*"}
    else:
        condition = {"IfMatch": condition_etag}
    if current_body is None or json.loads(current_body) != pointer:
        storage.put_object(Bucket=bucket, Key=key, Body=_bytes(pointer),
                           ContentType="application/json", **condition)
    _verify_readback(storage, bucket, prefix, pointer, snapshot, policy, quality)
    if existing != completed:
        current_journal, journal_etag = _pending(storage, bucket, pending_key)
        if current_journal != journal:
            raise ValueError("pending publication changed during readback")
        storage.put_object(Bucket=bucket, Key=pending_key, Body=_bytes(completed),
                           ContentType="application/json", IfMatch=journal_etag)
    return pointer


def reconcile_pending(*, storage, bucket, tenant_id, project_id, authorize):
    """Resume one durable pending publication from exact immutable objects.

    No retained descriptor grants authority; the publisher rechecks current
    source/evidence and Project access before advancing the pointer.
    """
    results._identifier(tenant_id)
    results._identifier(project_id)
    if not bucket or not callable(authorize):
        raise ValueError("storage and current authority required")
    prefix = f"results/v1/{tenant_id}/{project_id}/"
    journal, _ = _pending(storage, bucket, prefix + "pending.json")
    if journal is None or journal.get("state") == "completed":
        return None
    if (set(journal) != {"schema_version", "state", "pointer"}
            or journal["schema_version"] != "results-pending/1.0"
            or journal["state"] != "pending" or not isinstance(journal["pointer"], dict)):
        raise ValueError("invalid pending publication")
    pointer = journal["pointer"]
    if (set(pointer) != {"schema_version", "dataset_version", "snapshot_sha256",
                         "policy_sha256", "quality_sha256"}
            or pointer["schema_version"] != "results-publication/1.0"):
        raise ValueError("invalid pending pointer")
    results._identifier(pointer["dataset_version"])
    if any(not isinstance(pointer[field], str) or not re.fullmatch(r"[0-9a-f]{64}", pointer[field])
           for field in ("snapshot_sha256", "policy_sha256", "quality_sha256")):
        raise ValueError("invalid pinned publication digest")
    snapshot = _pinned(storage, bucket, prefix, pointer, "snapshots", pointer["snapshot_sha256"])
    policy = _pinned(storage, bucket, prefix, pointer, "policies", pointer["policy_sha256"])
    quality = _pinned(storage, bucket, prefix, pointer, "quality", pointer["quality_sha256"])
    return publish_candidate(storage=storage, bucket=bucket, tenant_id=tenant_id,
                             project_id=project_id, snapshot=snapshot, policy=policy,
                             quality=quality, authorize=authorize)
