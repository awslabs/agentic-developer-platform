"""Credential resolver — walks user → team → org → domain-app scope chain.

Issue #440: Credential scope relaxation

When an agent requests a credential for (user=alice, service=github), the
resolver checks scopes in order:

    1. user     — a credential owned by alice
    2. team     — a credential shared by alice's team
    3. org      — a tenant-wide credential
    4. domain-app — a credential installed by a domain app for this tenant

The first matching credential wins.  Credentials with ``strict=True`` are
**only** returned if the caller's resolved scope exactly matches that
credential's owner scope — they skip the fallback chain.

Cross-scope protection
----------------------
If the caller passes a ``scope_hint`` (e.g. "user") and the resolver finds a
credential at a wider scope (e.g. "org"), it raises ``ScopeEscalationError``.
This prevents an agent acting on behalf of a user from silently acquiring
tenant-wide credentials.

Usage
-----
    resolver = CredentialResolver(db_session)
    cred = await resolver.resolve(
        org_id="org-123",
        service="github",
        label="default",
        user_id="user-abc",
        team_id="team-xyz",
        scope_hint="user",   # optional; raises if resolved scope is wider
    )
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.vault import UserCredential

logger = logging.getLogger(__name__)

# Resolution order: narrowest scope first.
_SCOPE_ORDER = ("user", "team", "org", "domain_app")


class CredentialNotFoundError(Exception):
    """No matching credential found after exhausting all scopes."""


class ScopeEscalationError(Exception):
    """Resolved credential is at a wider scope than the caller's scope_hint.

    Raised when scope_hint is provided and the resolved credential's owner
    scope is broader (i.e. further right in _SCOPE_ORDER) than the hint.
    This is a safety rail against accidental privilege escalation.
    """


class CredentialResolver:
    """Async credential resolver backed by a SQLAlchemy AsyncSession.

    Parameters
    ----------
    session : AsyncSession
        The database session to use for queries.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self,
        *,
        org_id: str,
        service: str,
        label: str | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
        domain_app_id: str | None = None,
        scope_hint: str | None = None,
    ) -> UserCredential:
        """Find the best matching credential for the given context.

        Parameters
        ----------
        org_id : str
            Tenant identifier — always required for isolation.
        service : str
            The external service name, e.g. "github".
        label : str, optional
            If provided, only credentials with this label are considered.
            If None, returns the first match in the resolved scope.
        user_id : str, optional
            The user whose credential to look up first.
        team_id : str, optional
            The team to fall back to if no user credential exists.
        domain_app_id : str, optional
            Domain-app ID to fall back to (after org-scoped check).
        scope_hint : str, optional
            One of "user", "team", "org", "domain_app".  If the resolved
            credential's scope is wider than this hint, raises
            ``ScopeEscalationError``.

        Returns
        -------
        UserCredential
            The resolved credential row.

        Raises
        ------
        CredentialNotFoundError
            No credential found after checking all scopes.
        ScopeEscalationError
            Resolved scope is wider than scope_hint.
        ValueError
            scope_hint is not a recognised scope name.
        """
        if scope_hint is not None and scope_hint not in _SCOPE_ORDER:
            raise ValueError(f"scope_hint must be one of {_SCOPE_ORDER}; got {scope_hint!r}")

        # Determine the narrowest (most specific) scope the caller is acting at.
        # A strict credential is only returned when its scope exactly matches
        # this caller scope — i.e. the caller is not doing a fallback walk.
        if user_id is not None:
            caller_scope = "user"
        elif team_id is not None:
            caller_scope = "team"
        elif domain_app_id is not None:
            caller_scope = "domain_app"
        else:
            caller_scope = "org"

        # Collect candidates to check in order.
        candidates: list[tuple[str, UserCredential | None]] = []

        if user_id is not None:
            cred = await self._find(org_id=org_id, service=service, label=label, user_id=user_id)
            candidates.append(("user", cred))

        if team_id is not None:
            cred = await self._find(org_id=org_id, service=service, label=label, team_id=team_id)
            candidates.append(("team", cred))

        # Org-scoped: all three owner columns NULL.
        cred = await self._find(org_id=org_id, service=service, label=label, org_scoped=True)
        candidates.append(("org", cred))

        if domain_app_id is not None:
            cred = await self._find(org_id=org_id, service=service, label=label, domain_app_id=domain_app_id)
            candidates.append(("domain_app", cred))

        # Walk in order, pick first non-None.
        for scope, cred in candidates:
            if cred is None:
                continue

            # A strict credential is only returned when the caller's narrowest
            # scope matches the credential's owner scope exactly.  This prevents
            # an agent that requested "find me a credential for user X" from
            # silently acquiring a high-sensitivity team or org credential via
            # the fallback chain.
            if cred.strict and cred.owner_scope != caller_scope:
                logger.debug(
                    "Skipping strict credential %s (owner_scope=%s, caller_scope=%s)",
                    cred.id,
                    cred.owner_scope,
                    caller_scope,
                )
                continue

            # Cross-scope safety rail.
            if scope_hint is not None and _scope_width(scope) > _scope_width(scope_hint):
                raise ScopeEscalationError(
                    f"Credential found at scope {scope!r} but caller's scope_hint is {scope_hint!r}. "
                    "Pass a wider scope_hint or remove it to allow fallback."
                )

            logger.debug("Resolved credential %s at scope %s for service %s", cred.id, scope, service)
            return cred

        raise CredentialNotFoundError(f"No credential found for service={service!r}, label={label!r}, org_id={org_id!r} after checking all scopes.")

    # ------------------------------------------------------------------
    # Private query helpers
    # ------------------------------------------------------------------

    async def _find(
        self,
        *,
        org_id: str,
        service: str,
        label: str | None,
        user_id: str | None = None,
        team_id: str | None = None,
        domain_app_id: str | None = None,
        org_scoped: bool = False,
    ) -> UserCredential | None:
        """Query for a single credential matching the given owner scope."""
        stmt = select(UserCredential).where(
            UserCredential.org_id == org_id,
            UserCredential.service == service,
        )

        if label is not None:
            stmt = stmt.where(UserCredential.label == label)

        if user_id is not None:
            stmt = stmt.where(UserCredential.user_id == user_id)
        elif team_id is not None:
            stmt = stmt.where(UserCredential.team_id == team_id)
        elif domain_app_id is not None:
            stmt = stmt.where(UserCredential.domain_app_id == domain_app_id)
        elif org_scoped:
            stmt = stmt.where(
                UserCredential.user_id.is_(None),
                UserCredential.team_id.is_(None),
                UserCredential.domain_app_id.is_(None),
            )

        stmt = stmt.limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


def _scope_width(scope: str) -> int:
    """Return the numeric width of a scope (higher = broader access)."""
    return _SCOPE_ORDER.index(scope)
