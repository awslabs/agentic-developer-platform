"""Consumer of protected recovery routes; never reuse an execution grant or token.

Gateway authenticates the real recovery run and the current claim on every request.
It owns provider status/inventory reads. This process receives observations only,
not an ordinary SkyPilot bearer or an AWS role with mutation capabilities.
"""

from dataclasses import replace
from datetime import UTC, datetime
import re
from uuid import uuid4

from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused, ResolvedPrincipal


def claim_identity(lease):
    return {
        key: getattr(lease, key)
        for key in (
            "operation_id",
            "org_id",
            "workspace_id",
            "holder",
            "attempt_id",
            "fence_token",
        )
    }


def same_recovery_claim(current, previous):
    """A renewed expiry does not change the original holder, fence or deadline."""
    return replace(current, expires_at=previous.expires_at) == previous


def same_recovery_operation(current, previous):
    from harness_jobs.recovery_grant import RecoveryGrant

    if not isinstance(current.grant, RecoveryGrant) or not isinstance(
        previous.grant, RecoveryGrant
    ):
        return False
    normalized = replace(
        current.grant.lease, expires_at=previous.grant.lease.expires_at
    )
    return replace(current, grant=replace(current.grant, lease=normalized)) == previous


class RecoveryAuthority:
    def __init__(self, transport):
        self.transport = transport

    async def recovery_scope(self):
        data = await self.transport.post(
            "/internal/v1/controller-execution/recovery/scope", {}
        )
        try:
            if data["version"] != 1 or data["observation_only"] is not True:
                raise ValueError("unverified recovery scope")
            deadline = datetime.fromisoformat(data["not_after"])
            if deadline.tzinfo is None or deadline <= datetime.now(UTC):
                raise ValueError("expired recovery scope")
            permissions = frozenset(data["permissions"])
            if "workspace:recover" not in permissions:
                raise ValueError("recovery permission absent")
            return ResolvedPrincipal(
                data["org_id"], data["workspace_id"], data["subject"], permissions
            )
        except (KeyError, TypeError, ValueError):
            raise OperationRefused("authenticated recovery scope unavailable") from None

    async def resolve_recovery(self, claim):
        principal = await self.recovery_scope()
        data = await self.transport.post(
            "/internal/v1/controller-execution/recovery/authority",
            {"claim": claim_identity(claim)},
        )
        if data.get("observation_only") is not True or data.get(
            "claim"
        ) != claim_identity(claim):
            raise OperationRefused("recovery authority claim mismatch")
        operation = self.transport._verified_operation(
            data, claim.operation_id, recovery_principal=principal
        )
        lease = operation.grant.lease
        if claim_identity(lease) != claim_identity(claim) or min(
            lease.expires_at, lease.runtime_deadline
        ) <= datetime.now(UTC):
            raise OperationRefused("recovery claim expired or replaced")
        return operation

    async def _observation(self, path, claim, **arguments):
        query_id = str(uuid4())
        started = datetime.now(UTC)
        data = await self.transport.post(
            "/internal/v1/controller-execution/recovery/" + path,
            {"claim": claim_identity(claim), "query_id": query_id, **arguments},
        )
        try:
            checked = datetime.fromisoformat(data["checked_at"])
            if (
                data["version"] != 1
                or data["observation_only"] is not True
                or data["claim"] != claim_identity(claim)
                or data["query_id"] != query_id
                or checked.tzinfo is None
                or not started <= checked <= datetime.now(UTC)
            ):
                raise ValueError("observation binding or freshness mismatch")
        except (KeyError, ValueError, TypeError):
            raise OperationRefused(
                "claim-bound fresh provider observation unavailable"
            ) from None
        # A slow read cannot confer authority after revocation/claim replacement.
        await self.resolve_recovery(claim)
        return data

    async def observe(self, claim, idempotency_key, _provider, _kind, _target):
        data = await self._observation(
            "observe", claim, idempotency_key=idempotency_key
        )
        outcome = CallOutcome(data.get("outcome"))
        if outcome not in {CallOutcome.SUCCEEDED, CallOutcome.UNKNOWN}:
            # Failed/cancelled request status does not establish no allocation.
            raise OperationRefused("request status cannot establish resource absence")
        return (
            outcome,
            "claim-authorized provider observation",
            data.get("provider_ref"),
        )

    async def inventory(self, claim, allocation_id):
        data = await self._observation("inventory", claim, allocation_id=allocation_id)
        if (
            data.get("allocation_id") != allocation_id
            or data.get("complete") is not True
        ):
            raise OperationRefused("complete allocation inventory unavailable")
        resources = data.get("resources")
        if not isinstance(resources, list) or len(resources) > 2048:
            raise OperationRefused("invalid allocation inventory")
        return resources

    async def lifecycle(
        self, claim, idempotency_key, *, artifact_id, plan_digest, phase
    ):
        data = await self._observation(
            "lifecycle", claim, idempotency_key=idempotency_key
        )
        if (
            data.get("idempotency_key") != idempotency_key
            or data.get("result_artifact_id") != artifact_id
            or data.get("plan_digest") != plan_digest
            or data.get("phase") != phase
            or not isinstance(data.get("facts"), dict)
        ):
            raise OperationRefused("original lifecycle result observation changed")
        return data["facts"]

    async def account_creation(
        self, claim, idempotency_key, *, plan_digest, request_id
    ):
        data = await self._observation(
            "account-creation", claim, idempotency_key=idempotency_key
        )
        facts = data.get("facts")
        if (
            data.get("idempotency_key") != idempotency_key
            or data.get("plan_digest") != plan_digest
            or data.get("phase") != "create-account"
            or not isinstance(facts, dict)
            or facts.get("observation_only") is not True
            or facts.get("original_call_key") != idempotency_key
            or facts.get("plan_digest") != plan_digest
            or facts.get("creation_request_id") != request_id
            or facts.get("creation_status")
            not in {"succeeded", "failed", "in-progress"}
            or facts.get("release_permitted") is not False
            or facts.get("bootstrap_verified") is not False
        ):
            raise OperationRefused("original account request observation changed")
        if facts["creation_status"] == "succeeded":
            if (
                not isinstance(facts.get("account_id"), str)
                or not re.fullmatch(r"[0-9]{12}", facts["account_id"])
                or not isinstance(facts.get("creation_source_parent_id"), str)
                or not re.fullmatch(
                    r"r-[a-z0-9]{4,32}", facts["creation_source_parent_id"]
                )
            ):
                raise OperationRefused(
                    "successful account handoff identity unavailable"
                )
        elif facts.get("account_id") is not None:
            raise OperationRefused(
                "incomplete account observation cannot assert child identity"
            )
        return facts

    async def deliver_settlement(self, **receipt):
        result = await self.transport.post(
            "/internal/v1/controller-execution/recovery/settlement",
            receipt,
        )
        if result.get("receipt_id") != receipt["receipt_id"]:
            raise OperationRefused("ledger settlement acknowledgement mismatch")
        return result["receipt_id"]
