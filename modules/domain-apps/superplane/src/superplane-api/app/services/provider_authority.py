"""Fail-closed integration port for B's provider-operation authority.

B's live facade is not published yet. As with services.provisioning, production
has no validator by default. This module neither issues authority nor implements
leases, fencing or recovery. A future trusted adapter must verify B's live
authority and resolve its authenticated executor and exact operation context.
The opaque request value is never persisted or treated as proof on its own.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from superplane_contracts import ProviderHandle, Submitter


@dataclass(frozen=True)
class VerifiedProviderAuthority:
    """Server-resolved context, never deserialized from an HTTP request.

    IDs identify B's operation/run/attempt, not A-owned lifecycle records. The
    handle must be the pre-call identity, without a provider reference. Recovery
    across attempts needs an explicit B contract; A never rebinds it implicitly.
    """

    operation_id: str
    run_id: str
    attempt_id: str
    submitter_id: str
    handle: ProviderHandle
    expires_at: datetime
    active: bool


class ProviderAuthorityValidator(Protocol):
    async def resolve(
        self, authority: str, *, submitter: Submitter, handle: ProviderHandle
    ) -> VerifiedProviderAuthority | None:
        """Verify live B authority; refuse fabricated, revoked or foreign values.

        Check the active lease/fence and permission for this operation. Do not
        create a binding from the supplied handle or credential scope alone.
        """
        ...


_validator: ProviderAuthorityValidator | None = None


def set_provider_authority_validator(
    validator: ProviderAuthorityValidator | None,
) -> None:
    """Trusted startup wiring only; no HTTP/config path installs a validator."""
    global _validator
    _validator = validator


def get_provider_authority_validator() -> ProviderAuthorityValidator | None:
    return _validator


def uninstall_provider_authority_validator(
    validator: ProviderAuthorityValidator,
) -> bool:
    """Remove `validator` if it is the installed one. Returns whether it was.

    Identity-scoped for the reason given at
    `credential_evidence.uninstall_credential_evidence_reader`: a shutdown must
    release only its own registration. Here the consequence is sharper than a
    missing reader, because `set_` above does not refuse an overwrite — an
    unconditional clear on shutdown would leave the port empty while another
    composition believed it held it, and the next request would answer 503 with
    nothing in the logs saying why.
    """
    global _validator
    if _validator is not validator:
        return False
    _validator = None
    return True
