"""Read-only ownership inventory and conditional artifact-catalog backfill.

Legacy records with missing ownership remain quarantined. Only an artifact with
explicit catalog ownership corroborated by its key and complete session header
can have its ownership aliases annotated. Every quarantined or conflicting record
can be reported durably by key and reason; private values are never emitted.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator
from typing import Any

from botocore.exceptions import ClientError

_SEGMENT = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_SESSION = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CANONICAL = ("orgId", "tenantId", "teamId", "ownerUserId")
_LEGACY = ("org_id", "team_id", "user_id")
Report = Callable[[dict[str, Any]], None]


def _safe_segment(value: Any) -> bool:
    return isinstance(value, str) and bool(_SEGMENT.fullmatch(value)) and ".." not in value and value != "."


def _safe_session(value: Any) -> bool:
    return _safe_segment(value) and bool(_SESSION.fullmatch(value)) and value not in {"o", "t", "u", "s"}


def _pages(table: Any, page_size: int | None = None) -> Iterator[dict]:
    """Yield rows page by page; a failed page raises instead of ending the scan early."""
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"ExclusiveStartKey": cursor} if cursor else {}
        if page_size:
            kwargs["Limit"] = page_size
        page = table.scan(**kwargs)
        yield from page.get("Items", [])
        cursor = page.get("LastEvaluatedKey")
        if not cursor:
            break


class _Lookup:
    """Bounded, lazily populated view of corroborating rows; never materialises a table."""

    def __init__(self, table: Any, key: Callable[[str], dict], limit: int = 1024):
        self.table, self.key, self.limit = table, key, limit
        self.cache: OrderedDict[Any, dict | None] = OrderedDict()

    def get(self, reference: Any) -> dict | None:
        if reference in self.cache:
            self.cache.move_to_end(reference)
            return self.cache[reference]
        row = None
        if isinstance(reference, str):
            row = self.table.get_item(Key=self.key(reference), ConsistentRead=True).get("Item")
        self.cache[reference] = row
        if len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        return row


def _header_reason(header: dict | None) -> str | None:
    if not header or header.get("SK") != "header":
        return "session_header_missing"
    pk = header.get("PK")
    if not isinstance(pk, str) or not pk.startswith("session#") or not _safe_session(pk.removeprefix("session#")):
        return "session_key_invalid"
    org, team, user = (header.get(field) for field in ("orgId", "teamId", "ownerUserId"))
    if not all(_safe_segment(value) for value in (org, user)) or not (team == "" or _safe_segment(team)):
        return "header_owner_missing_or_invalid"
    if header.get("tenantId") != org or not _matches_owner_fields(header, (org, team, user)):
        return "header_owner_conflict"
    return None


def _owner(header: dict | None) -> tuple[str, str, str] | None:
    if header is None or _header_reason(header) is not None:
        return None
    return header["orgId"], header["teamId"], header["ownerUserId"]


def _owner_fields(owner: tuple[str, str, str]) -> dict[str, str]:
    org, team, user = owner
    return {
        "orgId": org,
        "tenantId": org,
        "teamId": team,
        "ownerUserId": user,
        "org_id": org,
        "tenant_id": org,
        "team_id": team,
        "user_id": user,
        "owner_user_id": user,
    }


def _matches_owner_fields(row: dict, owner: tuple[str, str, str]) -> bool:
    return all(row.get(field) in (None, expected) for field, expected in _owner_fields(owner).items())


def _owns_context_row(row: dict, owner: tuple[str, str, str]) -> bool:
    """A session partition or replacement header cannot adopt legacy children."""
    fields = _owner_fields(owner)
    return all(row.get(field) == fields[field] for field in _CANONICAL) and _matches_owner_fields(row, owner)


def _child_reason(row: dict, header: dict | None) -> str | None:
    header_owner = _owner(header)
    if header_owner is None:
        return "session_header_unresolved"
    if _owns_context_row(row, header_owner):
        return None
    return "child_owner_conflict" if _conflicts(row, header_owner) else "child_owner_missing"


def _conflicts(row: dict, owner: tuple[str, str, str]) -> bool:
    """A present canonical or alias field names someone else; absence alone is merely missing."""
    return any(row.get(field) not in (None, expected) for field, expected in _owner_fields(owner).items())


def _memory_owner(row: dict | None) -> tuple[str, str, str] | None:
    if not row or row.get("SK") != "record":
        return None
    reference = row.get("id")
    if not isinstance(reference, str) or not re.fullmatch(r"mem_[a-f0-9]{32}", reference) or row.get("PK") != f"memory#{reference}":
        return None
    owner = tuple(row.get(field) for field in ("tenantId", "teamId", "ownerUserId"))
    if not all(isinstance(value, str) for value in owner) or not owner[0] or not owner[2] or not _owns_context_row(row, owner):
        return None
    return owner if row.get("scope") == {"tenant": owner[0], "user": owner[2]} else None


def _memory_reason(row: dict, records: Any) -> str | None:
    if row.get("SK") == "record":
        return None if _memory_owner(row) is not None else "memory_record_invalid"
    sort_key = row.get("SK", "")
    if not isinstance(sort_key, str):
        return "sort_key_invalid"
    if sort_key.startswith("mem#"):
        reference = row.get("id")
    elif re.fullmatch(r"write#[a-f0-9]{64}", sort_key) and isinstance(row.get("result"), dict):
        reference = row["result"].get("memory_id")
    else:
        return "memory_row_unrecognized"
    if not isinstance(reference, str):
        return "memory_index_unreferenced"
    record = records.get(reference)
    owner = _memory_owner(record)
    if owner is None:
        return "memory_index_orphaned"
    if not _owns_context_row(row, owner) or row.get("PK") != f"memory-owner#{owner[0]}#{owner[2]}":
        return "memory_index_conflict"
    if sort_key.startswith("mem#") and sort_key != f"mem#{(record or {}).get('createdAt')}#{reference}":
        return "memory_index_conflict"
    return None


def _owned_memory_row(row: dict, records: Any) -> bool:
    return _memory_reason(row, records) is None


def _artifact_reason(row: dict, headers: Any) -> str | None:
    pk = row.get("PK")
    if not isinstance(pk, str) or not pk.startswith("session#"):
        return "session_key_invalid"
    session = pk.removeprefix("session#")
    if not _safe_session(session):
        return "session_key_invalid"
    owner = _owner(headers.get(pk))
    if owner is None:
        return "session_header_unresolved"
    org, team, user = owner
    if not _owns_context_row(row, owner):
        if team == "" or any(row.get(field) != expected for field, expected in zip(_LEGACY, owner, strict=True)):
            return "catalog_owner_conflict" if _conflicts(row, owner) else "catalog_owner_missing"
    key = row.get("s3Key")
    prefix = f"o/{org}/t/{team or '~personal'}/u/{user}/s/{session}/"
    if not isinstance(key, str) or not key.startswith(prefix) or ".." in key or "\\" in key:
        return "object_key_mismatch"
    if not _matches_owner_fields(row, owner):
        return "catalog_alias_conflict"
    return None


def _artifact_owner(row: dict, headers: Any) -> tuple[str, str, str] | None:
    if _artifact_reason(row, headers) is not None:
        return None
    return _owner(headers.get(row["PK"]))


def _backfill(context: Any, artifacts: Any, row: dict, owner: tuple[str, str, str]) -> None:
    fields = _owner_fields(owner)
    names = {f"#{field}": field for field in fields}
    values: dict[str, Any] = {f":{field}": expected for field, expected in fields.items()}
    values[":null"] = None
    compatible = {field: f"(attribute_not_exists(#{field}) OR #{field} = :null OR #{field} = :{field})" for field in fields}
    required = set(_CANONICAL)
    owner_condition = " AND ".join(f"#{field} = :{field}" if field in required else compatible[field] for field in fields)
    artifacts.meta.client.transact_write_items(
        TransactItems=[
            {
                "ConditionCheck": {
                    "TableName": context.name,
                    "Key": {"PK": row["PK"], "SK": "header"},
                    "ConditionExpression": owner_condition,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "Update": {
                    "TableName": artifacts.name,
                    "Key": {"PK": row["PK"], "SK": row["SK"]},
                    "UpdateExpression": "SET #org_id = :org_id, #team_id = :team_id, #user_id = :user_id",
                    "ConditionExpression": "#s3Key = :path AND " + owner_condition,
                    "ExpressionAttributeNames": {**names, "#s3Key": "s3Key"},
                    "ExpressionAttributeValues": {**values, ":path": row["s3Key"]},
                }
            },
        ]
    )


def _record(report: Report | None, table: str, row: dict, reason: str, category: str = "quarantined") -> None:
    if report is None:
        return
    key = {name: value if isinstance(value, str) else repr(value) for name, value in ((field, row.get(field)) for field in ("PK", "SK"))}
    report({"table": table, "key": key, "reason": reason, "category": category})


def inventory(
    context: Any, artifacts: Any, memory: Any, *, apply: bool = False, report: Report | None = None, page_size: int | None = None
) -> dict[str, dict[str, int]]:
    """Count every scanned row, fail on partial scans, and never log private values.

    Tables are processed one scan page at a time; corroborating headers and memory
    records are looked up through a bounded cache rather than materialised.
    ``apply`` annotates only independently corroborated artifact catalog rows, and
    only after the catalog scan has completed in full, so a failed page never leaves
    a partially backfilled catalog. Context or memory rows with missing/conflicting
    provenance are not rewritten. A transaction verifies current session and
    catalog ownership together with the backfill; a lost race is counted separately
    as a conflict for operator reconciliation. ``report`` receives one entry per
    quarantined or conflicting record (table, key, reason, category). Category
    counts reconcile: total = owned + quarantined + backfill_candidates, and under
    ``apply`` backfill_candidates = backfilled + conflicts.
    Apply requires ConditionCheckItem on context and UpdateItem on artifacts;
    both tables must be in the same account and region.
    """
    counts: dict[str, Counter] = {name: Counter() for name in ("context", "artifacts", "memory")}
    headers = _Lookup(context, lambda pk: {"PK": pk, "SK": "header"})
    for row in _pages(context, page_size):
        counts["context"]["total"] += 1
        if row.get("SK") == "header":
            reason = _header_reason(row)
        else:
            reason = _child_reason(row, headers.get(row.get("PK")))
        if reason is None:
            counts["context"]["owned"] += 1
        else:
            counts["context"]["quarantined"] += 1
            _record(report, "context", row, reason)

    candidates: list[tuple[dict, tuple[str, str, str]]] = []
    for row in _pages(artifacts, page_size):
        counts["artifacts"]["total"] += 1
        if not isinstance(row.get("SK"), str):
            counts["artifacts"]["quarantined"] += 1
            _record(report, "artifacts", row, "sort_key_invalid")
            continue
        reason = _artifact_reason(row, headers)
        if reason is not None:
            counts["artifacts"]["quarantined"] += 1
            _record(report, "artifacts", row, reason)
            continue
        if all(row.get(field) is not None for field in _LEGACY):
            counts["artifacts"]["owned"] += 1
            continue
        counts["artifacts"]["backfill_candidates"] += 1
        owner = _owner(headers.get(row["PK"]))
        if apply and owner is not None:
            candidates.append(({"PK": row["PK"], "SK": row["SK"], "s3Key": row["s3Key"]}, owner))
    for row, owner in candidates:
        try:
            _backfill(context, artifacts, row, owner)
        except ClientError as exc:
            reasons = {reason.get("Code") for reason in exc.response.get("CancellationReasons", [])}
            if (
                exc.response.get("Error", {}).get("Code") != "TransactionCanceledException"
                or "ConditionalCheckFailed" not in reasons
                or not reasons <= {"None", "ConditionalCheckFailed"}
            ):
                raise
            counts["artifacts"]["conflicts"] += 1
            _record(report, "artifacts", row, "backfill_conflict", category="conflict")
        else:
            counts["artifacts"]["backfilled"] += 1

    records = _Lookup(memory, lambda reference: {"PK": f"memory#{reference}", "SK": "record"})
    for row in _pages(memory, page_size):
        counts["memory"]["total"] += 1
        reason = _memory_reason(row, records)
        if reason is None:
            counts["memory"]["owned"] += 1
        else:
            counts["memory"]["quarantined"] += 1
            _record(report, "memory", row, reason)
    return {name: dict(counter) for name, counter in counts.items()}
