"""Attempt-owned delivery channel. No control-plane key or evidence-reader access."""

import asyncio
import logging
from typing import Any

from superplane_contracts.delivery import (
    REVOCATION_LIMITATION,
    DeliveryLease,
    DeliveryRefused,
    RevocationState,
    SecretMaterial,
)

logger = logging.getLogger(__name__)
DELIVERY_PATH = "/internal/v1/credential-delivery"
PREFLIGHT_PATH = "/internal/v1/credential-delivery/preflight"
_REFUSED = "credential delivery refused"


class ExecutorVaultChannel:
    """Compose only with the admitted attempt's SigV4/projected-token transport."""

    def __init__(self, *, transport):
        if transport is None:
            raise ValueError("executor delivery transport is required")
        self._delivery_transport = transport

    @staticmethod
    def _refuse_if_async(method: str) -> None:
        """Refuse a sync call made from inside a running event loop.

        Blocking HTTP on the loop thread would stall every other request in the
        process, and the failure is invisible — it looks like latency, not a bug.
        Raised as a ``RuntimeError`` rather than a ``DeliveryRefused``: this is a
        composition mistake by the caller, not the vault denying anything, and
        mapping it onto a refusal would hide a bug behind a plausible denial.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError(
            f"{method} is synchronous and must not be called from an event loop; use a worker thread"
        )

    @staticmethod
    def _decode(response: Any) -> dict[str, Any]:
        """Parse a JSON body, or return ``{}``.

        Never logs or re-raises with the body. On the delivery path the body holds
        the credential value, so a decode error that included it would write the
        secret to the log — the exact failure the redaction machinery exists to stop.
        """
        try:
            body = response.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------
    # TrustedDeliveryChannel
    # ------------------------------------------------------------------

    def revocation_state(self, lease: DeliveryLease) -> RevocationState:
        """Whether this lease's credential still admits work, per the Gateway.

        Fails CLOSED: a transport error, an unexpected status or an unreadable body
        all answer "does not admit work", carrying :data:`REVOCATION_LIMITATION`. The
        alternative — treating an unreachable vault as "still valid" — would let work
        proceed on a credential that may have been revoked, which is the direction
        that loses containment.
        """
        self._refuse_if_async("revocation_state")
        try:
            response = self._delivery_transport.preflight(
                lease, self._delivery_payload(lease)
            )
            status, body = response.status_code, self._decode(response)
        except Exception:
            logger.warning(
                "Vault revocation check failed for lease %s; treating as revoked",
                lease.lease_id,
            )
            return RevocationState(admits_work=False, limitation=REVOCATION_LIMITATION)

        if status != 200 or not isinstance(body.get("admits_work"), bool):
            return RevocationState(admits_work=False, limitation=REVOCATION_LIMITATION)
        if not body["admits_work"]:
            # Always surface the limitation, using the Gateway's text when it sent
            # one and the contract's own constant otherwise — RevocationState raises
            # if a refusal carries no limitation, and that must not become the
            # failure mode of a revoked credential.
            limitation = body.get("limitation")
            return RevocationState(
                admits_work=False,
                limitation=limitation
                if isinstance(limitation, str) and limitation.strip()
                else REVOCATION_LIMITATION,
            )
        return RevocationState(admits_work=True)

    def fetch_material(self, lease: DeliveryLease) -> SecretMaterial:
        """Hand over the leased credential's value, for this lease only.

        Takes the whole lease, never a credential id — a method that accepted an id
        would be a raw-read endpoint for any credential, which is what the design
        forbids and what the absent ``read_credential`` is absent to prevent.

        Raises :class:`DeliveryRefused` with a fixed reason for every failure. No
        response body, exception text or provider detail is included, because this
        exception string reaches logs and agent transcripts and the body on this path
        contains the credential value.
        """
        self._refuse_if_async("fetch_material")
        try:
            if self._delivery_transport is None:
                raise DeliveryRefused(_REFUSED)
            response = self._delivery_transport.post(
                lease, self._delivery_payload(lease)
            )
            status, body = response.status_code, self._decode(response)
        except Exception:
            logger.warning(
                "Vault delivery transport failed for lease %s", lease.lease_id
            )
            raise DeliveryRefused(_REFUSED) from None

        if status != 200:
            logger.info("Vault refused delivery for lease %s", lease.lease_id)
            raise DeliveryRefused(_REFUSED) from None

        value = body.get("value")
        if not isinstance(value, str) or not value:
            raise DeliveryRefused(_REFUSED) from None
        try:
            return SecretMaterial(value)
        except Exception:
            # `from None` so the ContractViolation's own message — which was built
            # around the value — cannot appear in this exception's cause chain.
            raise DeliveryRefused(_REFUSED) from None

    def record_delivered(self, lease: DeliveryLease) -> None:
        """Audit that material reached the lease's recipient.

        The Gateway already writes the authoritative audit row inside the delivery
        call itself, which is the only place that can record it atomically with the
        read. This is the domain-side record, and it is deliberately a log line
        rather than a second Gateway call: a separate audit request could fail after
        a successful delivery, leaving the domain believing nothing was delivered
        when it was — the reverse of the status-flag failure this unit replaces.
        """
        self._refuse_if_async("record_delivered")
        logger.info(
            "Delivered leased credential lease=%s workspace=%s credential=%s recipient=%s operation=%s mocked=%s",
            lease.lease_id,
            lease.workspace_id,
            lease.reference.credential_id,
            lease.recipient.executor_id,
            lease.binding.operation_id,
            lease.is_mocked,
        )

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------

    @staticmethod
    def _binding_payload(lease: DeliveryLease) -> dict[str, Any]:
        """Transmit actual durable IDs; the Gateway verifies live lease authority."""
        principal = lease.binding.principal
        if not lease.binding.job_id or not lease.binding.attempt_id:
            raise DeliveryRefused(_REFUSED)
        return {
            "operation_id": lease.binding.operation_id,
            "attempt_id": lease.binding.attempt_id,
            "job_id": lease.binding.job_id,
            "org_id": getattr(principal, "org_id", ""),
            "workspace_id": lease.workspace_id,
            "credential_id": lease.reference.credential_id,
        }

    @classmethod
    def _delivery_payload(cls, lease: DeliveryLease) -> dict[str, Any]:
        payload = cls._binding_payload(lease)
        payload.update(
            {
                "recipient": lease.recipient.executor_id,
                "service": lease.reference.service,
                "label": lease.reference.label,
                "provider": lease.binding.provider,
                "provider_account_id": lease.binding.provider_account_id,
            }
        )
        return payload
