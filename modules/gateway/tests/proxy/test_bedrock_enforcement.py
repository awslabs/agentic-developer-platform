"""Tests for Bedrock routing ENFORCEMENT — the call is actually signed for the account.

Issue #4744 (#4692 · R3 · routing enforcement), design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §2.1, §2.3, §2.4,
§2.5, §2.6, §5.1, §5.2.

R2 (#4743) resolved *where* a call should go and deliberately changed nothing —
``tests/proxy/test_bedrock_routing_shadow.py`` is the proof of that inertness. This
file covers the opposite claim: that the answer is now acted on, that it is acted on
for the *right* principal, and that when it cannot be acted on the call **fails**
rather than quietly billing the platform account.

Six properties, each of which is the difference between correct billing and charging
the wrong customer once the flag is on:

  1. ``TestSigningReachesTheDestination``  — a mapped principal's client is built with
     the destination's credentials; an unmapped one is byte-identical to main (§2.1)
  2. ``TestCredentialCacheIsolation``      — the §2.3 killer: two principals never
     share an entry, and a mapping or destination edit self-evicts
  3. ``TestFailClosed``                    — every cause raises its own reason code
     naming the account, and **no code path exists** that falls back (ruling 1)
  4. ``TestTimeoutsArePreserved``          — both ``Config`` timeouts survive on a
     routed client (§2.4, the 60s-default latency bug)
  5. ``TestErrorClassDiscrimination``      — ``AccessDeniedException`` on a routed call
     is ``model_not_enabled``, transport failures keep main's behaviour (§5.1, §5.2)
  6. ``TestRoutingAlwaysEnforced``        — saved mappings apply without opt-in;
     missing or obsolete false flags never redirect spend to the platform
  7. ``TestRedaction``                     — the account id is present, the role ARN and
     ExternalId are structurally absent (§2.6)

SQLite in-memory with the ORM metadata, matching ``test_bedrock_routing_resolver.py``.
Every STS call is stubbed: what is under test is which credentials reach the client and
which error reaches the caller, never AWS itself.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.sts_assume_service import AssumeRoleResult, STSAssumeError
from src.proxy.bedrock_enforcement import (
    resolve_routing_decision,
)
from src.proxy.bedrock_routing import BedrockTarget
from src.proxy.bedrock_routing_errors import (
    ALL_REASONS,
    REASON_ACCOUNT_UNLINKED,
    REASON_ASSUME_ROLE_FAILED,
    REASON_MODEL_NOT_ENABLED,
    BedrockAccountUnavailableError,
)
from src.proxy.bedrock_signing import (
    BedrockDestinationSigner,
    DestinationCredentialCache,
    DestinationCredentials,
    _AssumeInputs,
    bedrock_destination_signer,
)
from src.proxy.exceptions import BedrockInvocationError
from src.shared.models.base import Base
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext

# R2's docstring/comment-stripped source reader. Imported rather than re-implemented:
# the structural "this module cannot reach X" assertions in both files must agree on
# what counts as source, or one of them silently weakens.
from tests.proxy.test_bedrock_routing_shadow import _executable_source

PLATFORM_ACCOUNT = "999988887777"
MAPPED_ACCOUNT = "111111111111"
OTHER_ACCOUNT = "222222222222"

ORG_ID = "org-acme"
OTHER_ORG_ID = "org-globex"
TEAM_ID = "team-eng"
CANONICAL_USER_ID = "user-canonical-1"
COGNITO_SUB = "cognito-sub-abc"

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:999988887777:secret:adp/aws/acme-AbCdEf"
EXTERNAL_ID = "adp-external-acme"

MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"


# ============================================================================
# Fixtures and builders
# ============================================================================


@pytest.fixture
def routing_environment(monkeypatch):
    """Configure the platform account used by the test deployment.

    Set through the environment rather than by patching ``get_settings`` so the real
    ``BG_`` prefixed parsing runs — a flag the deployment sets via configmap that the
    code reads under a different name is the #4511 inert-config class, and this feature
    is one whose inert state looks exactly like success.
    """
    monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", PLATFORM_ACCOUNT)


@pytest.fixture
async def session_factory():
    """In-memory database with the routing tables, `users`, `organizations`, vault."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Clear the process-wide resolver and signer caches around every test.

    Both are module-level singletons by design (the caches are what make the hot
    path cheap), which means state leaks between tests unless it is cleared. Doing it
    in an autouse fixture rather than per test is deliberate: a test that forgot would
    not fail, it would pass for the wrong reason.
    """
    from src.proxy.bedrock_routing import bedrock_routing_resolver
    from src.proxy.bedrock_signing import bedrock_destination_signer

    def _clear():
        bedrock_routing_resolver._mappings_exist_cache = None
        bedrock_destination_signer.cache.clear()

    _clear()
    yield
    _clear()


def _context(**overrides) -> TokenContext:
    """A JWT-path context: `user_id` is a COGNITO SUB, as it is in production."""
    values = {
        "user_id": COGNITO_SUB,
        "org_id": ORG_ID,
        "team_id": TEAM_ID,
        "department_id": "dept-1",
        "account_type": "human",
        "is_admin": False,
        "expires_at": datetime.now(UTC) + timedelta(hours=12),
        **overrides,
    }
    return TokenContext(**values)


def _destination(
    dest_id: str = "dest-1",
    account_id: str = MAPPED_ACCOUNT,
    *,
    routing_capable: bool = True,
    verified: bool = True,
    region: str = "us-east-1",
    owner_org_id: str | None = ORG_ID,
    credential_id: str | None = "cred-1",
    role_arn: str | None = None,
    updated_at: datetime | None = None,
) -> BedrockDestinationRegistry:
    return BedrockDestinationRegistry(
        id=dest_id,
        account_id=account_id,
        role_arn=role_arn or f"arn:aws:iam::{account_id}:role/adp-bedrock-routing",
        credential_id=credential_id,
        owner_org_id=owner_org_id,
        is_platform_registered=owner_org_id is None,
        routing_capable=routing_capable,
        verified_at=datetime(2026, 9, 1, tzinfo=UTC) if verified else None,
        region=region,
        label=dest_id,
        registered_by_user_id="user-admin",
        updated_at=updated_at or datetime(2026, 9, 1, tzinfo=UTC),
    )


def _credential(cred_id: str = "cred-1", *, org_id: str = ORG_ID, secret_arn: str = SECRET_ARN) -> UserCredential:
    return UserCredential(
        id=cred_id,
        org_id=org_id,
        service="aws",
        credential_type="role",
        label="aws-account",
        secret_arn=secret_arn,
    )


def _mapping(map_id: str, destination_id: str, **scope) -> BedrockAccountMapping:
    return BedrockAccountMapping(id=map_id, destination_id=destination_id, authored_by_user_id="user-admin", **scope)


async def _seed(session_factory, *rows):
    async with session_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


async def _seed_enforced_org_mapping(
    session_factory,
    *,
    scope: str = "org",
    destination: BedrockDestinationRegistry | None = None,
    org_id: str = ORG_ID,
):
    """The full happy-path fixture: ordinary org, usable destination, matching mapping."""
    destination = destination if destination is not None else _destination(owner_org_id=org_id)
    scopes = {
        "org": {"scope_type": "org", "scope_id_org": org_id},
        "team": {"scope_type": "team", "scope_id_org": org_id, "scope_id_team": TEAM_ID},
        "user": {"scope_type": "user", "scope_id_user": CANONICAL_USER_ID},
    }
    await _seed(
        session_factory,
        Organization(id=org_id, name=f"name-{org_id}", settings={}),
        User(id=CANONICAL_USER_ID, org_id=org_id, team_id=TEAM_ID, email="a@example.com", cognito_sub=COGNITO_SUB),
        _credential(destination.credential_id or "cred-unused", org_id=org_id),
        destination,
        _mapping(f"map-{scope}", destination.id, **scopes[scope]),
    )
    return destination


def _assume_result(access_key_id: str = "ASIAROUTED", *, region: str = "us-east-1", ttl_seconds: int = 3600) -> AssumeRoleResult:
    return AssumeRoleResult(
        access_key_id=access_key_id,
        secret_access_key="routed-secret",
        session_token="routed-token",
        expiration=(datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
        region=region,
        profile_name="adp-bedrock-routing",
    )


def _credentials(access_key_id: str = "ASIAROUTED", *, region: str = "us-east-1", ttl_seconds: int = 3600) -> DestinationCredentials:
    return DestinationCredentials(
        access_key_id=access_key_id,
        secret_access_key="routed-secret",
        session_token="routed-token",
        expiration=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        region=region,
    )


def _secrets_manager(external_id: str | None = EXTERNAL_ID, *, raises: Exception | None = None) -> MagicMock:
    """A stand-in for ``SecretsManagerHelper`` returning the connect-flow payload shape."""
    import json

    helper = MagicMock()
    if raises is not None:
        helper.get_secret.side_effect = raises
    else:
        payload = {"role_arn": "arn:aws:iam::111111111111:role/x", "account_id": MAPPED_ACCOUNT, "default_region": "us-east-1"}
        if external_id is not None:
            payload["external_id"] = external_id
        helper.get_secret.return_value = json.dumps(payload)
    return helper


def _signer(secrets_manager: MagicMock | None = None) -> BedrockDestinationSigner:
    """A signer with its own cache, so cache tests never see another test's entries."""
    return BedrockDestinationSigner(cache=DestinationCredentialCache(), secrets_manager=secrets_manager or _secrets_manager())


