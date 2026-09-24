"""Control-plane ADP vault evidence reader.

This client owns the internal evidence key and is composed only in the Superplane
API. It exposes no TrustedDeliveryChannel methods. ExecutorVaultChannel instead
uses attempt-owned SigV4 and projected run/pod tokens for preflight and delivery.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx
from superplane_contracts.connections import CredentialReference, VaultOwnership

from app.services.credential_evidence import VerifiedCredentialEvidence

logger = logging.getLogger(__name__)

# Control-plane evidence endpoint, separate from the executor channel's routes.
EVIDENCE_PATH = "/internal/v1/credential-evidence"

# Substituted for a vault-reported `null` expiry. The port requires a timestamp:
# `datetime.max` is year 9999, so a horizon in 9999 leaves no room for a consumer to
# add a delta to it without raising OverflowError. Year 9000 keeps that arithmetic
# safe while staying obviously synthetic.
NON_EXPIRING_HORIZON = datetime(9000, 1, 1, tzinfo=timezone.utc)

_DEFAULT_TIMEOUT = 10.0


class AdpVaultClient:
    """Control-plane credential evidence reader, never a material delivery channel."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT,
        client_factory: Any = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("ADP vault base_url is required")
        if not api_key or not api_key.strip():
            # Fail at construction, not at first use: a client built without a
            # credential would otherwise 403 on every call at runtime and read as
            # "the vault denied us" rather than "we were never configured".
            raise ValueError("ADP vault api_key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # Injectable purely so tests can supply a transport; defaults to httpx.
        self._client_factory = client_factory

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Api-Key": self._api_key, "Content-Type": "application/json"}

    async def _post_async(
        self, path: str, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if self._client_factory is not None:
            client_cm = self._client_factory()
        else:
            client_cm = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            )
        async with client_cm as client:
            response = await client.post(
                path, json=dict(payload), headers=self._headers()
            )
        return response.status_code, self._decode(response)

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
    # CredentialEvidenceReader
    # ------------------------------------------------------------------

    async def read(
        self,
        *,
        org_id: str,
        workspace_id: str,
        reference: CredentialReference,
        principal: str,
        report_digest: str | None,
    ) -> VerifiedCredentialEvidence | None:
        """Current vault evidence for one credential, or ``None``.

        Returns ``None`` for every unestablished condition and **never raises** for a
        refusal: the port declares ``NONE_MEANS_UNVERIFIED``, and the consumer maps a
        raise onto 503 "vault unavailable". Raising on a denial would report an
        authorization decision as an outage and send an operator to diagnose a
        healthy system.

        A 503 from the Gateway is the one case that genuinely IS unavailability, and
        it propagates as an exception so the consumer's 503 is truthful.
        """
        if not isinstance(reference, CredentialReference):
            return None

        payload = {
            "org_id": org_id,
            "workspace_id": workspace_id,
            "credential_id": reference.credential_id,
            "service": reference.service,
            "label": reference.label,
            "principal": principal,
            "report_digest": report_digest,
        }
        try:
            status, body = await self._post_async(EVIDENCE_PATH, payload)
        except Exception as exc:
            # Transport failure is unavailability, not denial. Re-raised (the port
            # allows it for genuine unavailability) with no body in the message.
            raise RuntimeError("ADP vault evidence is unavailable") from exc

        if status == 503:
            raise RuntimeError("ADP vault evidence is unavailable")
        if status != 200:
            # 403 and anything else unexpected are both "could not establish".
            return None

        return self._evidence_from(
            body,
            org_id=org_id,
            workspace_id=workspace_id,
            reference=reference,
            report_digest=report_digest,
        )

    def _evidence_from(
        self,
        body: Mapping[str, Any],
        *,
        org_id: str,
        workspace_id: str,
        reference: CredentialReference,
        report_digest: str | None,
    ) -> VerifiedCredentialEvidence | None:
        """Map a vault response onto the contract type, or ``None``.

        Re-checks the identity of what came back rather than trusting the envelope:
        a response describing a different credential, workspace or tenant than the
        one asked about is discarded. The consumer performs the same comparisons
        again, and that duplication is intentional — this layer must not be the only
        thing standing between a confused vault answer and an authorization decision.
        """
        if body.get("org_id") != org_id or body.get("workspace_id") != workspace_id:
            return None
        if body.get("credential_id") != reference.credential_id:
            return None

        # The attestation must come back exactly as claimed when one was claimed. The
        # Gateway recomputes it independently; this confirms the answer it returned
        # is the one bound to this request.
        attested = body.get("attested_report_digest")
        if report_digest is not None and attested != report_digest:
            return None

        owner_principal = body.get("owner_principal")
        if not isinstance(owner_principal, str) or not owner_principal.strip():
            # Unresolved ownership is a denial, never a default.
            return None

        try:
            ownership = VaultOwnership(
                credential_id=reference.credential_id,
                owner_principal=owner_principal,
                delegated_to_workspaces=frozenset(
                    body.get("delegated_to_workspaces") or ()
                ),
            )
        except Exception:
            return None

        expires_at = self._expiry(
            body.get("expires_at"), credential_id=reference.credential_id
        )
        if expires_at is None:
            return None

        checked_at = self._timestamp(body.get("report_checked_at"))
        if report_digest is not None and checked_at is None:
            # An attestation whose observation time cannot be read is no attestation:
            # the consumer refuses a naive or absent `report_checked_at` anyway, so
            # returning it would produce a 403 that reads as the vault's fault.
            return None

        try:
            return VerifiedCredentialEvidence(
                org_id=org_id,
                workspace_id=workspace_id,
                reference=reference,
                ownership=ownership,
                expires_at=expires_at,
                attested_report_digest=attested if report_digest is not None else None,
                report_checked_at=checked_at if report_digest is not None else None,
            )
        except Exception:
            return None

    def _expiry(self, raw: Any, *, credential_id: str) -> datetime | None:
        """Map the vault's ``expires_at`` onto the contract's non-optional field.

        ``None`` from the vault means "this credential does not expire" and becomes
        :data:`NON_EXPIRING_HORIZON`. The substitution is logged at INFO with the
        credential id (an identifier, never the value) so a far-future expiry in a
        response can be told apart from one this adapter supplied.

        Returns ``None`` — a refusal — for a value that is present but unusable. A
        malformed or naive expiry is not something to guess about: the consumer
        refuses a naive datetime, so passing one through would produce a 403 that
        looks like the vault's fault rather than a parse failure here.
        """
        if raw is None:
            logger.info(
                "Vault reports no expiry for credential %s; substituting the non-expiring horizon",
                credential_id,
            )
            return NON_EXPIRING_HORIZON
        return self._timestamp(raw)

    @staticmethod
    def _timestamp(raw: Any) -> datetime | None:
        """Parse an ISO-8601 timestamp into an aware UTC datetime, or ``None``.

        A naive value is treated as UTC rather than rejected: the Gateway normalises
        to UTC before serialising, and SQLite-backed rows can round-trip without an
        offset, so rejecting naive input would fail against one backend and not the
        other for reasons that have nothing to do with authorization.
        """
        if isinstance(raw, datetime):
            parsed = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )


