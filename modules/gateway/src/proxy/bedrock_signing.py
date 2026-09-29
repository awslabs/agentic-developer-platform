"""Cross-account credentials for a routed Bedrock call. The enforcement half of R3.

Issue #4744 (#4692 · R3 · routing enforcement), per the design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §2.3, §2.4, §2.5,
§2.6.

R2 (#4743) resolved *where* a call should go and changed nothing. This module makes
that answer real: given a :class:`~src.proxy.bedrock_routing.BedrockTarget` from a
non-platform rung, it produces the destination account's temporary credentials, which
:class:`~src.pool.simple_pool.AsyncBedrockClient` then signs with.

**There is no fallback to the platform account in this module, and ruling 1 forbids
adding one.** Every failure path raises
:class:`~src.proxy.bedrock_routing_errors.BedrockAccountUnavailableError`. The ruling
forbids such a branch *existing*, not merely being taken, because the branch is
self-triggering: it fires exactly when the mapping matters most, and it fires with a
200 response, so a misdirected bill is invisible until someone reads a statement. A
misdirected bill is also unrecoverable in a way downtime is not — the money is already
spent on someone else's account. That asymmetry is the whole reason fail-closed was
ruled, and `tests/proxy/test_bedrock_enforcement.py` asserts the absence of the branch
against this module's source, not just its behaviour.

Two structural decisions carry the security of this file.

**1. The cache key is the full identity tuple (§2.3), not the role ARN.**

The dead pool ``STSClient`` keys on ``cache_key = account_config.role_arn`` alone
(``src/pool/sts_client.py:52``). That is safe only because its ``PoolService`` has a
static account list and no principal dimension at all. The moment the target is
principal-dependent — which is what this issue does — a role-ARN key becomes a
cross-tenant credential cache: two orgs whose admins connect the *same* role ARN (a
shared client account; entirely legitimate) with **different ExternalIds** would share
one entry, and the second org's calls would be signed with credentials minted under the
first org's ExternalId and session tags. So:

    (org_id, role_arn, external_id, region, rung, destination_updated_at)

``org_id`` leads for the reason the budget tables key ``org_id`` first (#4620): it makes
cross-tenant reuse *unrepresentable* rather than merely untested. ``external_id`` is in
the key because it changes the minted session's identity. ``rung`` is there so a
user-rung and an org-rung hit that happen to name the same role do not collide. The
general rule, which is what makes this list auditable rather than arbitrary: **every
input to the AssumeRole call belongs in the key.** If you add an argument to the
assume, add it here in the same commit.

**2. ``destination_updated_at`` is in the key, and that is what implements
eviction-on-change (§2.3, §8.3).**

§8.3 is blunt about the stakes: without eviction on mapping change, rolling back a bad
mapping is delayed by up to the credential TTL (3600s), "which is unacceptable for a
mis-billing incident". The obvious implementation is an explicit hook the authoring API
calls — but **R4 owns the authoring API and it does not exist yet**, so an
``invalidate()`` that nothing calls would satisfy the requirement on paper while a
mis-billing rollback still took an hour.

Folding the destination row's ``updated_at`` into the key makes both kinds of change
self-evicting, with no hook to wire and none to forget:

- Re-pointing a scope at a different destination changes ``destination_id``, hence a
  different row, hence a different ``role_arn``/``updated_at`` — different key.
- Editing a destination in place (new role ARN, re-verify) bumps ``updated_at`` via the
  model's ``onupdate`` — different key.

The stale entry is not deleted, it is simply never read again, and the LRU bound
reclaims it. :func:`invalidate_destination` ships anyway for R4 to call on delete, so
freeing memory promptly is possible — but correctness does not depend on R4 remembering
to call it, which is the point.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.proxy.bedrock_routing import BedrockTarget
from src.proxy.bedrock_routing_errors import (
    REASON_ACCOUNT_UNLINKED,
    REASON_ASSUME_ROLE_FAILED,
    BedrockAccountUnavailableError,
    BedrockUnavailableScope,
)
from src.shared.config import get_settings
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.vault import UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger("bedrockgateway.proxy.signing")


@dataclass(frozen=True)
class DestinationCredentials:
    """Temporary credentials for a destination account, plus when they expire.

    Frozen for the same reason :class:`BedrockTarget` is: these are handed to a boto3
    client and cached, and a mutation after caching would mean the entry no longer
    describes the session it is keyed under.

    ``expiration`` is kept so the cache can refresh *before* expiry rather than
    expiring into a failed call (§2.3). Under fail-closed an expired credential is not
    a retry, it is an outage, so the margin is load-bearing.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime
    region: str

    def is_expired(self, margin_seconds: int = 0) -> bool:
        """Expired, or close enough that we should refresh now?

        Delegates to the pool's existing :class:`AssumedRoleCredentials` logic rather
        than re-deriving it — §2.3 says to reuse that pattern verbatim, and two
        implementations of "is it time to refresh" is how one of them drifts.
        """
        from src.pool.models import AssumedRoleCredentials

        return AssumedRoleCredentials(
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            session_token=self.session_token,
            expiration=self.expiration,
        ).is_expired(margin_seconds=margin_seconds)

    def as_boto3_kwargs(self) -> dict[str, str]:
        """The three kwargs ``boto3.client`` wants. Never logged, never persisted."""
        return {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "aws_session_token": self.session_token,
        }


