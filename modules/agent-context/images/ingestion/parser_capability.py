"""Per-asset fetch/publish capability contracts for the isolated parser pipeline.

Capability contracts bind authority to specific actions, assets and attempts.
They prevent broad tenant wildcards and ensure the parser receives neither
cloud credentials nor publish authority.

Three capability types:

- **FetchCapability**: authorises fetching source/dependency artifacts for a
  specific asset and attempt from approved sources only.
- **PublishCapability**: authorises storing validated parser output for a
  specific asset and attempt to specific object keys with bounds.
- **ParseCapability**: the parser's own contract — no cloud credentials,
  no publish authority, just permission to read prepared input and write
  bounded output.

Production grant issuance is intentionally unavailable until the canonical
asset-authority owner (see issue #6059 seam documentation) delivers a reviewed
server-owned capability contract.  The ``ProductionAuthorizer`` denies all
requests; ``TestAuthorizer`` supplies synthetic authority through dependency
injection for tests only.
"""

from __future__ import annotations

import abc
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Capability contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchCapability:
    """Authorises fetching source/dependency artifacts for one asset+attempt.

    Consumed by the dependency preparation stage to validate that every
    fetch operation targets an approved source within the declared bounds.
    """

    invocation_id: str
    asset_id: str
    attempt_id: str

    # Approved sources
    approved_registries: list[str] = field(default_factory=list)
    approved_source_prefixes: list[str] = field(default_factory=list)

    # Bounds
    max_fetch_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    deadline_seconds: int = 300

    # Timing
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PublishCapability:
    """Authorises storing validated parser output for one asset+attempt.

    Consumed by the publisher to validate that every write targets an
    allowed key prefix within the declared bounds.  The parser never
    receives this; only the trusted publisher holds it.
    """

    invocation_id: str
    asset_id: str
    attempt_id: str

    # Allowed storage destinations
    allowed_object_keys: list[str] = field(default_factory=list)
    allowed_prefixes: list[str] = field(default_factory=list)

    # Bounds
    max_publish_bytes: int = 512 * 1024 * 1024  # 512 MiB

    # Timing
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    # State
    consumed: bool = False
    cancelled: bool = False

    @property
    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.consumed and not self.cancelled and not self.is_expired

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParseCapability:
    """The parser's own contract — no credentials, bounded I/O.

    This is what the parser receives.  It carries no cloud authority, no
    publish authority, and no network access.  It specifies only the
    input/output paths and resource bounds.
    """

    invocation_id: str
    asset_id: str
    attempt_id: str

    source_dir: str
    output_dir: str
    allowed_languages: list[str] = field(default_factory=list)

    # Bounds
    output_bytes_max: int = 512 * 1024 * 1024
    deadline_seconds: int = 600

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Authorizer interface and implementations
# ---------------------------------------------------------------------------


class CapabilityDeniedError(RuntimeError):
    """Raised when a capability grant is denied."""


class Authorizer(abc.ABC):
    """Abstract interface for issuing capability grants.

    The canonical production implementation must be supplied by the
    asset-authority owner (see issue #6059 canonical authority seam).
    Until that contract is delivered, ``ProductionAuthorizer`` denies
    all requests.

    Required contract for a production implementation:
    - ``issue_fetch(asset_id, attempt_id, ...)`` returns a ``FetchCapability``
      bound to a server-owned run/attempt identity with expiry.
    - ``issue_publish(asset_id, attempt_id, ...)`` returns a ``PublishCapability``
      bound to allowed object keys with byte limits.
    - ``issue_parse(asset_id, attempt_id, ...)`` returns a ``ParseCapability``
      with no cloud authority.
    - All grants must be traceable to a server-owned invocation record.
    - Grants must be invalidatable (cancel, expire).
    - The authorizer must not accept caller-declared tenant/asset IDs,
      unsigned queue fields, or status-callback compatibility behaviour
      as canonical grant authority.

    Owner needed: S10/A03 asset-authority or designated S15 integration owner.
    Schema needed: server-owned run/attempt/asset capability table with
    invocation provenance, expiry, cancellation and replay prevention.
    """

    @abc.abstractmethod
    def issue_fetch(
        self,
        asset_id: str,
        attempt_id: str,
        *,
        approved_registries: list[str] | None = None,
        approved_source_prefixes: list[str] | None = None,
    ) -> FetchCapability:
        """Issue a fetch capability for one asset+attempt."""

    @abc.abstractmethod
    def issue_publish(
        self,
        asset_id: str,
        attempt_id: str,
        *,
        allowed_prefixes: list[str] | None = None,
    ) -> PublishCapability:
        """Issue a publish capability for one asset+attempt."""

    @abc.abstractmethod
    def issue_parse(
        self,
        asset_id: str,
        attempt_id: str,
        *,
        source_dir: str = "",
        output_dir: str = "",
        allowed_languages: list[str] | None = None,
    ) -> ParseCapability:
        """Issue a parse capability for one asset+attempt."""


