"""Trusted ADP vault evidence port; never infer ownership from registration.

A startup adapter must retrieve vault-owned metadata and independently verify any
provider validation attestation. No adapter is installed by default. The domain
stores references and evidence only, never a credential value or vault replacement.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from superplane_contracts.connections import CredentialReference, VaultOwnership


@dataclass(frozen=True)
class VerifiedCredentialEvidence:
    org_id: str
    workspace_id: str
    reference: CredentialReference
    ownership: VaultOwnership
    expires_at: datetime
    attested_report_digest: str | None = None
    report_checked_at: datetime | None = None


class CredentialEvidenceReader(Protocol):
    async def read(
        self,
        *,
        org_id: str,
        workspace_id: str,
        reference: CredentialReference,
        principal: str,
        report_digest: str | None,
    ) -> VerifiedCredentialEvidence | None:
        """Return current vault metadata and independently verified report binding.

        report_digest is untrusted request context, not proof. The adapter must
        compare it against an authenticated provider/vault validation report bound
        to this exact credential and workspace; echoing it would authorize forgery.
        Return None if ownership or requested attestation cannot be established.
        """
        ...


_reader: CredentialEvidenceReader | None = None


def get_credential_evidence_reader() -> CredentialEvidenceReader | None:
    return _reader


def install_credential_evidence_reader(reader: CredentialEvidenceReader) -> None:
    """Startup composition only; no HTTP endpoint may replace the trust source."""
    global _reader
    if _reader is not None:
        raise RuntimeError("credential evidence reader is already installed")
    _reader = reader