@dataclass(frozen=True)
class _AssumeInputs:
    """Everything the AssumeRole call needs, resolved from the destination row.

    Exists as its own type so the cache key can be derived from it mechanically
    (:meth:`cache_key`). The alternative — building the key at the call site from
    loose locals — is how a newly added assume argument silently fails to reach the
    key, which is precisely the §2.3 bug this design is defending against.
    """

    org_id: str
    role_arn: str
    external_id: str | None
    region: str
    rung: str
    destination_updated_at: datetime | None
    account_id: str
    user_id: str

    def cache_key(self) -> tuple:
        """The §2.3 full identity tuple. ``org_id`` first, by design.

        ``user_id`` is deliberately **absent**. It is sent as a session *tag* for
        CloudTrail attribution but is not an authorization input on the v2 routing
        template (#4742 dropped the ``adp:user_id`` trust-policy pin precisely so a
        destination could serve a whole team), so two members of one team mapped to
        one destination may legitimately share an entry — which is the only reason a
        team destination is cacheable at all.

        The consequence is explicit and accepted: a shared entry carries the tag of
        whoever minted it, so CloudTrail attributes cached calls to that member. That
        is an attribution coarsening on the *destination's* audit trail, not on ADP's
        — ADP's own per-user attribution comes from ``usage_logs``, which is written
        per request and is unaffected. §2.3's rule that per-user-tagged (v1) roles must
        not be team destinations is what keeps this from becoming an authorization
        problem: such roles are never ``routing_capable``, so they never reach here.
        """
        return (
            self.org_id,
            self.role_arn,
            self.external_id,
            self.region,
            self.rung,
            self.destination_updated_at,
        )


