"""Domain-side contract for uncached authoritative ADP principal and membership reads."""

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


class IdentityDenied(IdentityUnavailable):
    """The upstream provider established that the principal lacks authority."""


class ProducerIdentityReader:
    def __init__(self, transport, *, domain_org_id: str, adp_org_id: str):
        self.transport = transport
        self.domain_org_id = domain_org_id
        self.adp_org_id = adp_org_id

    async def read(self, *, subject: str, principal_type: str, adp_org_id: str) -> CurrentIdentity:
        if adp_org_id != self.adp_org_id or principal_type != "human":
            raise IdentityDenied("current ADP identity organization or type refused")
        from app.adapters.operation_dispatch import ProducerRefusedError

        try:
            response = await self.transport.post(
                "/current-identity",
                {"domain": "superplane", "org_id": self.domain_org_id, "subject": subject, "principal_type": principal_type},
                distinguish_denial=True,
            )
        except ProducerRefusedError:
            raise IdentityDenied("current ADP identity refused") from None
        except Exception:
            raise IdentityUnavailable("current ADP identity provider unavailable") from None
        if type(response.get("version")) is not int or response["version"] != 1:
            raise IdentityUnavailable("unsupported ADP identity contract")
        try:
            return CurrentIdentity(**{key: response[key] for key in (
                "subject", "principal_type", "adp_org_id", "membership_id", "active", "enabled"
            )})
        except (KeyError, TypeError):
            raise IdentityUnavailable("incomplete ADP identity contract") from None


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
    except IdentityDenied:
        raise
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
        raise IdentityDenied("current ADP principal authority was not established")
    return identity


def identity_checks_enabled() -> bool:
    """One explicit enablement setting applies to both API and worker checks.

    The default preserves the pre-existing signed-token/live-domain-grant path.
    It does not claim current ADP membership integration is available.
    """
    from app.config import settings

    return settings.current_identity_enforced