def _patch_routing_session(session_factory):
    """Point the enforcement path's own session factory at the test database.

    ``resolve_routing_decision`` opens its own session (it is called from the invoke
    path, which has none), so the seam to patch is ``get_session_factory`` inside
    ``src.shared.database`` — the module it imports locally.
    """
    return patch("src.shared.database.get_session_factory", lambda: session_factory)


def _patch_assume(result_or_error=None):
    """Patch the STS helper the signer calls. Never touches AWS."""
    result = result_or_error if result_or_error is not None else _assume_result()
    mock = MagicMock(side_effect=result if isinstance(result, Exception) else None, return_value=None if isinstance(result, Exception) else result)
    return patch("src.proxy.bedrock_signing.assume_role", mock), mock


# ============================================================================
# 1. Signing reaches the destination — §2.1
# ============================================================================


class TestSigningReachesTheDestination:
    """The R3 claim in one sentence: a mapped call is signed for the mapped account.

    Asserted at the pool boundary — what ``get_client`` was *handed* — because that is
    the last point before boto3 where the decision is still inspectable, and because
    "the resolver returned the right account" (R2's tests) is not the same claim as
    "the request was signed with it".
    """

    @pytest.mark.asyncio
    async def test_a_mapped_principal_is_signed_with_destination_credentials(self, session_factory, routing_environment):
        await _seed_enforced_org_mapping(session_factory)
        assume, _ = _patch_assume()

        with _patch_routing_session(session_factory), assume, patch.object(bedrock_destination_signer, "_secrets_manager", _secrets_manager()):
            decision = await resolve_routing_decision(_context())

        assert decision.is_enforced
        assert decision.target is not None
        assert decision.target.account_id == MAPPED_ACCOUNT
        assert decision.credentials is not None
        assert decision.credentials.access_key_id == "ASIAROUTED"

    @pytest.mark.asyncio
    async def test_the_signed_credentials_reach_the_boto3_client(self):
        """End of the chain: the pool builds a client bound to those credentials.

        The `SimplePoolService` half of §2.1. Read back off the client's request signer
        rather than trusting the constructor argument, because the failure this guards
        against is a client that accepted the credentials and then signed ambiently.
        """
        from src.pool.simple_pool import SimplePoolService

        pool = SimplePoolService(region="us-east-1")
        client = await pool.get_client(_credentials(access_key_id="ASIAROUTEDXYZ", region="eu-west-1"))

        signed = client._invoke_client._request_signer._credentials
        assert signed.access_key == "ASIAROUTEDXYZ"
        assert signed.token == "routed-token"
        # The destination's own region wins over the pod's — a destination connected in
        # eu-west-1 must not have its calls sent to the platform's region.
        assert client._invoke_client.meta.region_name == "eu-west-1"

    @pytest.mark.asyncio
    async def test_an_unmapped_principal_gets_the_ambient_client_unchanged(self):
        """No credentials ⇒ the one shared ambient client, identical to main.

        Identity (`is`), not equality: a per-call client for the platform path would be
        a behaviour change for every unmapped install, which is all of them on day one.
        """
        from src.pool.simple_pool import SimplePoolService

        pool = SimplePoolService(region="us-east-1")
        first = await pool.get_client()
        second = await pool.get_client(None)
        assert first is second

    @pytest.mark.asyncio
    async def test_the_platform_rung_never_reaches_the_signer(self, session_factory, routing_environment):
        """A resolved platform target returns a decision with no credentials at all.

        And it must do so without an assume: the platform rung is the *absence* of a
        mapping, so signing anything for it would be inventing a routing decision.
        """
        await _seed(session_factory, Organization(id=ORG_ID, name="acme", settings={}))
        assume, assume_mock = _patch_assume()

        with _patch_routing_session(session_factory), assume:
            decision = await resolve_routing_decision(_context())

        assert decision.credentials is None
        assert decision.target is not None and decision.target.is_platform
        assume_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_signer_refuses_a_platform_target_outright(self, session_factory):
        """Calling the signer for the platform rung is a caller bug, not a default.

        It raises rather than assuming *something*, because the something it would have
        to invent is which account to route to — the exact decision this module must
        never make.
        """
        async with session_factory() as session:
            with pytest.raises(ValueError, match="platform rung"):
                await _signer().get_credentials(session, BedrockTarget(account_id=PLATFORM_ACCOUNT, rung="platform"), user_id=CANONICAL_USER_ID)

    @pytest.mark.asyncio
    async def test_the_canonical_user_id_is_the_session_tag_not_the_cognito_sub(self, session_factory, routing_environment):
        """#4647: the destination's CloudTrail gets `users.id`, never a Cognito sub.

        Two id namespaces in one audit field means the destination account's operator
        cannot join ADP's records to their own. The tag is the only place this id
        crosses the account boundary, so it is the only place worth pinning.
        """
        await _seed_enforced_org_mapping(session_factory)
        assume, assume_mock = _patch_assume()

        with _patch_routing_session(session_factory), assume, patch.object(bedrock_destination_signer, "_secrets_manager", _secrets_manager()):
            await resolve_routing_decision(_context())

        assert assume_mock.call_args.kwargs["user_id"] == CANONICAL_USER_ID

    @pytest.mark.asyncio
    async def test_the_external_id_from_the_connection_is_sent(self, session_factory, routing_environment):
        """Confused-deputy protection survives the routing path.

        The ExternalId lives in the Secrets Manager payload, not on the registry row.
        If it were dropped, every destination whose trust policy requires one would
        fail — and every destination whose policy merely *accepts* one would be
        assumable by anyone who learned the role ARN.
        """
        await _seed_enforced_org_mapping(session_factory)
        assume, assume_mock = _patch_assume()

        with _patch_routing_session(session_factory), assume, patch.object(bedrock_destination_signer, "_secrets_manager", _secrets_manager()):
            await resolve_routing_decision(_context())

        assert assume_mock.call_args.kwargs["external_id"] == EXTERNAL_ID


