"""Human-owned recovery of an exited standalone bootstrap, without retrying work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.archived_worker_exit import ExitRecoveryRefusedError, ExitRecoveryUnavailableError
from src.agentauth.retained_abort_recovery import unreported_exit_transaction
from src.agentauth.store import AuthorityStoreError

UUID = r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$"
_FOREIGN_WORK = (
    "parent_principal",
    "parent_grant_id",
    "parent_grant_epoch",
    "dispatch_reservation_id",
    "orchestration_node_id",
    "orchestration_node_attempt",
    "orchestration_continuation_receipt",
    "orchestration_continuation_action",
    "orchestration_review_repairs",
    "orchestration_correction_receipt",
    "orchestration_amendment_request_id",
    "chat_user_turn",
    "chat_delivery",
    "chat_sandbox_creation",
    "domain_operation",
    "abort_command_id",
)
_FOREIGN_GRANT = ("launch_authority_reference_id", "launch_authority_human_id", "launch_authority_kind")


class ExitRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=UUID)
    expected_attempt: int = Field(strict=True, ge=1, le=1_000_000)
    expected_credential_epoch: int = Field(strict=True, ge=1, le=1_000_000)
    expected_workload_uid: str = Field(pattern=UUID)
    audit_id: str = Field(pattern=UUID)
    audit_at: str = Field(min_length=20, max_length=40)


@dataclass(frozen=True)
class RecoveryContext:
    raw: dict
    owner: str
    checks: list


class BootstrapExitRecovery:
    def __init__(self, *, store, events_table: str, reader):
        self.store, self.events_table, self.reader = store, events_table, reader

    def _check(self, raw: dict, fields: tuple[str, ...]) -> dict:
        return {
            "ConditionCheck": {
                "TableName": self.store.table,
                "Key": {key: raw[key] for key in ("pk", "sk")},
                "ConditionExpression": " AND ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
                "ExpressionAttributeNames": {f"#f{i}": key for i, key in enumerate(fields)},
                "ExpressionAttributeValues": {f":v{i}": raw[key] for i, key in enumerate(fields)},
            }
        }

    def context(self, *, tenant: str, invocation: str) -> RecoveryContext:
        """Historical ownership is not a live delegation or permission to restart.

        An expired grant may establish its original human owner; it grants no new
        worker action. Revoked/missing/foreign authority remains refused, and the
        route separately requires that human's current JWT and live membership.
        """
        try:
            pk = f"TENANT#{tenant}"
            raw = self.store._read(pk, f"EXEC#{invocation}") or {}
            attempt = raw["current_attempt"]["N"]
            epoch = raw["current_credential_epoch"]["N"]
            if not (
                raw.get("tenant_id") == {"S": tenant}
                and raw.get("invocation_id") == {"S": invocation}
                and raw.get("status", {}).get("S") in {"active", "completed"}
                and int(attempt) > 0
                and int(epoch) > 0
                and raw["workload_binding"]["S"]
                and raw["pod_name"]["S"]
                and raw["arrived_at"]["S"]
                and raw.get("chain_depth", {"N": "0"}) == {"N": "0"}
                and not any(key in raw for key in _FOREIGN_WORK)
            ):
                raise ValueError()
            grant = self.store._read(pk, f"GRANT#{invocation}#{attempt}") or {}
            owner = grant["authority_human_id"]["S"]
            authority = self.store._read(pk, f"AUTHORITY#{grant['authority_reference_id']['S']}") or {}
            binding = self.store._read(f"POD#{raw['workload_binding']['S']}", "BINDING") or {}
            if not (
                owner
                and grant.get("tenant_id") == {"S": tenant}
                and grant.get("principal") == {"S": f"{invocation}#{attempt}"}
                and grant.get("authority_org_id") == {"S": tenant}
                and grant.get("authority_kind") == {"S": "github_event"}
                and grant.get("revoked") == {"BOOL": False}
                and grant.get("revocation_epoch", {}).get("N")
                and not any(key in grant for key in _FOREIGN_GRANT)
                and authority.get("status") == {"S": "active"}
                and authority.get("human_id") == {"S": owner}
                and authority.get("authority_kind") == {"S": "github_event"}
                and binding.get("invocation_id") == {"S": invocation}
                and binding.get("tenant_id") == {"S": tenant}
                and binding.get("attempt") == {"N": attempt}
                and self.store._read(pk, f"REG#{invocation}#{attempt}") is None
            ):
                raise ValueError()
            checks = [
                self._check(
                    grant,
                    (
                        "principal",
                        "tenant_id",
                        "authority_human_id",
                        "authority_org_id",
                        "authority_kind",
                        "authority_reference_id",
                        "revoked",
                        "revocation_epoch",
                    ),
                ),
                self._check(authority, ("status", "human_id", "authority_kind")),
                self._check(binding, ("invocation_id", "tenant_id", "attempt")),
                {
                    "ConditionCheck": {
                        "TableName": self.store.table,
                        "Key": {"pk": {"S": pk}, "sk": {"S": f"REG#{invocation}#{attempt}"}},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
            checks[0]["ConditionCheck"]["ConditionExpression"] += "".join(f" AND attribute_not_exists({key})" for key in _FOREIGN_GRANT)
            return RecoveryContext(raw, owner, checks)
        except (KeyError, TypeError, ValueError):
            raise ExitRecoveryRefusedError("standalone bootstrap recovery unavailable") from None

    def receipt(self, *, tenant: str, invocation: str, attempt: int) -> dict | None:
        raw = self.store._read(f"TENANT#{tenant}", f"EXIT_RECOVERY#{invocation}#{attempt}")
        if raw is None:
            return None
        try:
            return {
                key: int(value["N"]) if "N" in value else TypeDeserializer().deserialize(value)
                for key, value in raw.items()
                if key not in {"pk", "sk"}
            }
        except (ValueError, TypeError):
            raise ExitRecoveryRefusedError("recovery receipt unavailable") from None

    def recover(
        self,
        *,
        tenant: str,
        invocation: str,
        actor: str,
        context: RecoveryContext,
        request: ExitRecoveryRequest,
        preview: bool = False,
        before_commit=None,
    ) -> dict:
        raw = context.raw
        if (
            raw.get("current_attempt") != {"N": str(request.expected_attempt)}
            or raw.get("current_credential_epoch") != {"N": str(request.expected_credential_epoch)}
            or raw.get("workload_binding") != {"S": request.expected_workload_uid}
        ):
            raise ExitRecoveryRefusedError("protected recovery identity changed")
        payload_digest = hashlib.sha256(
            json.dumps({"request": request.model_dump(), "actor": actor, "tenant": tenant, "invocation": invocation}, sort_keys=True).encode()
        ).hexdigest()
        existing = self.receipt(tenant=tenant, invocation=invocation, attempt=request.expected_attempt)
        if existing is not None:
            if existing.get("request_digest") != payload_digest or existing.get("operation_id") != request.operation_id:
                raise ExitRecoveryRefusedError("recovery operation conflicts with its durable receipt")
            if raw.get("status") != {"S": "completed"} or raw.get("terminal_outcome") != {"S": "failed"}:
                raise ExitRecoveryRefusedError("recovery receipt disagrees with protected outcome")
            return existing
        if raw.get("status") != {"S": "active"}:
            raise ExitRecoveryRefusedError("execution already has an outcome")
        evidence = self.reader.observe(
            audit_id=request.audit_id,
            audit_at=request.audit_at,
            name=raw["pod_name"]["S"],
            uid=request.expected_workload_uid,
            arrived_at=raw["arrived_at"]["S"],
        )
        result = {
            "status": "verified" if preview else "recovered",
            "run_id": invocation,
            "operation_id": request.operation_id,
            "attempt": request.expected_attempt,
            "credential_epoch": request.expected_credential_epoch,
            "workload_uid": request.expected_workload_uid,
            "registration_generation": None,
            "outcome": "failed",
            "audit_id": evidence.audit_id,
            "audit_event_id": evidence.event_id,
            "evidence_digest": evidence.digest,
            "image_digest": evidence.image_digest,
            "finished_at": evidence.finished_at,
            "exit_code": evidence.exit_code,
            "actor": actor,
            "request_digest": payload_digest,
        }
        if preview:
            return result
        if before_commit is not None:
            # The human route revalidates its session/membership after provider I/O.
            before_commit()
        try:
            transaction = unreported_exit_transaction(
                self.store,
                raw=raw,
                tenant=tenant,
                invocation=invocation,
                events_table=self.events_table,
                source=f"kubernetes-audit:{evidence.audit_id}",
            )
            # Preserve a genuine terminal report even if it appeared during the audit read.
            if not transaction or ":reason" not in transaction[0]["Update"]["ExpressionAttributeValues"]:
                raise ExitRecoveryRefusedError("execution already has a terminal report")
            update = transaction[1]["Update"]
            update["ConditionExpression"] += " AND current_credential_epoch = :epoch AND pod_name = :name AND arrived_at = :arrived"
            update["ConditionExpression"] += " AND (attribute_not_exists(chain_depth) OR chain_depth = :root_depth)"
            update["ConditionExpression"] += "".join(f" AND attribute_not_exists({key})" for key in _FOREIGN_WORK if key != "abort_command_id")
            update["ExpressionAttributeValues"].update(
                {":epoch": raw["current_credential_epoch"], ":name": raw["pod_name"], ":arrived": raw["arrived_at"], ":root_depth": {"N": "0"}}
            )
            result["recorded_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            item = {
                "pk": {"S": f"TENANT#{tenant}"},
                "sk": {"S": f"EXIT_RECOVERY#{invocation}#{request.expected_attempt}"},
                **{key: TypeSerializer().serialize(value) for key, value in result.items()},
            }
            transaction.extend(
                [*context.checks, {"Put": {"TableName": self.store.table, "Item": item, "ConditionExpression": "attribute_not_exists(pk)"}}]
            )
            self.store.client.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError) as exc:
            committed = self.receipt(tenant=tenant, invocation=invocation, attempt=request.expected_attempt)
            if committed and committed.get("request_digest") == payload_digest:
                return committed
            if isinstance(exc, ClientError) and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise ExitRecoveryRefusedError("recovery identity or terminal report changed") from None
            raise ExitRecoveryUnavailableError("recovery outcome unavailable; reconcile the original operation") from None
        except AuthorityStoreError:
            raise ExitRecoveryUnavailableError("recovery outcome unavailable; reconcile the original operation") from None
        return result
