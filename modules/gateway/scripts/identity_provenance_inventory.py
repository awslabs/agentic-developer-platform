#!/usr/bin/env python3
"""Offline S11 inventory. Candidate evidence never grants or repairs authority.

Only local, bounded JSON input and an optional new private manifest are used.
Current source audit events do not bind an identity-row lifecycle or establish
trusted export provenance. Recorded proven labels are not historical proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = "2.0.0"
REVIEWED_SOURCE_SHA = "113858f2bd4915d8dd80626792e90e5ccb52e9fe"
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_ROWS_PER_TABLE = 100_000
PROVEN_LABELS = frozenset({"oauth", "org_placement", "admin_attested", "magic_link_confirmed"})
UNPROVEN_LABELS = frozenset({"self_asserted", "magic_link", "channel_placement", "admin_manual"})
EVENT_TYPES = frozenset({"shadow_user_created", "identity_linked", "identity_unlinked", "magic_link_consumed", "magic_link_issued"})
TABLES = ("user_identities", "users", "security_audit_logs")
IDENTITY_FIELDS = (
    "id",
    "org_id",
    "user_id",
    "provider",
    "provider_user_id",
    "verification_method",
    "created_at",
    "verified_at",
    "updated_at",
    "team_id",
    "is_primary",
)
USER_FIELDS = ("id", "org_id", "is_shadow", "user_kind", "bot_kind", "created_at", "updated_at")
DETAIL_FIELDS = ("provider", "provider_user_id", "shadow_user_id", "verification_method", "delivery_method", "ownership_proven")


class SnapshotError(Exception):
    """Messages are fixed codes; never include untrusted snapshot values."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise SnapshotError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SnapshotError("invalid_timestamp") from None
    if result.tzinfo is None:
        raise SnapshotError("timezone_required")
    return result.astimezone(UTC)