# ============================================================================
# 2. Credential cache isolation — §2.3, the cross-tenant one
# ============================================================================


class TestCredentialCacheIsolation:
    """The most security-critical class in this file.

    The dead pool ``STSClient`` keys its credential cache on the role ARN alone. That
    is safe only for a static account list with no principal dimension. Once the
    destination is principal-dependent — which is what this issue does — a role-ARN key
    is a cross-tenant credential cache: two orgs that legitimately connect the *same*
    role with *different* ExternalIds would share one entry, and the second org's calls
    would be signed under the first org's session identity.
    """

    def test_the_cache_key_is_the_full_identity_tuple(self):
        """Every input to the assume is in the key, org_id first.

        Pinned as an exact tuple rather than "contains org_id", because the failure mode
        is an *omission* — a newly added assume argument that never reaches the key —
        and a containment check cannot see an omission.
        """
        inputs = _AssumeInputs(
            org_id=ORG_ID,
            role_arn="arn:aws:iam::111111111111:role/r",
            external_id=EXTERNAL_ID,
            region="us-east-1",
            rung="org",
            destination_updated_at=datetime(2026, 9, 1, tzinfo=UTC),
            account_id=MAPPED_ACCOUNT,
            user_id=CANONICAL_USER_ID,
        )
        assert inputs.cache_key() == (
            ORG_ID,
            "arn:aws:iam::111111111111:role/r",
            EXTERNAL_ID,
            "us-east-1",
            "org",
            datetime(2026, 9, 1, tzinfo=UTC),
        )

    def test_two_orgs_sharing_a_role_arn_do_not_share_an_entry(self):
        """The bug this key exists to make unrepresentable.

        Same role ARN, different tenants and different ExternalIds — entirely legitimate
        (a shared client account) and the case a role-ARN-keyed cache silently gets
        wrong. Two distinct keys is the whole assertion.
        """
        common = {
            "role_arn": "arn:aws:iam::111111111111:role/shared",
            "region": "us-east-1",
            "rung": "org",
            "destination_updated_at": datetime(2026, 9, 1, tzinfo=UTC),
            "account_id": MAPPED_ACCOUNT,
            "user_id": CANONICAL_USER_ID,
        }
        acme = _AssumeInputs(org_id=ORG_ID, external_id="external-acme", **common)
        globex = _AssumeInputs(org_id=OTHER_ORG_ID, external_id="external-globex", **common)

        assert acme.cache_key() != globex.cache_key()

        cache = DestinationCredentialCache()
        cache.put(acme.cache_key(), _credentials(access_key_id="ASIA-ACME"))
        assert cache.get(globex.cache_key(), margin_seconds=0) is None
        assert cache.get(acme.cache_key(), margin_seconds=0).access_key_id == "ASIA-ACME"

    def test_the_user_and_org_rungs_do_not_collide(self):
        """`rung` is in the key, so one destination reached two ways stays two entries.

        Not a security boundary on its own, but it keeps the audit story coherent: an
        entry minted for a user-rung decision is not handed to an org-rung one.
        """
        common = {
            "org_id": ORG_ID,
            "role_arn": "arn:aws:iam::111111111111:role/r",
            "external_id": EXTERNAL_ID,
            "region": "us-east-1",
            "destination_updated_at": datetime(2026, 9, 1, tzinfo=UTC),
            "account_id": MAPPED_ACCOUNT,
            "user_id": CANONICAL_USER_ID,
        }
        assert _AssumeInputs(rung="user", **common).cache_key() != _AssumeInputs(rung="org", **common).cache_key()

    def test_editing_a_destination_in_place_self_evicts(self):
        """§8.3: rolling back a bad mapping must not wait out the 3600s credential TTL.

        `updated_at` is in the key, so re-verifying or re-pointing a destination yields
        a different key and the stale credentials are simply never read again. This is
        what makes eviction-on-change work with **no hook for R4 to forget to call**.
        """
        base = _AssumeInputs(
            org_id=ORG_ID,
            role_arn="arn:aws:iam::111111111111:role/r",
            external_id=EXTERNAL_ID,
            region="us-east-1",
            rung="org",
            destination_updated_at=datetime(2026, 9, 1, tzinfo=UTC),
            account_id=MAPPED_ACCOUNT,
            user_id=CANONICAL_USER_ID,
        )
        edited = replace(base, destination_updated_at=datetime(2026, 9, 2, tzinfo=UTC))

        cache = DestinationCredentialCache()
        cache.put(base.cache_key(), _credentials())
        assert cache.get(edited.cache_key(), margin_seconds=0) is None

    def test_repointing_a_scope_at_another_destination_self_evicts(self):
        """The other change shape: a different destination row ⇒ a different role ARN."""
        base = _AssumeInputs(
            org_id=ORG_ID,
            role_arn="arn:aws:iam::111111111111:role/old",
            external_id=EXTERNAL_ID,
            region="us-east-1",
            rung="org",
            destination_updated_at=datetime(2026, 9, 1, tzinfo=UTC),
            account_id=MAPPED_ACCOUNT,
            user_id=CANONICAL_USER_ID,
        )
        repointed = replace(base, role_arn="arn:aws:iam::222222222222:role/new", account_id=OTHER_ACCOUNT)

        cache = DestinationCredentialCache()
        cache.put(base.cache_key(), _credentials())
        assert cache.get(repointed.cache_key(), margin_seconds=0) is None

    @pytest.mark.asyncio
    async def test_a_destination_edit_changes_what_is_signed_end_to_end(self, session_factory, routing_environment):
        """The self-eviction property, asserted through the real signer and database.

        The key-level tests above prove the key discriminates; this proves the signer
        actually re-assumes rather than serving the cached entry — the difference
        between a correct key and a correct cache.
        """
        destination = await _seed_enforced_org_mapping(session_factory)
        signer = _signer()
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id, region="us-east-1")

        with patch("src.proxy.bedrock_signing.assume_role", MagicMock(return_value=_assume_result("ASIA-FIRST"))):
            async with session_factory() as session:
                first = await signer.get_credentials(session, target, user_id=CANONICAL_USER_ID)
        assert first.access_key_id == "ASIA-FIRST"

        async with session_factory() as session:
            row = await session.get(BedrockDestinationRegistry, destination.id)
            row.updated_at = datetime(2026, 9, 5, tzinfo=UTC)
            await session.commit()

        with patch("src.proxy.bedrock_signing.assume_role", MagicMock(return_value=_assume_result("ASIA-SECOND"))):
            async with session_factory() as session:
                second = await signer.get_credentials(session, target, user_id=CANONICAL_USER_ID)
        assert second.access_key_id == "ASIA-SECOND", "an edited destination must not serve stale credentials"

    @pytest.mark.asyncio
    async def test_a_cache_hit_does_not_re_assume(self, session_factory, routing_environment):
        """The cache is the reason the hot path is not one STS call per model call."""
        destination = await _seed_enforced_org_mapping(session_factory)
        signer = _signer()
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id, region="us-east-1")
        assume_mock = MagicMock(return_value=_assume_result())

        with patch("src.proxy.bedrock_signing.assume_role", assume_mock):
            async with session_factory() as session:
                await signer.get_credentials(session, target, user_id=CANONICAL_USER_ID)
                await signer.get_credentials(session, target, user_id=CANONICAL_USER_ID)

        assert assume_mock.call_count == 1

    def test_credentials_are_refreshed_before_they_expire_not_after(self):
        """Under fail-closed an expired credential is an outage, not a retry.

        A margin-less cache would hand out a credential with two seconds left and the
        call would fail mid-flight — a 502 for a correctly configured mapping.
        """
        cache = DestinationCredentialCache()
        key = ("k",)
        cache.put(key, _credentials(ttl_seconds=120))

        assert cache.get(key, margin_seconds=0) is not None
        assert cache.get(key, margin_seconds=300) is None, "an entry inside the refresh margin must be treated as due"

    def test_the_cache_is_lru_bounded(self):
        """Bounded because the key space is now principal-dependent.

        The dead pool cache is an unbounded dict — fine for two static accounts, a
        memory-growth problem once every principal can mint an entry.
        """
        cache = DestinationCredentialCache(max_entries=2)
        cache.put(("a",), _credentials())
        cache.put(("b",), _credentials())
        cache.get(("a",), margin_seconds=0)  # `a` becomes most-recently-used
        cache.put(("c",), _credentials())

        assert len(cache) == 2
        assert cache.get(("b",), margin_seconds=0) is None, "the least-recently-used entry is the one evicted"
        assert cache.get(("a",), margin_seconds=0) is not None

    def test_invalidate_destination_drops_every_entry_for_a_role(self):
        """The explicit hook R4 calls on delete. Promptness, not correctness."""
        cache = DestinationCredentialCache()
        role = "arn:aws:iam::111111111111:role/r"
        cache.put((ORG_ID, role, "e1", "us-east-1", "org", None), _credentials())
        cache.put((OTHER_ORG_ID, role, "e2", "us-east-1", "org", None), _credentials())
        cache.put((ORG_ID, "arn:aws:iam::222222222222:role/other", "e3", "us-east-1", "org", None), _credentials())

        assert cache.invalidate_destination(role) == 2
        assert len(cache) == 1


