"""Fresh human decision binds a new repair plan while keeping the paid admission."""

import json

from workspace_provisioning.artifacts import digest
from workspace_provisioning.partial_apply_artifact import repair_artifact

from app.services.operation_approvals import ApprovalDenied
from app.services.provider_recovery import ProviderRecoveryService


class PartialApplyRepairService(ProviderRecoveryService):
    lifecycle_phase = "apply-infrastructure"
    recovery_scope = "partial-apply-repair"
    recovery_endpoint = "/repair-partial-apply"
    recovery_description = (
        "Complete the original paid allocation using the separately prepared exact repair plan. "
        "Preserve the existing cluster and all retained resources except the explicitly identified unused, tainted EIP. "
        "Keep the original request, approval, budget reservation and attempt count; do not replay the old saved plan."
    )

    async def terms(self, workspace_id, artifact_id):
        _, operation = await self.original(workspace_id)
        return await self._terms(operation, artifact_id)

    async def owner_evidence_id(self, operation, artifact_id):
        async with self.composition.domain_connect() as domain:
            registered = await domain.fetchrow(
                "SELECT p.owner_evidence_json FROM workspace_lifecycle_artifacts a "
                "JOIN workspace_partial_apply_preparations p ON p.operation_id=a.source_operation_id "
                "WHERE a.artifact_id=$1 AND a.org_id=$2 AND a.workspace_id=$3 AND p.source_operation_id=$4",
                artifact_id,
                operation["org_id"],
                operation["workspace_id"],
                operation["operation_id"],
            )
        if registered is None:
            raise ApprovalDenied("registered repair artifact unavailable")
        return json.loads(registered["owner_evidence_json"])["record_id"]

    async def _terms(self, operation, artifact_id):
        selected = await self.owner_evidence_id(operation, artifact_id)
        proof = await self.composition.dispatcher.transport.post(
            "/partial-apply-observation",
            {
                "domain": "superplane",
                "org_id": operation["org_id"],
                "workspace_id": operation["workspace_id"],
                "operation_id": operation["operation_id"],
                "requester": operation["requester"],
                "expected_version": operation["version"],
                "owner_evidence_id": selected,
            },
            distinguish_denial=True,
        )
        evidence = proof["owner_evidence"]
        _, repair = await repair_artifact(
            self.composition.operation_connect,
            self.composition.domain_connect,
            source=operation,
            artifact_id=artifact_id,
            evidence=evidence,
        )
        return {
            "version": 1,
            "owner_evidence_id": evidence["record_id"],
            "owner_evidence_sha256": digest(evidence),
            "repair": repair,
        }

    async def request(
        self, workspace_id, *, expected_version, idempotency_key, artifact_id
    ):
        terms = await self.terms(workspace_id, artifact_id)
        return await super().request(
            workspace_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            review_terms=terms,
        )

    @classmethod
    def ticket(cls, row, original, subject):
        ticket = super().ticket(row, original, subject)
        ticket["repair_plan"] = json.loads(row.amendment_json)
        return ticket

    async def before_decision(self, session, row, result):
        if result == "allowed-once":
            terms = json.loads(row.amendment_json)
            # The base service has authenticated the selected approver. Read the
            # original requester from the admission, not the approver's identity.
            async with self.composition.operation_connect() as connection:
                operation = await connection.fetchrow(
                    "SELECT o.*,c.requester FROM harness_operations o "
                    "JOIN harness_approval_consumption c USING(operation_id) "
                    "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3",
                    row.operation_id,
                    row.org_id,
                    row.workspace_id,
                )
            if (
                operation is None
                or operation["requester"] != row.requester
                or operation["version"] != row.expected_version
                or operation["plan_digest"] != row.plan_digest
            ):
                raise ApprovalDenied("original partial apply admission changed")
            if terms != await self._terms(operation, terms["repair"]["artifact_id"]):
                raise ApprovalDenied("partial apply repair review changed")