class DestinationCredentialCache:
    """LRU-bounded cache of assumed destination credentials, keyed per §2.3.

    Bounded because the key space is now principal-dependent: the dead pool cache was
    an unbounded dict with no eviction (``sts_client.py:33``), which is fine for two
    static accounts and a memory-growth plus stale-entry problem here.

    Not thread-locked, but **serialized per key by an asyncio lock**: the gateway runs
    one event loop per worker, and without the per-key lock a burst of concurrent
    first-calls for one destination would each miss the cache and fire their own
    AssumeRole — a thundering herd against an STS rate limit, which under fail-closed
    turns into throttled 502s for the very principal the mapping was authored for.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        settings = get_settings()
        self._max_entries = max_entries if max_entries is not None else settings.bedrock_routing_credential_cache_size
        self._entries: OrderedDict[tuple, DestinationCredentials] = OrderedDict()
        self._locks: dict[tuple, asyncio.Lock] = {}

    def get(self, key: tuple, *, margin_seconds: int) -> DestinationCredentials | None:
        """Return a live entry, or None if absent or due for refresh."""
        creds = self._entries.get(key)
        if creds is None:
            return None
        if creds.is_expired(margin_seconds=margin_seconds):
            # Drop it rather than hand back something about to fail mid-call.
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return creds

    def put(self, key: tuple, creds: DestinationCredentials) -> None:
        self._entries[key] = creds
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def lock_for(self, key: tuple) -> asyncio.Lock:
        """The per-key lock that collapses a first-call burst into one assume."""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
            # The lock map shares the credential bound so it cannot grow without
            # limit either. Dropping a lock that a waiter still holds a reference to
            # is safe: the waiter keeps its own reference, so mutual exclusion for
            # the in-flight assume is unaffected.
            while len(self._locks) > self._max_entries:
                self._locks.pop(next(iter(self._locks)))
        return lock

    def invalidate_destination(self, role_arn: str) -> int:
        """Forget every entry for a role ARN. Returns how many were dropped.

        For R4 to call when a destination is deleted or re-verified. Correctness does
        **not** depend on it — ``destination_updated_at`` in the key already makes
        edits self-evicting (see the module docstring) — so this is a promptness and
        memory-hygiene tool, and a delete hook that also wants the credentials gone
        immediately rather than merely unreachable.
        """
        doomed = [key for key in self._entries if key[1] == role_arn]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()

    def __len__(self) -> int:
        return len(self._entries)


class BedrockDestinationSigner:
    """Turns a resolved :class:`BedrockTarget` into destination-account credentials.

    One instance per process (see :data:`bedrock_destination_signer`), because the
    credential cache lives on it and a per-request instance would assume a fresh role
    on every single call — an STS round trip per model call, and a rate-limit incident
    at any real volume.
    """

    def __init__(
        self,
        cache: DestinationCredentialCache | None = None,
        secrets_manager: SecretsManagerHelper | None = None,
    ) -> None:
        self._cache = cache if cache is not None else DestinationCredentialCache()
        self._secrets_manager = secrets_manager

    @property
    def cache(self) -> DestinationCredentialCache:
        return self._cache

    def _sm(self) -> SecretsManagerHelper:
        """Lazily build the Secrets Manager helper.

        Lazy so that importing this module — which the proxy does unconditionally —
        never constructs a boto3 client. An install with enforcement off must not pay
        for, or fail on, a client it will never use.
        """
        if self._secrets_manager is None:
            self._secrets_manager = SecretsManagerHelper(region_name=get_settings().aws_region)
        return self._secrets_manager

    async def _load_destination(self, session: AsyncSession, target: BedrockTarget) -> BedrockDestinationRegistry:
        """Fetch the destination row, or fail closed as ``account_unlinked``.

        The resolver already filtered on ``is_usable_for_routing``, so a miss here
        means the row was deleted between resolution and signing — which §8.3 names
        as the dangerous rollback ordering (deleting a *connection* a mapping still
        references is an outage, unlike deleting a mapping). Failing with a named
        account and a clear cause is exactly what makes that outage diagnosable.
        """
        scope = self._scope_of(target)
        destination = (
            await session.execute(select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == target.destination_id))
        ).scalar_one_or_none()

        if destination is None:
            logger.error(
                "Bedrock routing destination row missing at signing time",
                extra={"destination_id": target.destination_id, "rung": target.rung},
            )
            raise BedrockAccountUnavailableError(
                reason=REASON_ACCOUNT_UNLINKED,
                account_id=target.account_id or "unknown",
                scope=scope,
            )

        # Re-check usability at signing time rather than trusting the resolver's read.
        # The resolver skips an unusable destination and falls through (§4.4), so this
        # can only fire if the row changed in between — and under fail-closed, signing
        # with a destination that is no longer verified is the one thing worse than
        # failing the call.
        if not destination.is_usable_for_routing:
            logger.error(
                "Bedrock routing destination became unusable before signing",
                extra={
                    "destination_id": destination.id,
                    "routing_capable": destination.routing_capable,
                    "verified": destination.verified_at is not None,
                },
            )
            raise BedrockAccountUnavailableError(
                reason=REASON_ACCOUNT_UNLINKED,
                account_id=destination.account_id,
                scope=scope,
            )

        return destination

    @staticmethod
    def _scope_of(target: BedrockTarget) -> BedrockUnavailableScope:
        """The rung, narrowed to the three that can be routed.

        The platform rung never reaches this module (callers check
        ``target.is_platform`` first), so this only ever sees the three routable
        rungs. The cast is a type narrowing, not a behavioural default.
        """
        return target.rung  # type: ignore[return-value]

    async def _resolve_external_id(
        self,
        session: AsyncSession,
        destination: BedrockDestinationRegistry,
    ) -> str | None:
        """Fetch the destination's ExternalId from Secrets Manager, if it has one.

        The registry stores ``account_id`` + ``role_arn``, but the ExternalId lives in
        the SM payload behind ``credential_id`` — the shape ``connect_start`` writes
        (``role_arn`` / ``external_id`` / ``account_id`` / ``default_region``). It is
        deliberately not copied onto the registry row: it is a shared secret for
        confused-deputy protection, and duplicating it into a second table would double
        the number of places it can leak from.

        Platform registrations use their server-issued destination ID as the
        trust binding (``adp-platform:<destination-id>``). Their IAM role must
        require that exact ExternalId. Personal connections continue to use
        the separately verified trust ID stored with the credential.
        """
        # Platform registration issues the stable trust ID; personal connections
        # continue to use their separately verified credential trust binding.
        if destination.is_platform_registered and destination.owner_org_id is None and not destination.credential_id:
            return f"adp-platform:{destination.id}"

        if not destination.credential_id:
            logger.warning(
                "Bedrock routing destination has no linked connection; assuming without ExternalId",
                extra={"destination_id": destination.id, "account_id": destination.account_id},
            )
            return None

        credential = (await session.execute(select(UserCredential).where(UserCredential.id == destination.credential_id))).scalar_one_or_none()
        if credential is None:
            logger.error(
                "Bedrock routing destination references a missing credential row",
                extra={"destination_id": destination.id, "credential_id": destination.credential_id},
            )
            raise BedrockAccountUnavailableError(
                reason=REASON_ACCOUNT_UNLINKED,
                account_id=destination.account_id,
                scope="org",
            )

        try:
            payload = json.loads(await asyncio.to_thread(self._sm().get_secret, credential.secret_arn))
        except Exception as exc:  # noqa: BLE001 - any SM/parse failure is one cause: no usable credential
            # Deliberately broad: SM throttling, a deleted secret and malformed JSON
            # are different causes with the *same* remediation (re-validate the
            # connection), and the exception text must not reach the user — it can
            # carry the secret ARN.
            logger.error(
                "Bedrock routing could not read the destination credential secret",
                extra={"destination_id": destination.id, "error": str(exc)},
            )
            raise BedrockAccountUnavailableError(
                reason=REASON_ACCOUNT_UNLINKED,
                account_id=destination.account_id,
                scope="org",
            ) from exc

        return payload.get("external_id")

    async def get_credentials(
        self,
        session: AsyncSession,
        target: BedrockTarget,
        *,
        user_id: str,
    ) -> DestinationCredentials:
        """The entry point: credentials for ``target``, or a fail-closed error.

        Args:
            session: Async session bound to the gateway DB.
            target: A **non-platform** resolved target. Callers check
                ``target.is_platform`` and use the ambient client for that rung; a
                platform target here would be a caller bug, so it raises rather than
                quietly assuming something.
            user_id: Canonical ``users.id``, sent as a session tag for CloudTrail
                attribution in the destination account. Not part of the cache key —
                see :meth:`_AssumeInputs.cache_key` for why, and for what that costs.

        Raises:
            BedrockAccountUnavailableError: On every failure. There is no path out of
                this method that returns platform-account credentials.
        """
        if target.is_platform:
            raise ValueError("get_credentials must not be called for the platform rung; use the ambient client")

        settings = get_settings()
        destination = await self._load_destination(session, target)
        external_id = await self._resolve_external_id(session, destination)

        inputs = _AssumeInputs(
            # The destination's OWN tenant, not the caller's. A platform-registered
            # destination has no owner, so it keys on a literal that cannot collide
            # with any org id — using the caller's org_id instead would give every
            # tenant its own entry for one shared destination, defeating the cache,
            # while using an empty string would make "platform-registered" and
            # "unknown tenant" the same key.
            org_id=destination.owner_org_id or "__platform__",
            role_arn=destination.role_arn,
            external_id=external_id,
            region=destination.region,
            rung=target.rung,
            destination_updated_at=destination.updated_at,
            account_id=destination.account_id,
            user_id=user_id,
        )

        key = inputs.cache_key()
        margin = settings.bedrock_routing_credential_refresh_margin_seconds

        cached = self._cache.get(key, margin_seconds=margin)
        if cached is not None:
            return cached

        async with self._cache.lock_for(key):
            # Re-check under the lock: whoever held it may have just populated the
            # entry, and without this every waiter in a first-call burst still fires
            # its own AssumeRole — the herd the lock exists to prevent.
            cached = self._cache.get(key, margin_seconds=margin)
            if cached is not None:
                return cached

            creds = await self._assume(inputs, settings.bedrock_routing_session_duration_seconds)
            self._cache.put(key, creds)
            return creds

    async def _assume(self, inputs: _AssumeInputs, duration_seconds: int) -> DestinationCredentials:
        """Perform the AssumeRole. Any failure is ``assume_role_failed``.

        Reuses ``src/internal/sts_assume_service.assume_role`` — the same helper the
        vault delivery path uses — rather than adding a third STS wrapper to the
        codebase. It already sends session tags for CloudTrail attribution and
        ExternalId for confused-deputy protection, which are exactly the two
        properties a routed assume needs.
        """
        try:
            result = await asyncio.to_thread(
                assume_role,
                role_arn=inputs.role_arn,
                external_id=inputs.external_id,
                session_duration_seconds=duration_seconds,
                default_region=inputs.region,
                user_id=inputs.user_id,
                # Identifies the caller in the destination's CloudTrail as the
                # gateway's routing path, distinguishable from an agent's vault
                # delivery assume against the same role.
                agent_id="bedrock-routing",
                task_id=inputs.rung,
                label="bedrock-routing",
            )
        except STSAssumeError as exc:
            # role_arn is logged (server-side, for diagnosis) and deliberately NOT
            # passed to the error — the §2.6 redaction rule, and the class has no
            # parameter for it anyway.
            logger.error(
                "Bedrock routing assume-role failed",
                extra={
                    "account_id": inputs.account_id,
                    "role_arn": inputs.role_arn,
                    "rung": inputs.rung,
                    "sts_code": exc.code,
                },
            )
            raise BedrockAccountUnavailableError(
                reason=REASON_ASSUME_ROLE_FAILED,
                account_id=inputs.account_id,
                scope=inputs.rung,  # type: ignore[arg-type]
            ) from exc

        logger.info(
            "Bedrock routing assumed destination role",
            extra={"account_id": inputs.account_id, "rung": inputs.rung, "region": inputs.region},
        )
        return DestinationCredentials(
            access_key_id=result.access_key_id,
            secret_access_key=result.secret_access_key,
            session_token=result.session_token,
            expiration=_parse_expiration(result.expiration),
            region=result.region,
        )


def _parse_expiration(raw: str) -> datetime:
    """Parse the ISO-8601 expiration the STS helper returns.

    ``AssumeRoleResult.expiration`` is a string (the helper ISO-formats boto3's
    datetime), while the refresh check needs a datetime. If it is ever unparseable,
    treat the session as already expired — ``datetime.min`` in UTC — so the entry is
    never reused. The alternative (assume a full lifetime) would cache a credential
    whose real expiry we do not know, and hand it out until a call fails mid-flight.
    """
    from datetime import UTC

    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning("Could not parse assumed-role expiration; treating as expired", extra={"raw": str(raw)[:64]})
        return datetime.min.replace(tzinfo=UTC)


# Process-wide instance, mirroring `bedrock_routing_resolver`. The credential cache
# lives on it, so sharing one instance is what makes the cache work at all — a
# per-request signer would assume a role on every model call.
bedrock_destination_signer = BedrockDestinationSigner()