# ============================================================================
# 3. Fail closed — ruling 1
# ============================================================================


class TestFailClosed:
    """Every cause fails the call, names the account, and carries its own reason.

    Ruling 1 forbids a fallback branch *existing*, not merely being taken, so this
    class asserts both: each failure raises, and the module has no such branch in its
    source at all.
    """

    async def _expect_failure(self, session_factory, target, *, signer=None) -> BedrockAccountUnavailableError:
        signer = signer or _signer()
        async with session_factory() as session:
            with pytest.raises(BedrockAccountUnavailableError) as exc_info:
                await signer.get_credentials(session, target, user_id=CANONICAL_USER_ID)
        return exc_info.value

    @pytest.mark.asyncio
    async def test_assume_role_failure_names_the_account_and_its_own_reason(self, session_factory):
        destination = await _seed_enforced_org_mapping(session_factory)
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id)

        with patch("src.proxy.bedrock_signing.assume_role", MagicMock(side_effect=STSAssumeError("denied", code="AccessDenied"))):
            error = await self._expect_failure(session_factory, target)

        assert error.reason == REASON_ASSUME_ROLE_FAILED
        assert error.account_id == MAPPED_ACCOUNT
        assert MAPPED_ACCOUNT in error.message
        assert error.status_code == 502

    @pytest.mark.asyncio
    async def test_a_deleted_destination_row_is_account_unlinked(self, session_factory):
        """§8.3's dangerous rollback ordering: deleting a connection a mapping still uses.

        Distinguished from an assume failure because the fix is different — reconnect
        the account, rather than repair a trust policy.
        """
        error = await self._expect_failure(
            session_factory,
            BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id="dest-gone"),
        )
        assert error.reason == REASON_ACCOUNT_UNLINKED
        assert error.account_id == MAPPED_ACCOUNT

    @pytest.mark.asyncio
    async def test_a_destination_that_became_unverified_fails_rather_than_signing(self, session_factory):
        """Signing with a no-longer-verified destination is worse than failing.

        The resolver skips unusable destinations (§4.4), so this only fires if the row
        changed between resolution and signing — and at that point the safe move is the
        502, not an assume against something nobody has proven.
        """
        destination = await _seed_enforced_org_mapping(session_factory, destination=_destination(verified=False))
        error = await self._expect_failure(
            session_factory,
            BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id),
        )
        assert error.reason == REASON_ACCOUNT_UNLINKED

    @pytest.mark.asyncio
    async def test_an_unreadable_credential_secret_is_account_unlinked(self, session_factory):
        """Secrets Manager failure ⇒ fail closed, and the SM error text never escapes.

        The exception text can carry the secret ARN, so the message must be built here
        rather than forwarded — the same split ``assume_role_routes.py`` already makes.
        """
        destination = await _seed_enforced_org_mapping(session_factory)
        signer = _signer(_secrets_manager(raises=RuntimeError(f"ResourceNotFound: {SECRET_ARN}")))

        error = await self._expect_failure(
            session_factory,
            BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id),
            signer=signer,
        )
        assert error.reason == REASON_ACCOUNT_UNLINKED
        assert SECRET_ARN not in error.message

    @pytest.mark.asyncio
    async def test_a_missing_credential_row_is_account_unlinked(self, session_factory):
        """A destination pointing at a deleted vault row has no ExternalId to send."""
        destination = _destination(credential_id="cred-gone")
        await _seed(session_factory, destination)
        error = await self._expect_failure(
            session_factory,
            BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id),
        )
        assert error.reason == REASON_ACCOUNT_UNLINKED

    @pytest.mark.asyncio
    async def test_an_admin_registered_destination_assumes_without_an_external_id(self, session_factory):
        """`credential_id IS NULL` proceeds without one, and fails loudly if refused.

        Not silently degraded: a role whose trust policy requires an ExternalId rejects
        this, which surfaces as `assume_role_failed` naming the account. R4 is where
        such a row should be refused at save time (§6.7).
        """
        destination = _destination(credential_id=None, owner_org_id=None)
        await _seed(session_factory, destination)
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id)
        assume_mock = MagicMock(return_value=_assume_result())

        with patch("src.proxy.bedrock_signing.assume_role", assume_mock):
            async with session_factory() as session:
                await _signer().get_credentials(session, target, user_id=CANONICAL_USER_ID)

        assert assume_mock.call_args.kwargs["external_id"] is None

    @pytest.mark.asyncio
    async def test_no_failure_path_returns_platform_credentials(self, session_factory):
        """The property, not a sample: every failure raises, none returns.

        Enumerated over the concrete failure modes rather than asserting one, because
        ruling 1's guarantee is about the *set* of outcomes.
        """
        destination = await _seed_enforced_org_mapping(session_factory)
        good_target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id)

        cases = [
            # (target, signer, assume side effect)
            (BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id="dest-gone"), _signer(), None),
            (good_target, _signer(_secrets_manager(raises=RuntimeError("boom"))), None),
            (good_target, _signer(), STSAssumeError("nope")),
        ]
        for target, signer, side_effect in cases:
            assume = MagicMock(side_effect=side_effect, return_value=None if side_effect else _assume_result())
            with patch("src.proxy.bedrock_signing.assume_role", assume):
                async with session_factory() as session:
                    with pytest.raises(BedrockAccountUnavailableError):
                        await signer.get_credentials(session, target, user_id=CANONICAL_USER_ID)

    def test_the_signing_module_contains_no_fallback_branch(self):
        """Structural, and the one that survives refactoring.

        Ruling 1 forbids the branch existing. A behavioural test can only show it was
        not taken on the inputs tried; a source assertion shows there is nothing to
        take. Compared against comment- and docstring-stripped source, since the module
        docstring necessarily discusses the fallback it promises not to have.
        """
        from src.proxy import bedrock_signing

        source = _executable_source(bedrock_signing)
        for forbidden in ("platform_bedrock_account_id", "_platform_target", "is_platform_registered"):
            assert forbidden not in source, f"the signer must not be able to reach {forbidden} — ruling 1 forbids a fallback"

    def test_every_reason_code_is_reachable_and_has_a_message(self):
        """Each cause gets its own remediation, so one generic code cannot serve.

        The vocabulary is wire contract shared with R4's save-time validation, so this
        also pins that every declared code renders — a code with no message would reach
        the R4 UI as an empty error.
        """
        for reason in sorted(ALL_REASONS):
            error = BedrockAccountUnavailableError(reason=reason, account_id=MAPPED_ACCOUNT, scope="org", model_id=MODEL_ID)
            assert MAPPED_ACCOUNT in error.message
            assert error.details["reason"] == reason
            assert error.details["remediation"]

    def test_the_remediation_is_addressed_to_whoever_can_apply_it(self):
        """§2.6 requirement 2: a member cannot fix a team mapping they cannot author.

        Telling them to change it is a dead end that becomes a support ticket. The user
        rung is self-service (ruling 2) and points at their own screen; team and org
        rungs point at a platform admin (ruling 4).
        """
        admin_facing = BedrockAccountUnavailableError(reason=REASON_ASSUME_ROLE_FAILED, account_id=MAPPED_ACCOUNT, scope="team")
        self_facing = BedrockAccountUnavailableError(reason=REASON_ASSUME_ROLE_FAILED, account_id=MAPPED_ACCOUNT, scope="user")

        assert "platform admin" in admin_facing.details["remediation"]
        assert "team" in admin_facing.details["remediation"]
        assert "platform admin" not in self_facing.details["remediation"]
        assert "Credentials" in self_facing.details["remediation"]