def string(value: Any, *, nullable: bool = False, empty: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or len(value) > 512 or (not value and not empty):
        raise SnapshotError("invalid_string_field")
    if any(ord(c) < 32 for c in value):
        raise SnapshotError("invalid_string_field")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("duplicate_json_key")
        result[key] = value
    return result


def load_snapshot(path: str, tenant: str) -> dict:
    """Read exactly one bounded regular-file snapshot; reject malformed rows."""
    string(tenant)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("invalid_snapshot_file")
        data = file.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot_too_large")
    raw = json.loads(data, object_pairs_hook=_no_duplicate_keys, parse_constant=lambda _: (_ for _ in ()).throw(SnapshotError("invalid_number")))
    validate_snapshot(raw, tenant)
    return raw


def validate_snapshot(raw: Any, tenant: str) -> None:
    if not isinstance(raw, dict) or set(raw) != set(TABLES):
        raise SnapshotError("invalid_snapshot_tables")
    for table in TABLES:
        rows = raw[table]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS_PER_TABLE:
            raise SnapshotError("invalid_table_rows")
        seen = set()
        accounts = set()
        for row in rows:
            if not isinstance(row, dict):
                raise SnapshotError("invalid_row")
            for field in ("id", "org_id"):
                string(row.get(field))
            if row["org_id"] != tenant:
                raise SnapshotError("cross_tenant_or_user_tenant_mismatch")
            if table != "security_audit_logs" and row["id"] in seen:
                raise SnapshotError("duplicate_row_id")
            seen.add(row["id"])
            if table == "user_identities":
                if any(field not in row for field in IDENTITY_FIELDS):
                    raise SnapshotError("missing_identity_state")
                for field in ("user_id", "provider", "provider_user_id"):
                    string(row[field])
                string(row["verification_method"], empty=True)
                string(row["team_id"], nullable=True, empty=True)
                if type(row["is_primary"]) is not bool:
                    raise SnapshotError("invalid_boolean")
                timestamp(row["created_at"])
                for field in ("verified_at", "updated_at"):
                    if row[field] is not None:
                        timestamp(row[field])
                key = (row["provider"], row["provider_user_id"], row["org_id"])
                if key in accounts:
                    raise SnapshotError("duplicate_account_tuple")
                accounts.add(key)
            elif table == "users":
                if type(row.get("is_shadow")) is not bool:
                    raise SnapshotError("invalid_boolean")
                for field in ("user_kind", "bot_kind"):
                    string(row.get(field), nullable=True)
                for field in ("created_at", "updated_at"):
                    if row.get(field) is not None:
                        timestamp(row[field])
            else:
                string(row.get("event_type"))
                string(row.get("actor_id"), nullable=True)
                timestamp(row.get("created_at"))
                details = row.get("details")
                if details is not None and not isinstance(details, dict):
                    raise SnapshotError("invalid_event_details")
                for key in DETAIL_FIELDS:
                    if details and key in details:
                        if key == "ownership_proven":
                            if details[key] is not None and type(details[key]) is not bool:
                                raise SnapshotError("invalid_boolean")
                        else:
                            string(details[key], nullable=True)


def deduplicate_audit_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    by_id: dict[str, dict[str, dict]] = {}
    for event in events:
        by_id.setdefault(event["id"], {})[digest(event)] = event
    clean, conflicts = [], []
    for event_id in sorted(by_id):
        versions = by_id[event_id]
        target = clean if len(versions) == 1 else conflicts
        target.extend(versions[key] for key in sorted(versions))
    return clean, conflicts


def compute_row_fingerprint(identity: dict) -> str:
    # Canonical JSON binds field boundaries and actual nulls without collisions.
    return digest({key: identity[key] for key in IDENTITY_FIELDS})


def event_user(event: dict) -> str | None:
    details = event.get("details") or {}
    if event["event_type"] == "shadow_user_created":
        return details.get("shadow_user_id")
    # Current identity_linked and magic_link_consumed bind consumer in actor_id.
    # Unlink/issue may name an actor rather than holder; never infer proof.
    return event.get("actor_id")


def _account_matches(identity: dict, event: dict) -> bool:
    details = event.get("details") or {}
    return (
        event.get("org_id") == identity["org_id"]
        and event.get("event_type") in EVENT_TYPES
        and details.get("provider") == identity["provider"]
        and details.get("provider_user_id") == identity["provider_user_id"]
    )


def _find_matching_events(identity: dict, events: list[dict]) -> list[dict]:
    return [e for e in events if _account_matches(identity, e) and event_user(e) == identity["user_id"]]


def classify_identity(identity: dict, user: dict | None, events: list[dict], conflicts: list[dict]) -> dict:
    flags, reasons, evidence = set(), set(), set()
    method = identity["verification_method"]
    current_label = "recorded_proven" if method in PROVEN_LABELS else "recorded_unproven" if method in UNPROVEN_LABELS else "unknown"
    if user is None:
        flags.add("missing_user")
    elif user["org_id"] != identity["org_id"] or user["id"] != identity["user_id"]:
        flags.add("user_identity_mismatch")
    if user and user.get("is_shadow") and method == "admin_manual":
        flags.add("manual_on_still_shadow_requires_review")
    related = [e for e in events if _account_matches(identity, e)]
    if any(event_user(e) != identity["user_id"] for e in related):
        flags.add("account_event_different_or_missing_user")
    if any(_account_matches(identity, e) for e in conflicts):
        flags.add("conflicting_audit_event")
    matched = _find_matching_events(identity, events)
    lifecycle = []
    for event in matched:
        evidence.add(event["id"])
        if timestamp(event["created_at"]) < timestamp(identity["created_at"]):
            flags.add("event_before_current_row_possible_recreation")
        else:
            lifecycle.append(event)
    if matched:
        reasons.add("exact_tuple_events_are_candidates_not_origin_proof")
        reasons.add("audit_has_no_current_identity_lifecycle_binding")
    else:
        reasons.add("no_exact_tuple_candidate_evidence")
    if any(e["event_type"] == "shadow_user_created" for e in matched):
        reasons.add("historical_shadow_origin_candidate")
    links = [e for e in lifecycle if e["event_type"] == "identity_linked"]
    if len(links) > 1:
        flags.add("repeated_link_or_same_method_confirmation")
    if any(e["event_type"] == "identity_unlinked" for e in related):
        flags.add("unlink_or_recreation_requires_review")
    if any((e.get("details") or {}).get("ownership_proven") is True for e in links):
        reasons.add("recorded_ownership_claim_requires_trusted_lifecycle_review")
        if identity["verified_at"] is None:
            flags.add("null_verified_at_with_ownership_claim")
    if any(e["event_type"] == "magic_link_consumed" for e in matched):
        reasons.add("consumption_event_does_not_supply_delivery_or_row_proof")
    reasons.add("snapshot_does_not_establish_trusted_event_provenance")
    if current_label == "unknown":
        flags.add("unknown_or_empty_method")
    # Never overwrite a legitimate manual/OAuth/bot link or infer it is repaired.
    # No current export fields establish a trusted, current-row proof chain.
    return {
        "classification": "review_required" if flags or "historical_shadow_origin_candidate" in reasons else "insufficient_evidence",
        "recorded_trust_label": current_label,
        "reasons": sorted(reasons),
        "flags": sorted(flags),
        "evidence_ids": sorted(evidence),
        "authority_action": "none",
    }


def build_manifest(snapshot: dict, tenant: str, source_sha: str = REVIEWED_SOURCE_SHA) -> dict:
    validate_snapshot(snapshot, tenant)
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise SnapshotError("invalid_source_sha")
    # Row order and identical duplicate events do not change semantic hashes.
    events, conflicts = deduplicate_audit_events(snapshot["security_audit_logs"])
    normalized = {
        "user_identities": sorted(snapshot["user_identities"], key=lambda r: r["id"]),
        "users": sorted(snapshot["users"], key=lambda r: r["id"]),
        "security_audit_logs": sorted(events + conflicts, key=lambda r: (r["id"], digest(r))),
    }
    snapshot_hash = digest(normalized)
    users = {row["id"]: row for row in snapshot["users"]}
    entries = []
    # Index by exact tenant/provider/account to avoid rows*events scanning.
    event_index, conflict_index = {}, {}
    for values, index in ((events, event_index), (conflicts, conflict_index)):
        for event in values:
            details = event.get("details") or {}
            key = (event["org_id"], details.get("provider"), details.get("provider_user_id"))
            index.setdefault(key, []).append(event)
    for identity in normalized["user_identities"]:
        key = (identity["org_id"], identity["provider"], identity["provider_user_id"])
        result = classify_identity(identity, users.get(identity["user_id"]), event_index.get(key, []), conflict_index.get(key, []))
        entries.append(
            {
                "identity_id": identity["id"],
                **result,
                "row_state": {key: identity[key] for key in IDENTITY_FIELDS},
                "row_fingerprint": compute_row_fingerprint(identity),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha,
        "snapshot_hash": snapshot_hash,
        "tenant": tenant,
        "entries": entries,
        "total_identities": len(entries),
        "total_users": len(users),
        "total_audit_events": len(events),
        "conflicting_event_ids": sorted({e["id"] for e in conflicts}),
        "classification_summary": dict(sorted(Counter(e["classification"] for e in entries).items())),
        "has_unresolved": bool(entries or conflicts),
        "authority_action": "none",
        "live_acceptance": False,
    }
    # Includes evidence/snapshot/source/classification, not merely row hashes.
    manifest["manifest_hash"] = digest(manifest)
    return manifest


def snapshot_still_matches(expected: dict, current: dict, tenant: str) -> bool:
    """Offline comparison only, not a DB predicate executor or proof of safety.

    New evidence/user changes require re-review even with an identical identity.
    Deletion/recreation, method-preserving relink, and null transitions change
    the canonical snapshot. A future repair must lock/revalidate atomically.
    """
    fresh = build_manifest(current, tenant, expected["source_sha"])
    return fresh["snapshot_hash"] == expected["snapshot_hash"]


def format_redacted_summary(manifest: dict) -> str:
    # No tenant/source/input/output paths or arbitrary event values on stdout.
    return "\n".join(
        [
            "Identity provenance inventory (offline; no authority changes)",
            f"Identities: {manifest['total_identities']}",
            f"Conflicting event IDs: {len(manifest['conflicting_event_ids'])}",
            f"Manifest hash: {manifest['manifest_hash']}",
            "STATUS: UNRESOLVED" if manifest["has_unresolved"] else "STATUS: EMPTY — no identity rows; not live closure",
        ]
    )


def write_manifest(manifest: dict, path: str) -> None:
    content = canonical(manifest) + b"\n"
    if len(content) > MAX_OUTPUT_BYTES:
        raise SnapshotError("manifest_too_large")
    # Never truncate input, an existing public file, a symlink or another artifact.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline identity inventory; no authority changes")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--output")
    parser.add_argument("--source-sha", default=REVIEWED_SOURCE_SHA)
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot, args.tenant)
        manifest = build_manifest(snapshot, args.tenant, args.source_sha)
        if args.output:
            write_manifest(manifest, args.output)
    except (SnapshotError, ValueError, OSError, RecursionError, TypeError):
        print("ERROR: invalid snapshot, provenance metadata, or protected output; no authority changes", file=sys.stderr)
        return 2
    print(format_redacted_summary(manifest))
    if args.output:
        print("Private manifest created (0600).")
    return 1 if manifest["has_unresolved"] else 0


if __name__ == "__main__":
    sys.exit(main())