def build_vault_client(settings: Any) -> AdpVaultClient | None:
    """Construct the client from settings, or ``None`` when unconfigured.

    ``None`` rather than a stub: the consumer answers 503 "vault evidence is
    unavailable" when no reader is installed, which is the honest answer for a
    deployment that has not been given vault credentials. A permissive stub that
    returned evidence would be a bypass, and one that returned ``None`` from every
    read would report a configuration gap as a per-credential denial.

    Delivery is intentionally composed by the trusted executor per attempt, never
    from application-wide token settings. See ExecutorVaultTransport.
    """
    base_url = getattr(settings, "adp_gateway_internal_url", "") or ""
    api_key = getattr(settings, "adp_gateway_internal_api_key", "") or ""
    if not base_url.strip() or not api_key.strip():
        logger.info(
            "ADP vault client is not configured; credential evidence will be unavailable"
        )
        return None
    # The timeout comes from settings so the bound is a deployment decision rather
    # than an image constant (#5535). `getattr` with the module default, because this
    # function accepts any settings-shaped object — including the stubs the
    # composition tests pass — and a missing attribute must fall back to the safe
    # bound rather than raise. `or` is deliberately not used: it would treat a
    # configured 0.0 as absent, and 0.0 is refused by the setting's validator, so
    # silently replacing it with 10.0 would hide a misconfiguration.
    timeout = getattr(settings, "adp_vault_timeout_seconds", _DEFAULT_TIMEOUT)
    return AdpVaultClient(base_url=base_url, api_key=api_key, timeout=timeout)