# ============================================================================
# 4. Timeouts are preserved — §2.4
# ============================================================================


class TestTimeoutsArePreserved:
    """A routed client keeps BOTH `Config` timeouts. The §2.4 latency requirement.

    The dead ``PoolService`` builds its cross-account client with no ``Config`` at all,
    inheriting botocore's 60s default read timeout. Routing a principal through a
    client like that times out exactly the long Opus/Sonnet generations the 3600s
    setting exists to survive — presenting as random failures *only for routed
    principals*, which is close to the hardest latency bug to attribute.
    """

    @pytest.mark.asyncio
    async def test_both_timeouts_survive_on_a_routed_client(self):
        from src.pool.simple_pool import SimplePoolService

        routed = await SimplePoolService(region="us-east-1").get_client(_credentials())

        assert routed._invoke_client.meta.config.read_timeout == 3600
        assert routed._streaming_client.meta.config.read_timeout == 300

    @pytest.mark.asyncio
    async def test_a_routed_client_is_configured_identically_to_the_ambient_one(self):
        """Same config both ways, so routing changes the account and nothing else.

        Compared field by field against the ambient client rather than against literals,
        so a future change to the ambient timeouts cannot silently apply to only one of
        the two paths.
        """
        from src.pool.simple_pool import SimplePoolService

        pool = SimplePoolService(region="us-east-1")
        ambient = await pool.get_client()
        routed = await pool.get_client(_credentials())

        for attribute in ("read_timeout", "connect_timeout", "retries"):
            assert getattr(routed._invoke_client.meta.config, attribute) == getattr(ambient._invoke_client.meta.config, attribute)
            assert getattr(routed._streaming_client.meta.config, attribute) == getattr(ambient._streaming_client.meta.config, attribute)