class ProductionAuthorizer(Authorizer):
    """Strict denying authorizer for production.

    Denies ALL capability requests until a reviewed canonical server-owned
    asset/run capability contract is delivered by the designated owner.

    This is intentional, not a bug.  Production isolated parsing requires:
    1. A server-owned invocation/attempt identity scheme
    2. Reviewed grant issuance tied to that identity
    3. Authenticated publish activation
    None of these are available yet (see issue #6059 canonical authority seam).
    """

    def issue_fetch(self, asset_id: str, attempt_id: str, **kwargs: Any) -> FetchCapability:
        raise CapabilityDeniedError(
            "Production fetch capability denied: canonical asset-authority "
            "contract not yet delivered. "
            "Owner needed: S10/A03 asset-authority or designated S15 integration owner."
        )

    def issue_publish(self, asset_id: str, attempt_id: str, **kwargs: Any) -> PublishCapability:
        raise CapabilityDeniedError(
            "Production publish capability denied: canonical asset-authority "
            "contract not yet delivered. "
            "Owner needed: S10/A03 asset-authority or designated S15 integration owner."
        )

    def issue_parse(self, asset_id: str, attempt_id: str, **kwargs: Any) -> ParseCapability:
        raise CapabilityDeniedError(
            "Production parse capability denied: canonical asset-authority "
            "contract not yet delivered. "
            "Owner needed: S10/A03 asset-authority or designated S15 integration owner."
        )


class TestAuthorizer(Authorizer):
    """Test-only authorizer that issues synthetic capabilities.

    Supplies authority through dependency injection for isolated tests.
    NEVER used in production — the ``ProductionAuthorizer`` is the default.

    Synthetic capabilities have no real cloud binding; they exercise the
    manifest validation, output checking and lifecycle paths.
    """

    def __init__(self, *, expiry_seconds: float = 3600.0):
        self._expiry_seconds = expiry_seconds
        self._issued: list[dict[str, Any]] = []
        self._invocations: dict[tuple[str, str], str] = {}

    @property
    def issued_grants(self) -> list[dict[str, Any]]:
        return list(self._issued)

    def issue_fetch(
        self,
        asset_id: str,
        attempt_id: str,
        *,
        approved_registries: list[str] | None = None,
        approved_source_prefixes: list[str] | None = None,
    ) -> FetchCapability:
        import uuid

        cap = FetchCapability(
            invocation_id=self._invocations.setdefault((asset_id, attempt_id), str(uuid.uuid4())),
            asset_id=asset_id,
            attempt_id=attempt_id,
            approved_registries=approved_registries or ["https://registry.npmjs.org"],
            approved_source_prefixes=approved_source_prefixes or [],
            expires_at=time.time() + self._expiry_seconds,
        )
        self._issued.append({"type": "fetch", **cap.to_dict()})
        return cap

    def issue_publish(
        self,
        asset_id: str,
        attempt_id: str,
        *,
        allowed_prefixes: list[str] | None = None,
    ) -> PublishCapability:
        import uuid

        cap = PublishCapability(
            invocation_id=self._invocations.setdefault((asset_id, attempt_id), str(uuid.uuid4())),
            asset_id=asset_id,
            attempt_id=attempt_id,
            allowed_prefixes=allowed_prefixes or [f"scip/{asset_id}/"],
            expires_at=time.time() + self._expiry_seconds,
        )
        self._issued.append({"type": "publish", **cap.to_dict()})
        return cap

    def issue_parse(
        self,
        asset_id: str,
        attempt_id: str,
        *,
        source_dir: str = "",
        output_dir: str = "",
        allowed_languages: list[str] | None = None,
    ) -> ParseCapability:
        import uuid

        cap = ParseCapability(
            invocation_id=self._invocations.setdefault((asset_id, attempt_id), str(uuid.uuid4())),
            asset_id=asset_id,
            attempt_id=attempt_id,
            source_dir=source_dir,
            output_dir=output_dir,
            allowed_languages=allowed_languages or [],
        )
        self._issued.append({"type": "parse", **cap.to_dict()})
        return cap
