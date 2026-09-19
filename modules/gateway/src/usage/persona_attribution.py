"""Trusted persona-model evidence carried from authority to usage rows (#5426).

The values in this object are reporting evidence, not authorization.  They are
nevertheless security-sensitive: accepting them from a request header would let
the billed worker relabel its own spend.  The only production constructor lives
in :mod:`src.agentauth.model_policy` and reads the protected execution snapshot.
The object is then attached to ``TokenContext`` as a pydantic ``PrivateAttr``.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PersonaUsageAttribution:
    """Immutable evidence for one protected model invocation.

    ``principal_kind`` follows the PMM vocabulary (``human`` or
    ``service_account``); it is deliberately not ``TokenContext.account_type``.
    The two namespaces answer different questions and must never be compared.
    """

    tenant_id: str
    invocation_id: str
    root_invocation_id: str
    chain_id: str
    persona_key: str
    compatibility_class: str
    harness_contract_revision: str
    principal_kind: Literal["human", "service_account"]
    principal_id: str
    snapshot_digest: str
    policy_revision: str
    catalogue_revision: str
    requested_model_id: str | None = None
    resolved_model_id: str | None = None
    resolution_source: Literal["explicit-direct", "principal-mapping", "system-default"] | None = None
    runtime_posture: Literal["report_only"] | None = None
    posture_revision: int | None = None
