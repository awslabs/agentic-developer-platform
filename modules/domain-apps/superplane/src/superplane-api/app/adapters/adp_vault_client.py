"""API workload IAM credential-evidence reader; never a material delivery channel."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import botocore.session
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx
from superplane_contracts.connections import CredentialReference, VaultOwnership

from app.adapters.iam_signing import signed_headers
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
        region: str,
        timeout: float = _DEFAULT_TIMEOUT,
        client_factory: Any = None,
        session: Any = None,
    ) -> None:
        if not isinstance(region, str) or not re.fullmatch(
            r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region
        ):
            raise ValueError("Evidence signing region is required")
        if not isinstance(base_url, str) or not re.fullmatch(
            r"https://[a-z0-9]{10}\.execute-api\."
            + re.escape(region)
            + r"\.amazonaws\.com/[A-Za-z0-9_-]{1,128}",
            base_url,
        ):
            raise ValueError(
                "Evidence requires the exact regional API Gateway endpoint and stage"
            )
        self._base_url, self._region = base_url, region
        self._timeout, self._client_factory = timeout, client_factory
        self._session = session or botocore.session.get_session()

    def _headers(self, url: str, encoded: bytes) -> dict[str, str]:
        # Resolve and freeze on every send so web-identity renewal remains active.
        credentials = self._session.get_credentials()
        if credentials is None or credentials.method != "assume-role-with-web-identity":
            raise RuntimeError("Evidence requires the API workload web identity")
        return signed_headers(credentials, url, encoded, self._region)

    async def _post_async(
        self,
        path: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        if path != EVIDENCE_PATH:
            raise ValueError("Unsupported evidence route")
        url = self._base_url + path
        encoded = json.dumps(
            dict(payload), separators=(",", ":"), allow_nan=False
        ).encode()
        headers = await asyncio.to_thread(self._headers, url, encoded)
        client_cm = (
            self._client_factory()
            if self._client_factory
            else httpx.AsyncClient(
                timeout=self._timeout,
                trust_env=False,
                follow_redirects=False,
            )
        )
        async with client_cm as client:
            async with client.stream(
                "POST", url, content=encoded, headers=headers
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise RuntimeError("Evidence response exceeds its bound")
                try:
                    body = json.loads(content)
                except (ValueError, UnicodeError):
                    body = {}
                return response.status_code, body if isinstance(body, dict) else {}

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
        except Exception:
            # Transport failure is unavailability, not denial. Re-raised (the port
            # allows it for genuine unavailability) with no body in the message.
            raise RuntimeError("ADP vault evidence is unavailable") from None

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
        if (
            body.get("credential_id") != reference.credential_id
            or body.get("service") != reference.service
            or body.get("label") != reference.label
        ):
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
    deployment without a selected IAM evidence transport. A permissive stub that
    returned evidence would be a bypass, and one that returned ``None`` from every
    read would report a configuration gap as a per-credential denial.

    Delivery is intentionally composed by the trusted executor per attempt, never
    from application-wide token settings. See ExecutorVaultTransport.
    """
    base_url = getattr(settings, "adp_gateway_internal_url", "") or ""
    auth = getattr(settings, "adp_gateway_evidence_auth", "") or ""
    if (
        not base_url
        and not auth
        and not getattr(settings, "adp_gateway_internal_api_key", "")
    ):
        return None
    if (
        auth != "api-producer-iam"
        or base_url != getattr(settings, "superplane_operation_gateway_url", "")
        or getattr(settings, "adp_gateway_internal_api_key", "")
    ):
        raise ValueError(
            "Evidence requires the selected API producer IAM transport without a shared key"
        )
    region = getattr(settings, "superplane_operation_gateway_region", "")
    # The timeout comes from settings so the bound is a deployment decision rather
    # than an image constant (#5535). `getattr` with the module default, because this
    # function accepts any settings-shaped object — including the stubs the
    # composition tests pass — and a missing attribute must fall back to the safe
    # bound rather than raise. `or` is deliberately not used: it would treat a
    # configured 0.0 as absent, and 0.0 is refused by the setting's validator, so
    # silently replacing it with 10.0 would hide a misconfiguration.
    timeout = getattr(settings, "adp_vault_timeout_seconds", _DEFAULT_TIMEOUT)
    return AdpVaultClient(base_url=base_url, region=region, timeout=timeout)
