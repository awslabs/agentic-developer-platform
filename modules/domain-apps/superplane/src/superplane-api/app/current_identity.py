"""Domain-side contract for authoritative ADP principal and membership reads.

The gateway has no endpoint that establishes this complete contract yet. When explicitly enabled, a
missing reader or incomplete response refuses mapped-tenant capabilities. The
reader must use an authenticated ADP interface, not token claims or gateway DB
tables; its result is deliberately not cached between authority boundaries.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CurrentIdentity:
    subject: str
    principal_type: str
    adp_org_id: str
    membership_id: str
    active: bool
    enabled: bool
    delegation_id: str | None = None


class CurrentIdentityReader(Protocol):
    async def read(
        self, *, subject: str, principal_type: str, adp_org_id: str
    ) -> CurrentIdentity | None: ...


class IdentityUnavailable(Exception):
    """The upstream identity contract cannot currently establish authority."""


async def require_current_identity(
    reader: CurrentIdentityReader | None,
    *,
    subject: str,
    principal_type: str,
    adp_org_id: str,
    membership_id: str | None = None,
) -> CurrentIdentity:
    if reader is None:
        raise IdentityUnavailable("current ADP identity interface is unavailable")
    try:
        identity = await reader.read(
            subject=subject, principal_type=principal_type, adp_org_id=adp_org_id
        )
    except Exception as exc:
        raise IdentityUnavailable("current ADP identity could not be read") from exc
    if (
        not isinstance(identity, CurrentIdentity)
        or not subject
        or principal_type not in {"human", "service"}
        or not adp_org_id
        or identity.subject != subject
        or identity.principal_type != principal_type
        or identity.adp_org_id != adp_org_id
        or not identity.membership_id
        or identity.active is not True
        or identity.enabled is not True
        or (membership_id is not None and identity.membership_id != membership_id)
        or (principal_type == "service" and not identity.delegation_id)
        or (principal_type == "human" and identity.delegation_id is not None)
    ):
        raise IdentityUnavailable("current ADP principal authority was not established")
    return identity


def identity_checks_enabled() -> bool:
    """One explicit enablement setting applies to both API and worker checks.

    The default preserves the pre-existing signed-token/live-domain-grant path.
    It does not claim current ADP membership integration is available.
    """
    from app.config import settings

    return settings.current_identity_enforced
