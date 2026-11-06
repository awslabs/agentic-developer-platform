"""Read existing test authority and journal admissions; never enroll or reset it.

This preparatory store deliberately has no authority/accumulator creation API.
The table/permissions and scoped native identity issuer still need adoption by
the original tools-accounting assignment before any route can use it.
"""

from __future__ import annotations

import hashlib
import json

from botocore.exceptions import BotoCoreError, ClientError


class AdmissionRefusedError(Exception):
    """No provider/setup action may follow this result."""


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class TestAdmissionStore:
    """Conditional Dynamo journal with exact authority-document guards.

    All methods are synchronous; the service calls them in the thread pool.
    Neither preparing-owner takeover nor dispatch replay is supported. An
    ambiguous write leaves the original operation for explicit reconciliation.
    """

    __test__ = False

    def __init__(self, *, table_name: str, client):
        if not table_name:
            raise AdmissionRefusedError("test_authority_unavailable")
        self.table, self.client = table_name, client

    @staticmethod
    def authority_key(tenant_id: str, run_id: str) -> dict:
        return {"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"TOOLS_TEST_RUN#{run_id}"}}

    @staticmethod
    def operation_key(tenant_id: str, run_id: str, operation_id: str) -> dict:
        return {"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"TOOLS_TEST_OP#{run_id}#{operation_id}"}}

    def _read(self, key: dict) -> dict | None:
        try:
            return self.client.get_item(TableName=self.table, Key=key, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError):
            raise AdmissionRefusedError("test_authority_unavailable") from None

    def authority(self, tenant_id: str, run_id: str) -> dict:
        row = self._read(self.authority_key(tenant_id, run_id))
        try:
            document = json.loads(row["document"]["S"])
            if (
                row["kind"]["S"] != "tools_test_run_v1"
                or document["tenant_id"] != tenant_id
                or document["run_id"] != run_id
                or row["dispatch_expires_at"]["N"] != str(document["dispatch_expires_at"])
                or canonical(document) != row["document"]["S"]
            ):
                raise ValueError
            return document
        except (KeyError, TypeError, ValueError):
            raise AdmissionRefusedError("test_authority_unavailable") from None

    def _guard(self, authority: dict, now: int | float) -> dict:
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": self.authority_key(authority["tenant_id"], authority["run_id"]),
                "ConditionExpression": "document = :document AND dispatch_expires_at > :now AND kind = :kind",
                "ExpressionAttributeValues": {
                    ":document": {"S": canonical(authority)},
                    ":now": {"N": str(now)},
                    ":kind": {"S": "tools_test_run_v1"},
                },
            }
        }

    def authority_guards(self, authority: dict, now: int | float) -> list[dict]:
        """Allow a protected issuer to fence revocation in the same dispatch CAS."""
        return [self._guard(authority, now)]

    def read_operation(self, tenant_id: str, run_id: str, operation_id: str) -> dict | None:
        row = self._read(self.operation_key(tenant_id, run_id, operation_id))
        if row is None:
            return None
        try:
            document = json.loads(row["document"]["S"])
            if (
                document["tenant_id"] != tenant_id
                or document["run_id"] != run_id
                or document["operation_id"] != operation_id
                or row["version"]["N"] != str(document["version"])
                or row["binding_digest"]["S"] != document["binding_digest"]
                or row["owner_token"]["S"] != document["owner_token"]
                or row["status"]["S"] != document["status"]
                or document["binding_digest"] != digest(document["binding"])
            ):
                raise ValueError
            return document
        except (KeyError, TypeError, ValueError):
            raise AdmissionRefusedError("test_operation_unavailable") from None

    def _item(self, operation: dict) -> dict:
        return {
            **self.operation_key(operation["tenant_id"], operation["run_id"], operation["operation_id"]),
            "document": {"S": canonical(operation)},
            "version": {"N": str(operation["version"])},
            "binding_digest": {"S": operation["binding_digest"]},
            "owner_token": {"S": operation["owner_token"]},
            "status": {"S": operation["status"]},
        }

    def claim(self, authority: dict, operation: dict, now: int, *, claim_items=()) -> tuple[dict, bool]:
        try:
            self.client.transact_write_items(
                TransactItems=[
                    *self.authority_guards(authority, now),
                    *claim_items,
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": self._item(operation),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
            return operation, True
        except (ClientError, BotoCoreError):
            # Even if a successful write's response was lost, it is a replay.
            # Never give a second caller ownership of a possible preparation.
            existing = self.read_operation(operation["tenant_id"], operation["run_id"], operation["operation_id"])
            if existing is not None:
                if existing["binding_digest"] != operation["binding_digest"]:
                    raise AdmissionRefusedError("test_operation_conflict") from None
                return existing, False
            raise AdmissionRefusedError("test_operation_claim_unavailable") from None

    def transition(self, authority: dict, operation: dict, *, status: str, now: int | float, quote_guards=(), **updates) -> dict:
        allowed = {
            ("preparing", "quoted"),
            ("quoted", "admitted"),
            ("admitted", "dispatch_started"),
            ("preparing", "unknown"),
            ("quoted", "unknown"),
            ("admitted", "unknown"),
            ("dispatch_started", "unknown"),
        }
        if (operation["status"], status) not in allowed or set(updates) - {"quote", "target_keys", "targets"}:
            raise AdmissionRefusedError("test_operation_transition_refused")
        updated = {**operation, **updates, "status": status, "version": operation["version"] + 1}
        if status == "admitted":
            updated["reservation_confirmed"] = True
        items = [] if status == "unknown" else self._distinct_guards([*self.authority_guards(authority, now), *quote_guards])
        items.append(
            {
                "Put": {
                    "TableName": self.table,
                    "Item": self._item(updated),
                    "ConditionExpression": (
                        "#version = :version AND owner_token = :owner AND binding_digest = :binding AND #status = :status AND document = :document"
                    ),
                    "ExpressionAttributeNames": {"#version": "version", "#status": "status"},
                    "ExpressionAttributeValues": {
                        ":version": {"N": str(operation["version"])},
                        ":owner": {"S": operation["owner_token"]},
                        ":binding": {"S": operation["binding_digest"]},
                        ":status": {"S": operation["status"]},
                        ":document": {"S": canonical(operation)},
                    },
                }
            }
        )
        try:
            self.client.transact_write_items(TransactItems=items)
            return updated
        except (ClientError, BotoCoreError):
            raise AdmissionRefusedError("test_operation_transition_unconfirmed") from None

    @staticmethod
    def _distinct_guards(guards):
        # All-in authority and the exact quote may protect the same immutable
        # contract. Dynamo forbids two operations on one key in a transaction.
        # Deduplicate only identical conditions; never weaken conflicting ones.
        result, seen = [], {}
        for guard in guards:
            try:
                condition = guard["ConditionCheck"]
                key = digest({"table": condition["TableName"], "key": condition["Key"]})
                if key in seen:
                    if seen[key] != guard:
                        raise ValueError
                    continue
                seen[key] = guard
                result.append(guard)
            except (KeyError, TypeError, ValueError):
                raise AdmissionRefusedError("test_operation_guard_conflict") from None
        return result
