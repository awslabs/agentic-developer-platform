"""Exact-binding authority retirement for an independently authorized campaign owner.

This store primitive does not authorize an HTTP caller or prove that a pod stopped.
Its receipt proves only that the recorded native execution can no longer act.
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.store import AuthorityStoreError

EXECUTION_FIELDS = (
    "invocation_id",
    "tenant_id",
    "current_attempt",
    "status",
    "current_credential_epoch",
    "min_acceptable_credential_epoch",
    "epoch_overlap_expires_at",
    "workload_binding",
    "pod_name",
    "envelope_digest",
    "parent_principal",
    "parent_grant_id",
    "parent_grant_epoch",
    "flow_id",
    "orchestration_node_id",
    "orchestration_node_attempt",
    "campaign_retirement_operation",
    "repo",
    "arrived_at",
    "persona",
    "source_revision",
    "definition_revision",
    "issue_number",
    "installation_id",
)
GRANT_FIELDS = (
    "grant_id",
    "principal",
    "tenant_id",
    "authority_kind",
    "authority_reference_id",
    "authority_human_id",
    "authority_org_id",
    "revoked",
    "revocation_epoch",
    "expires_at",
    "allowed_actions",
    "delegable_actions",
    "target_relationships",
    "target_run_ids",
    "repo_scope",
    "flow_id",
)
AUTHORITY_FIELDS = ("status", "human_id", "authority_kind", "expires_at")
BINDING_FIELDS = ("invocation_id", "tenant_id", "attempt")
REGISTRATION_FIELDS = ("invocation_id", "tenant_id", "attempt", "generation", "workload_binding", "address", "port", "registered_at")


class CampaignExecutionConflictError(BootstrapRefusedError):
    pass


def fingerprint(value):
    normalized = {
        category: {
            field: {kind: sorted(content) if kind in {"SS", "NS"} else content for kind, content in attribute.items()}
            for field, attribute in row.items()
        }
        for category, row in value.items()
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def active_fingerprint(binding):
    return fingerprint({**binding, "execution": {**binding["execution"], "status": {"S": "active"}}})


class CampaignExecutionRecovery:
    def __init__(self, store):
        self.store = store

    def owner(self, *, tenant, invocation):
        try:
            execution = self.store._read(f"TENANT#{tenant}", f"EXEC#{invocation}") or {}
            grant = self.store._read(f"TENANT#{tenant}", f"GRANT#{invocation}#{execution['current_attempt']['N']}") or {}
            return grant["authority_human_id"]["S"]
        except (KeyError, TypeError):
            raise CampaignExecutionConflictError("campaign execution owner unavailable") from None

    def _snapshot(self, tenant, invocation, human):
        try:
            execution = self.store._read(f"TENANT#{tenant}", f"EXEC#{invocation}") or {}
            attempt = execution["current_attempt"]["N"]
            grant = self.store._read(f"TENANT#{tenant}", f"GRANT#{invocation}#{attempt}") or {}
            authority = self.store._read(f"TENANT#{tenant}", f"AUTHORITY#{grant['authority_reference_id']['S']}") or {}
            binding = self.store._read(f"POD#{execution['workload_binding']['S']}", "BINDING") or {}
            registration = self.store._read(f"TENANT#{tenant}", f"REG#{invocation}#{attempt}") or {}
            if not (
                tenant
                and invocation
                and human
                and execution["invocation_id"] == {"S": invocation}
                and execution["tenant_id"] == {"S": tenant}
                and execution["status"]["S"] in {"active", "revoked", "completed", "cancelled"}
                and int(attempt) > 0
                and int(execution["current_credential_epoch"]["N"]) > 0
                and execution["workload_binding"]["S"]
                and execution["pod_name"]["S"]
                and execution["envelope_digest"]["S"]
                and grant["tenant_id"] == {"S": tenant}
                and grant["principal"] == {"S": f"{invocation}#{attempt}"}
                and grant["authority_org_id"] == {"S": tenant}
                and grant["authority_human_id"] == {"S": human}
                and grant["revoked"] == {"BOOL": False}
                and authority["human_id"] == {"S": human}
                and authority["authority_kind"] == grant["authority_kind"]
                and authority["status"] == {"S": "active"}
                and binding["tenant_id"] == {"S": tenant}
                and binding["invocation_id"] == {"S": invocation}
                and binding["attempt"] == {"N": attempt}
                and (
                    not registration
                    or (
                        registration["invocation_id"] == {"S": invocation}
                        and registration["tenant_id"] == {"S": tenant}
                        and registration["attempt"] == {"N": attempt}
                        and registration["workload_binding"] == execution["workload_binding"]
                        and int(registration["generation"]["N"]) > 0
                    )
                )
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise CampaignExecutionConflictError("campaign execution ownership or workload binding changed") from None
        rows = {"execution": execution, "grant": grant, "authority": authority, "binding": binding, "registration": registration}
        fields = {
            "execution": EXECUTION_FIELDS,
            "grant": GRANT_FIELDS,
            "authority": AUTHORITY_FIELDS,
            "binding": BINDING_FIELDS,
            "registration": REGISTRATION_FIELDS,
        }
        selected = {category: {field: row[field] for field in fields[category] if field in row} for category, row in rows.items()}
        return rows, selected, fingerprint(selected)

    def capture(self, *, tenant, invocation, human):
        rows, selected, version = self._snapshot(tenant, invocation, human)
        execution = rows["execution"]
        if execution["status"] != {"S": "active"} or "campaign_retirement_operation" in execution:
            raise CampaignExecutionConflictError("campaign execution is not active")
        self.store.live_grant(invocation_id=invocation, tenant_id=tenant, attempt=int(execution["current_attempt"]["N"]), now=datetime.now(UTC))
        return {"version": version, "binding": selected}

    def enroll(self, *, tenant, invocation, human, expected_version, operation_id, before_commit=None):
        operation_id = str(UUID(operation_id))
        captured = self.capture(tenant=tenant, invocation=invocation, human=human)
        if captured["version"] != expected_version or "dispatch" in captured["binding"]["grant"].get("allowed_actions", {}).get("SS", []):
            raise CampaignExecutionConflictError("campaign enrollment requires an unchanged bounded workload")
        request = {"tenant": tenant, "invocation": invocation, "human": human, "expected_version": expected_version, "operation_id": operation_id}
        receipt = {**captured, "request": request, "status": "prepared", "operation_id": operation_id}
        key = {"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": f"CAMPAIGN_ENROLL#{operation_id}"}}
        existing = self.store._read(key["pk"]["S"], key["sk"]["S"])
        if existing:
            if TypeDeserializer().deserialize(existing["receipt"]) != receipt:
                raise CampaignExecutionConflictError("campaign enrollment operation changed")
            return receipt
        rows, _, version = self._snapshot(tenant, invocation, human)
        if version != expected_version:
            raise CampaignExecutionConflictError("campaign enrollment binding changed")
        checks = [("execution", EXECUTION_FIELDS), ("grant", GRANT_FIELDS), ("authority", AUTHORITY_FIELDS), ("binding", BINDING_FIELDS)]
        if rows["registration"]:
            checks.append(("registration", REGISTRATION_FIELDS))
        if before_commit is not None:
            before_commit()
        try:
            self.store.client.transact_write_items(
                TransactItems=[
                    *[{"ConditionCheck": self._check(rows[category], fields)} for category, fields in checks],
                    *([] if rows["registration"] else [{"ConditionCheck": self._absent_registration(tenant, invocation, rows)}]),
                    {
                        "Put": {
                            "TableName": self.store.table,
                            "Item": {**key, "receipt": TypeSerializer().serialize(receipt)},
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except (ClientError, BotoCoreError):
            raise AuthorityStoreError("campaign enrollment outcome unavailable; reconcile the same operation") from None
        return receipt

    def stopping(self, *, tenant, invocation, human, expected_version, operation_id):
        rows, selected, version = self._snapshot(tenant, invocation, human)
        if "campaign_retirement_operation" in selected["execution"]:
            if selected["execution"]["campaign_retirement_operation"] != {"S": str(UUID(operation_id))}:
                raise CampaignExecutionConflictError("campaign retirement belongs to another operation")
            receipt = self.retire(tenant=tenant, invocation=invocation, human=human, expected_version=expected_version, operation_id=operation_id)
            return {"version": version, "binding": receipt["binding"]}
        if active_fingerprint(selected) != expected_version:
            raise CampaignExecutionConflictError("campaign execution changed before stop")
        if rows["execution"]["status"]["S"] not in {"active", "completed", "cancelled"}:
            raise CampaignExecutionConflictError("campaign execution was independently revoked")
        return {"version": version, "binding": selected}

    def exit_receipt(self, *, tenant, invocation, human, expected_version, operation_id, observation_scope):
        operation_id = str(UUID(operation_id))
        self.stopping(tenant=tenant, invocation=invocation, human=human, expected_version=expected_version, operation_id=operation_id)
        existing = self.store._read(f"TENANT#{tenant}", f"CAMPAIGN_EXIT#{operation_id}")
        if not existing:
            return None
        receipt = TypeDeserializer().deserialize(existing["receipt"])
        expected = {
            "tenant": tenant,
            "invocation": invocation,
            "human": human,
            "expected_version": expected_version,
            "operation_id": operation_id,
            "status": "workload_exited",
            "observation_scope": observation_scope,
        }
        if any(receipt.get(key) != value for key, value in expected.items()) or active_fingerprint(receipt["binding"]) != expected_version:
            raise CampaignExecutionConflictError("campaign exit receipt changed")
        return receipt

    def record_exit(self, *, tenant, invocation, human, expected_version, operation_id, observation_scope):
        operation_id = str(UUID(operation_id))
        captured = self.stopping(tenant=tenant, invocation=invocation, human=human, expected_version=expected_version, operation_id=operation_id)
        key = {"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": f"CAMPAIGN_EXIT#{operation_id}"}}
        receipt = {
            "tenant": tenant,
            "invocation": invocation,
            "human": human,
            "expected_version": expected_version,
            "operation_id": operation_id,
            "status": "workload_exited",
            "binding": captured["binding"],
            "observation_scope": observation_scope,
        }
        existing = self.exit_receipt(
            tenant=tenant,
            invocation=invocation,
            human=human,
            expected_version=expected_version,
            operation_id=operation_id,
            observation_scope=observation_scope,
        )
        if existing:
            return existing
        rows, _, version = self._snapshot(tenant, invocation, human)
        if version != captured["version"]:
            raise CampaignExecutionConflictError("campaign binding changed after exit observation")
        checks = [("execution", EXECUTION_FIELDS), ("grant", GRANT_FIELDS), ("authority", AUTHORITY_FIELDS), ("binding", BINDING_FIELDS)]
        if rows["registration"]:
            checks.append(("registration", REGISTRATION_FIELDS))
        try:
            self.store.client.transact_write_items(
                TransactItems=[
                    *[{"ConditionCheck": self._check(rows[category], fields)} for category, fields in checks],
                    *([] if rows["registration"] else [{"ConditionCheck": self._absent_registration(tenant, invocation, rows)}]),
                    {
                        "Put": {
                            "TableName": self.store.table,
                            "Item": {**key, "receipt": TypeSerializer().serialize(receipt)},
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except (ClientError, BotoCoreError):
            raise AuthorityStoreError("campaign exit receipt unavailable; reconcile the same operation") from None
        return receipt

    def _absent_registration(self, tenant, invocation, rows):
        return {
            "TableName": self.store.table,
            "Key": {"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": f"REG#{invocation}#{rows['execution']['current_attempt']['N']}"}},
            "ConditionExpression": "attribute_not_exists(pk)",
        }

    def _check(self, row, fields):
        names, values, conditions = {}, {}, []
        for index, field in enumerate(fields):
            name = f"#field{index}"
            names[name] = field
            if field in row:
                value = f":value{index}"
                conditions.append(f"{name} = {value}")
                values[value] = row[field]
            else:
                conditions.append(f"attribute_not_exists({name})")
        return {
            "TableName": self.store.table,
            "Key": {key: row[key] for key in ("pk", "sk")},
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }

    def retire(self, *, tenant, invocation, human, expected_version, operation_id, before_commit=None):
        operation_id = str(UUID(operation_id))
        request = {"tenant": tenant, "invocation": invocation, "human": human, "expected_version": expected_version, "operation_id": operation_id}
        key = {"pk": {"S": f"TENANT#{tenant}"}, "sk": {"S": f"CAMPAIGN_RETIRE#{operation_id}"}}
        existing = self.store._read(key["pk"]["S"], key["sk"]["S"])
        rows, selected, version = self._snapshot(tenant, invocation, human)
        if existing:
            receipt = TypeDeserializer().deserialize(existing["receipt"])
            if receipt["request"] != request or receipt["after_version"] != version:
                raise CampaignExecutionConflictError("campaign retirement receipt does not match the current execution")
            return receipt
        terminal = rows["execution"]["status"]["S"] in {"completed", "cancelled"}
        if terminal:
            exit_row = self.store._read(f"TENANT#{tenant}", f"CAMPAIGN_EXIT#{operation_id}")
            exit_receipt = TypeDeserializer().deserialize(exit_row["receipt"]) if exit_row else {}
            if (
                exit_receipt.get("expected_version") != expected_version
                or active_fingerprint(selected) != expected_version
                or active_fingerprint(exit_receipt.get("binding", {"execution": {}})) != expected_version
                or exit_receipt.get("human") != human
                or exit_receipt.get("tenant") != tenant
                or exit_receipt.get("invocation") != invocation
            ):
                raise CampaignExecutionConflictError("terminal campaign requires its exact workload exit receipt")
        elif version != expected_version or rows["execution"]["status"] != {"S": "active"}:
            raise CampaignExecutionConflictError("campaign execution changed before retirement")
        if "campaign_retirement_operation" in rows["execution"]:
            raise CampaignExecutionConflictError("campaign execution already has a retirement owner")
        retired_status = rows["execution"]["status"] if terminal else {"S": "revoked"}
        after = {**selected, "execution": {**selected["execution"], "status": retired_status, "campaign_retirement_operation": {"S": operation_id}}}
        receipt = {
            "request": request,
            "status": "authority_revoked",
            "after_version": fingerprint(after),
            "binding": selected,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        update = self._check(rows["execution"], EXECUTION_FIELDS)
        update["UpdateExpression"] = "SET #retired_status = :revoked, #retirement = :operation"
        update["ExpressionAttributeNames"].update({"#retired_status": "status", "#retirement": "campaign_retirement_operation"})
        update["ExpressionAttributeValues"].update({":revoked": retired_status, ":operation": {"S": operation_id}})
        transactions = [
            {"Update": update},
            {"ConditionCheck": self._check(rows["grant"], GRANT_FIELDS)},
            {"ConditionCheck": self._check(rows["authority"], AUTHORITY_FIELDS)},
            {"ConditionCheck": self._check(rows["binding"], BINDING_FIELDS)},
            {
                "Put": {
                    "TableName": self.store.table,
                    "Item": {**key, "receipt": TypeSerializer().serialize(receipt)},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        if rows["registration"]:
            transactions.append({"ConditionCheck": self._check(rows["registration"], REGISTRATION_FIELDS)})
        else:
            transactions.append({"ConditionCheck": self._absent_registration(tenant, invocation, rows)})
        if before_commit is not None:
            before_commit()
        try:
            self.store.client.transact_write_items(TransactItems=transactions)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise CampaignExecutionConflictError("campaign retirement raced a protected binding change") from None
            raise AuthorityStoreError("campaign retirement outcome unavailable; reconcile the operation receipt") from None
        except BotoCoreError:
            raise AuthorityStoreError("campaign retirement outcome unavailable; reconcile the operation receipt") from None
        return receipt