# ============================================================================
# 5. Error-class discrimination — §5.1, §5.2
# ============================================================================


class TestErrorClassDiscrimination:
    """The three things "model not enabled", "role not assumable" and "Bedrock is down" are three things.

    §5.1 calls this a prerequisite rather than a nicety: with a bare ``Exception`` catch,
    the routing feature's most common failure mode is indistinguishable from an outage,
    and the operator's first instinct is to page someone rather than enable a model.
    """

    @staticmethod
    def _classify(exc: Exception, target: BedrockTarget | None) -> Exception:
        from src.proxy.service import _classify_bedrock_failure

        return _classify_bedrock_failure(exc, model_id=MODEL_ID, target=target)

    @staticmethod
    def _client_error(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "denied"}}, "InvokeModel")

    def test_access_denied_on_a_routed_call_becomes_model_not_enabled(self):
        """Bedrock model access is per-account, and a routed call passed every ADP check.

        `check_model_access` is a glob match against the caller's allowed-model config —
        it knows nothing about what the *destination* account has enabled — so an
        AccessDenied from the destination is overwhelmingly an unenabled model there.
        """
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id="dest-1")
        error = self._classify(self._client_error("AccessDeniedException"), target)

        assert isinstance(error, BedrockAccountUnavailableError)
        assert error.reason == REASON_MODEL_NOT_ENABLED
        assert error.account_id == MAPPED_ACCOUNT
        assert error.model_id == MODEL_ID
        assert MODEL_ID in error.message and MAPPED_ACCOUNT in error.message

    def test_access_denied_on_an_unrouted_call_keeps_mains_behaviour(self):
        """The platform path must not be reclassified. This issue has no mandate there.

        Both the no-decision case and an explicitly-resolved platform target, because
        the two arrive here differently (enforcement off vs. no mapping) and only one
        of them was exercised by the shadow release.
        """
        for target in (None, BedrockTarget(account_id=PLATFORM_ACCOUNT, rung="platform")):
            error = self._classify(self._client_error("AccessDeniedException"), target)
            assert isinstance(error, BedrockInvocationError)
            assert not isinstance(error, BedrockAccountUnavailableError)

    def test_a_transport_failure_on_a_routed_call_is_not_blamed_on_the_account(self):
        """A "Bedrock is down" failure must not read as "enable the model in account X".

        The wrong remediation sends an operator to a console page for a problem no
        console change can fix, and hides a real outage behind a config message.
        """
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id="dest-1")
        error = self._classify(EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"), target)

        assert isinstance(error, BedrockInvocationError)
        assert not isinstance(error, BedrockAccountUnavailableError)

    def test_a_validation_exception_stays_a_validation_exception(self):
        """A malformed body is the caller's problem on every path, routed or not."""
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id="dest-1")
        error = self._classify(self._client_error("ValidationException"), target)

        assert isinstance(error, BedrockInvocationError)
        assert error.details is not None
        assert error.details.get("bedrock_error_code") == "ValidationException"

    def test_a_fail_closed_error_passes_through_untouched(self):
        """It already names its account, cause and fix; re-wrapping would lose all three."""
        original = BedrockAccountUnavailableError(reason=REASON_ASSUME_ROLE_FAILED, account_id=MAPPED_ACCOUNT, scope="org")
        assert self._classify(original, BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org")) is original


# ============================================================================
# 6. Saved mappings are always enforced
# ============================================================================


class TestRoutingAlwaysEnforced:
    """Saved mappings apply without rollout flags, including old opt-out values."""

    @pytest.mark.parametrize("legacy_value", ["true", "false"])
    def test_retired_dotenv_flag_does_not_break_startup(self, tmp_path, monkeypatch, legacy_value):
        from src.shared.config import Settings

        monkeypatch.delenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text(f"BG_BEDROCK_ROUTING_ENFORCE={legacy_value}\nBG_PLATFORM_BEDROCK_ACCOUNT_ID={PLATFORM_ACCOUNT}\n")
        settings = Settings(_env_file=dotenv)
        assert settings.platform_bedrock_account_id == PLATFORM_ACCOUNT

    def test_unknown_dotenv_settings_are_still_rejected(self, tmp_path):
        from pydantic import ValidationError

        from src.shared.config import Settings

        dotenv = tmp_path / ".env"
        dotenv.write_text("BG_UNKNOWN_ROUTING_SETTING=true\n")
        with pytest.raises(ValidationError, match="extra_forbidden"):
            Settings(_env_file=dotenv)

    @pytest.mark.asyncio
    async def test_routing_database_failure_cannot_fall_back_to_platform(self):
        from src.proxy.service import ProxyService

        pool = MagicMock(get_client=AsyncMock())
        service = ProxyService(pool)
        service._log_usage = AsyncMock()
        with patch("src.shared.database.get_session_factory", side_effect=RuntimeError("routing database unavailable")):
            with pytest.raises(RuntimeError, match="routing database unavailable"):
                await service.invoke_model(MODEL_ID, {"messages": [], "max_tokens": 8}, _context())
        pool.get_client.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", ["org", "team", "user"])
    @pytest.mark.parametrize("legacy_value", [None, False, "false"])
    @pytest.mark.parametrize("legacy_env", [None, "false"])
    async def test_saved_mapping_is_active_without_opt_in(self, session_factory, monkeypatch, scope, legacy_value, legacy_env):
        from sqlalchemy import update

        if legacy_env is None:
            monkeypatch.delenv("BG_BEDROCK_ROUTING_ENFORCE", raising=False)
        else:
            monkeypatch.setenv("BG_BEDROCK_ROUTING_ENFORCE", legacy_env)
        await _seed_enforced_org_mapping(session_factory, scope=scope)
        settings = {} if legacy_value is None else {"bedrock_routing_enforce": legacy_value}
        async with session_factory() as session:
            await session.execute(update(Organization).where(Organization.id == ORG_ID).values(settings=settings))
            await session.commit()
        assume, assume_mock = _patch_assume()

        with _patch_routing_session(session_factory), assume, patch.object(bedrock_destination_signer, "_secrets_manager", _secrets_manager()):
            decision = await resolve_routing_decision(_context())

        assert decision.is_enforced
        assert decision.target.account_id == MAPPED_ACCOUNT
        assert decision.target.rung == scope
        assert decision.credentials is not None
        assume_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_fail_closed_error_propagates_out_of_the_decision(self, session_factory, routing_environment):
        """The decision does not catch it. That is what makes fail-closed reach the client.

        Swallowing it here would be the silent fallback under another name: the caller
        would get `credentials=None` and the platform account would serve the call.
        """
        await _seed_enforced_org_mapping(session_factory, destination=_destination(credential_id="cred-gone"))

        with _patch_routing_session(session_factory), patch("src.proxy.bedrock_signing.assume_role", MagicMock(return_value=_assume_result())):
            with pytest.raises(BedrockAccountUnavailableError) as exc_info:
                await resolve_routing_decision(_context())

        assert exc_info.value.account_id == MAPPED_ACCOUNT

    def test_the_decision_is_an_explicit_argument_not_ambient_state(self):
        """§2.1: `get_client` takes credentials as a parameter, never a contextvar.

        The contextvar alternative was rejected because a credential decision read from
        ambient state is precisely how one principal's call gets signed with another's —
        a leak that survives across requests on a reused event loop and is invisible in
        a stack trace.
        """
        from src.pool import simple_pool
        from src.pool.simple_pool import SimplePoolService

        parameters = inspect.signature(SimplePoolService.get_client).parameters
        assert "credentials" in parameters
        assert parameters["credentials"].default is None
        assert "ContextVar" not in _executable_source(simple_pool)


# ============================================================================
# 7. Redaction — §2.6
# ============================================================================


class TestRedaction:
    """The account id is required in the error; the role ARN and ExternalId must not be.

    Enforced by construction rather than by review: the error class has no parameter for
    either, so a future edit that wants to leak one has to change a signature in a file
    whose docstring says why not.
    """

    def test_the_error_class_has_no_parameter_for_a_role_arn_or_external_id(self):
        parameters = set(inspect.signature(BedrockAccountUnavailableError.__init__).parameters)
        assert "role_arn" not in parameters
        assert "external_id" not in parameters
        assert {"reason", "account_id", "scope", "model_id"} <= parameters

    @pytest.mark.asyncio
    async def test_an_assume_failure_leaks_neither_the_arn_nor_the_external_id(self, session_factory):
        """The end-to-end check, since the class-level guarantee only covers parameters.

        A caller could still interpolate an ARN into a message it builds itself, so the
        message and the details payload are both searched for the real values.
        """
        role_arn = "arn:aws:iam::111111111111:role/secret-named-role"
        destination = await _seed_enforced_org_mapping(session_factory, destination=_destination(role_arn=role_arn))
        target = BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id=destination.id)

        with patch("src.proxy.bedrock_signing.assume_role", MagicMock(side_effect=STSAssumeError("AccessDenied"))):
            async with session_factory() as session:
                with pytest.raises(BedrockAccountUnavailableError) as exc_info:
                    await _signer().get_credentials(session, target, user_id=CANONICAL_USER_ID)

        rendered = f"{exc_info.value.message} {exc_info.value.details}"
        assert role_arn not in rendered
        assert "secret-named-role" not in rendered
        assert EXTERNAL_ID not in rendered
        # Ruling 1 REQUIRES the account id: without it the user cannot tell an operator
        # which account to look at, and the "actionable" half of the error is lost.
        assert MAPPED_ACCOUNT in rendered
