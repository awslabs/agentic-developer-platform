"""Fresh decisions for the original planning call rejected before credentials."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from harness_jobs.provider_retry import active_call_rows

from app.services.dispatch_recovery import DispatchRecoveryService
from app.services.operation_approvals import ApprovalDenied


class ProviderRecoveryService(DispatchRecoveryService):
    recovery_scope = "uncredentialed-planning"
    recoverable_state = "running"
    recovery_endpoint = "/recover-uncredentialed"
    recovery_description = (
        "One retry of the same initial infrastructure planning operation, only after "
        "the credential owner proves every original execution grant expired and no "
        "AWS session was delivered. Original intent, approvals and budget are retained."
    )

    async def request(
        self, workspace_id, *, expected_version, idempotency_key, review_terms=None
    ):
        _, operation = await self.original(workspace_id)
        async with self.composition.operation_connect() as connection:
            lease = await connection.fetchrow(
                "SELECT * FROM harness_operation_leases WHERE operation_id=$1",
                operation["operation_id"],
            )
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                operation["operation_id"],
            )
            if self.recovery_scope in {
                "unstarted-planning",
                "interrupted-planning",
                "planning-budget",
                "saved-plan-completion",
                "unstarted-bootstrap",
            }:
                calls = await active_call_rows(
                    connection, operation["operation_id"], calls
                )
        if (
            lease is None
            or lease["closed_at"] is not None
            or lease["runtime_deadline"] is None
            or lease["runtime_deadline"] > datetime.now(UTC)
            or len(calls) != 1
            or calls[0]["stage"] != "intended"
            or calls[0]["provider_ref"] is not None
        ):
            raise ApprovalDenied(
                "original planning attempt is not eligible for credential recovery"
            )
        return await super().request(
            workspace_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            review_terms=review_terms,
        )


class UnstartedPlanningRecoveryService(ProviderRecoveryService):
    recovery_scope = "unstarted-planning"
    recovery_endpoint = "/recover-unstarted-planning"
    recovery_description = (
        "Retry the unchanged initial infrastructure plan only after the protected "
        "installation owner proves planning did not start on the original retained "
        "volume and the credential owner proves all prior grants and issued AWS "
        "sessions expired. Credential issuance, intents, approvals, budget and "
        "attempt counts are retained. A separately authorized, protected owner record "
        "may add at most five attempts, with a total ceiling of ten. Pre-init files "
        "are preserved under the recovery receipt. This decision does not approve infrastructure apply."
    )

    async def request(self, workspace_id, *, expected_version, idempotency_key):
        from app.services.planning_budget_confirmation import confirmation_terms

        _, operation = await self.original(workspace_id)
        binding = {**operation, "original_approval_id": operation["approval_id"]}
        async with self.sessions() as session:
            terms = await confirmation_terms(session, binding)
        return await super().request(
            workspace_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            review_terms=terms,
        )

    @classmethod
    def ticket(cls, row, original, subject):
        ticket = super().ticket(row, original, subject)
        if row.amendment_json is not None:
            terms = json.loads(row.amendment_json)
            ticket["planning_budget_confirmation"] = {
                **terms,
                "daily_usd": str(Decimal(terms["daily_micros"]) / 1000000),
                "operation_id": row.operation_id,
                "original_plan_digest": row.plan_digest,
                "description": "Confirm this unchanged daily cap for this recovery only. "
                "The original budget decision is retained; exact-plan apply approval is separate.",
            }
        return ticket

    async def before_decision(self, session, row, result):
        from app.services.planning_budget_confirmation import confirm_decision

        await confirm_decision(self, session, row, result)


class InterruptedPlanningRecoveryService(UnstartedPlanningRecoveryService):
    recovery_scope = "interrupted-planning"
    recovery_endpoint = "/recover-interrupted-planning"
    recovery_description = (
        "Retry the original initial infrastructure plan after it was interrupted. "
        "The owner must verify the read-only recipe, stopped source, retained files "
        "and empty versioned backend. All original grants and sessions must expire. "
        "This decision permits releasing only the recorded abandoned plan lock "
        "and preserves the original files, workspace, budget and attempt counts. "
        "Applying the resulting plan requires a separate exact-plan approval."
    )


class SavedPlanCompletionService(UnstartedPlanningRecoveryService):
    recovery_scope = "saved-plan-completion"
    recovery_endpoint = "/complete-saved-plan"
    recovery_description = (
        "Finish validation and publication of the original saved initial plan only. "
        "The protected owner must verify its original source, pod, volume and exact "
        "artifact bytes, and all source grants and sessions must have expired. "
        "No Terraform plan or apply is replayed. Original files, approvals, history, "
        "budget and attempt counts are retained; no attempt extension is accepted. "
        "Infrastructure apply requires a separate exact-plan approval."
    )


class UnstartedBootstrapRecoveryService(ProviderRecoveryService):
    lifecycle_phase = "bootstrap-workspace"
    recovery_scope = "unstarted-bootstrap"
    recovery_endpoint = "/recover-unstarted-bootstrap"
    recovery_description = (
        "Retry the original bootstrap only after the protected installation owner "
        "qualifies its stopped source and pod, all original grants and AWS sessions "
        "expire, and Gateway verifies no bootstrap mutation journals or reservation "
        "exist. Preserve the existing cluster, apply, operation, approval, budget, "
        "attempt count and original files. This decision permits no Terraform replay "
        "or attempt extension. An explicit runtime amendment can authorize this "
        "recovery for up to three hours, including dispatch, setup and cleanup."
    )

    async def request(
        self,
        workspace_id,
        *,
        expected_version,
        idempotency_key,
        max_runtime_seconds=None,
    ):
        from app.models.operation_approval import OperationApproval

        terms = None
        if max_runtime_seconds is not None:
            _, operation = await self.original(workspace_id)
            async with self.sessions() as session:
                original = await session.get(
                    OperationApproval, operation["approval_id"]
                )
                if original is None:
                    raise ApprovalDenied("original bootstrap approval unavailable")
                terms = {
                    "version": 1,
                    "kind": "bootstrap-runtime-extension-v1",
                    "original_max_runtime_seconds": original.max_runtime_seconds,
                    "max_runtime_seconds": max_runtime_seconds,
                }
                self.authority_lifetime_seconds(original, terms)
        return await super().request(
            workspace_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            review_terms=terms,
        )

    @classmethod
    def authority_lifetime_seconds(cls, original, review_terms):
        from harness_jobs.bootstrap_runtime_authorization import runtime_amendment
        from harness_jobs.identity import OperationRefused

        if review_terms is None:
            return super().authority_lifetime_seconds(original, review_terms)
        try:
            return runtime_amendment(review_terms, original.max_runtime_seconds)
        except OperationRefused:
            raise ApprovalDenied("bootstrap runtime amendment refused") from None

    @classmethod
    def ticket(cls, row, original, subject):
        ticket = super().ticket(row, original, subject)
        if row.amendment_json is not None:
            try:
                terms = json.loads(row.amendment_json)
            except (ValueError, TypeError):
                raise ApprovalDenied("bootstrap runtime amendment invalid") from None
            ticket["effective_max_runtime_seconds"] = cls.authority_lifetime_seconds(
                original, terms
            )
            ticket["runtime_amendment"] = terms
        return ticket

    async def before_decision(self, session, row, result):
        from app.models.operation_approval import OperationApproval

        if row.amendment_json is not None:
            original = await session.get(OperationApproval, row.original_approval_id)
            if original is None:
                raise ApprovalDenied("original bootstrap approval unavailable")
            try:
                terms = json.loads(row.amendment_json)
            except (ValueError, TypeError):
                raise ApprovalDenied("bootstrap runtime amendment invalid") from None
            self.authority_lifetime_seconds(original, terms)


class SettledBootstrapRecoveryService(UnstartedBootstrapRecoveryService):
    """A separately reviewed closure, within the existing bootstrap receipt scope."""

    recoverable_state = "unknown"
    recovery_endpoint = "/recover-settled-bootstrap"
    recovery_description = (
        "Continue this exact bootstrap after its recorded unknown recovery settlement. "
        "The owner must qualify the stopped source, wait for every original grant and "
        "session to expire, and positively revoke the recorded acquisition grants. "
        "The original closure, intent, completed apply and namespace are retained. "
        "This decision authorizes one new execution fence with the reviewed runtime."
    )

    async def request(
        self,
        workspace_id,
        *,
        expected_version,
        idempotency_key,
        max_runtime_seconds=None,
    ):
        from harness_jobs.bootstrap_runtime_authorization import (
            MAX_BOOTSTRAP_RUNTIME_SECONDS,
            SETTLED_REAUTHORIZATION_KIND,
        )
        from harness_jobs.settled_bootstrap import read, review_terms

        from app.models.operation_approval import OperationApproval

        _, operation = await self.original(workspace_id)
        async with self.composition.operation_connect() as connection:
            lease = await connection.fetchrow(
                "SELECT * FROM harness_operation_leases WHERE operation_id=$1",
                operation["operation_id"],
            )
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                operation["operation_id"],
            )
            calls = await active_call_rows(connection, operation["operation_id"], calls)
            if len(calls) != 1:
                raise ApprovalDenied(
                    "settled bootstrap requires its unique active call"
                )
            closure = await read(connection, operation, lease, calls[0])
        async with self.sessions() as session:
            original = await session.get(OperationApproval, operation["approval_id"])
            if original is None:
                raise ApprovalDenied("original bootstrap approval unavailable")
            amendment = {
                "version": 1,
                "kind": (
                    SETTLED_REAUTHORIZATION_KIND
                    if original.max_runtime_seconds == MAX_BOOTSTRAP_RUNTIME_SECONDS
                    else "bootstrap-runtime-extension-v1"
                ),
                "original_max_runtime_seconds": original.max_runtime_seconds,
                "max_runtime_seconds": max_runtime_seconds,
            }
            terms = review_terms(closure, amendment)
            self.authority_lifetime_seconds(original, terms)
        return await DispatchRecoveryService.request(
            self,
            workspace_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            review_terms=terms,
        )

    @classmethod
    def authority_lifetime_seconds(cls, original, review_terms):
        from harness_jobs.bootstrap_runtime_authorization import (
            settled_runtime_amendment,
        )
        from harness_jobs.identity import OperationRefused
        from harness_jobs.settled_bootstrap import review_runtime

        try:
            return settled_runtime_amendment(
                review_runtime(review_terms), original.max_runtime_seconds
            )
        except OperationRefused:
            raise ApprovalDenied(
                "settled bootstrap runtime amendment refused"
            ) from None

    @classmethod
    def ticket(cls, row, original, subject):
        from harness_jobs.settled_bootstrap import review_runtime

        ticket = DispatchRecoveryService.ticket.__func__(cls, row, original, subject)
        terms = json.loads(row.amendment_json)
        ticket.update(
            recovery_boundary="settled-bootstrap-v1",
            closure_sha256=terms["closure_sha256"],
            runtime_amendment=review_runtime(terms),
            effective_max_runtime_seconds=cls.authority_lifetime_seconds(
                original, terms
            ),
            **(
                {"provenance_assumption": terms["provenance_assumption"]}
                if "provenance_assumption" in terms
                else {}
            ),
        )
        if "provenance_assumption" in terms:
            ticket["recovery_scope"] += (
                " This decision also accepts the separately approved assumption about "
                "two historical diagnostic scripts. Their executed stdin bytes were "
                "not captured in protected audit evidence."
            )
        return ticket

    async def before_decision(self, session, row, result):
        from harness_jobs.settled_bootstrap import digest, read
        from sqlalchemy import select

        from app.models.operation_approval import OperationApproval
        from app.models.operation_budget import OperationBudgetReservation

        await super().before_decision(session, row, result)
        if result != "allowed-once":
            return
        async with self.composition.operation_connect() as connection:
            operation = await connection.fetchrow(
                "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                row.operation_id,
                row.org_id,
                row.workspace_id,
            )
            if (
                operation is None
                or operation["version"] != row.expected_version
                or operation["plan_digest"] != row.plan_digest
            ):
                raise ApprovalDenied("settled bootstrap review version changed")
            lease = await connection.fetchrow(
                "SELECT * FROM harness_operation_leases WHERE operation_id=$1",
                row.operation_id,
            )
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                row.operation_id,
            )
            calls = await active_call_rows(connection, row.operation_id, calls)
            if len(calls) != 1:
                raise ApprovalDenied("settled bootstrap source changed")
            closure = await read(connection, operation, lease, calls[0])
        if digest(closure) != json.loads(row.amendment_json)["closure_sha256"]:
            raise ApprovalDenied("settled bootstrap closure changed since review")
        budget = await session.scalar(
            select(OperationBudgetReservation)
            .where(
                OperationBudgetReservation.job_id == operation["job_id"],
                OperationBudgetReservation.attempt_id == operation["attempt_id"],
                OperationBudgetReservation.org_id == row.org_id,
                OperationBudgetReservation.workspace_id == row.workspace_id,
            )
            .with_for_update()
        )
        original = await session.get(OperationApproval, row.original_approval_id)
        if (
            budget is None
            or budget.state != "retained"
            or any(
                getattr(budget, k) != getattr(original, k)
                for k in (
                    "max_resource_units",
                    "max_runtime_seconds",
                    "max_cost_micros",
                )
            )
        ):
            raise ApprovalDenied("original retained budget differs from this decision")
        # The immutable settlement retains the old reason and unknown accounting.
        # Only the current reservation projection becomes executable again.
        budget.state = "confirmed"
        budget.reason = "Fresh settled bootstrap decision " + row.reauthorization_id
        budget.updated_at = datetime.now(UTC)
