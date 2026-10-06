"""Tests for auth endpoints, and for domain token/authorization enforcement.

The second half of this file (issue #5055, U14) is the negative matrix for
R5/R6 enforcement. Two properties of how it is written matter:

**Tokens are really signed.** Each case mints a token with a real RSA keypair
generated in-process and serves the matching public key through the JWKS cache.
Stubbing verification would leave the parts most worth testing — the pinned
RS256 algorithm, the JWKS key lookup, signature and expiry checking — untested,
and a bypass in any of them would pass a mocked suite.

**Failures are asserted as 401/403, never merely "not 200".** A request rejected
with 422 is also "not 200", and that was a real defect this story found: a
required ``Header(...)`` made a missing credential a validation error. Asserting
the status class is what distinguishes "refused" from "malformed".
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.middleware.auth import create_access_token, decode_token
from app.routers.auth import _generate_api_key, _hash_api_key, _verify_api_key
from app.schemas.auth import TokenPayload


# -- JWT utility tests --


class TestJWTUtils:
    """Test JWT creation and decoding."""

    def test_create_and_decode_token(self):
        org_id = uuid.uuid4()
        token, expires_in = create_access_token(org_id)
        assert isinstance(token, str)
        assert expires_in == 3600  # default 60 min * 60

        payload = decode_token(token)
        assert isinstance(payload, TokenPayload)
        assert payload.org_id == org_id

    def test_decode_invalid_token_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            decode_token("invalid.token.here")
        assert exc_info.value.status_code == 401

    def test_create_token_returns_string(self):
        org_id = uuid.uuid4()
        token, _ = create_access_token(org_id)
        assert token.count(".") == 2  # JWT has 3 parts


# -- API key hashing tests --


class TestApiKeyHashing:
    """Test API key SHA-256 hashing."""

    def test_hash_and_verify(self):
        raw_key = _generate_api_key()
        hashed = _hash_api_key(raw_key)
        assert _verify_api_key(raw_key, hashed)

    def test_wrong_key_fails_verify(self):
        raw_key = _generate_api_key()
        hashed = _hash_api_key(raw_key)
        wrong_key = _generate_api_key()
        assert not _verify_api_key(wrong_key, hashed)

    def test_key_prefix_format(self):
        key = _generate_api_key()
        assert key.startswith("sp_")
        assert len(key) == 3 + 32  # "sp_" + 32 hex chars

    def test_hash_is_hex_string(self):
        key = _generate_api_key()
        hashed = _hash_api_key(key)
        assert len(hashed) == 64  # SHA-256 hex digest


# -- Auth router endpoint tests --


class TestLoginEndpoint:
    """Test POST /auth/login."""

    @pytest.mark.asyncio
    async def test_login_without_key_returns_422(self, client):
        """Login without api_key field returns validation error."""
        response = await client.post("/auth/login", json={})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_login_with_empty_body_returns_422(self, client):
        """Login with no JSON body returns validation error."""
        response = await client.post("/auth/login")
        assert response.status_code == 422


class TestCreateApiKeyEndpoint:
    """Test POST /auth/token."""

    @pytest.mark.asyncio
    async def test_create_key_requires_auth(self, client):
        """Creating an API key requires a valid JWT."""
        response = await client.post("/auth/token", json={"name": "test-key"})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_key_empty_name_returns_422(self, client):
        """Creating an API key with empty name returns 422."""
        org_id = uuid.uuid4()
        token, _ = create_access_token(org_id)
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post("/auth/token", json={"name": ""}, headers=headers)
        assert response.status_code == 422


# ====================================================================

# Domain token policy + workspace authorization enforcement (issue #5055, U14)
# ====================================================================


from app import auth as domain_auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.endpoint_inventory import (  # noqa: E402
    DOMAIN_ROUTES,
    PRIVATE_DOMAIN_ROUTES,
    WORKSPACE_PATH_PARAM,
    all_inventoried,
    classify,
    mounted_operations,
)
from app.main import app as fastapi_app  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.organization_grant import (  # noqa: E402
    ORGANIZATION_ADMINISTER,
    OrganizationGrantRecord,
)
from app.models.research_finding import ResearchFinding  # noqa: E402
from app.models.research_proposal import ResearchProposal  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
from app.models.workspace_grant import WorkspaceGrantRecord  # noqa: E402
from superplane_auth.policy import (  # noqa: E402
    REJECTED_VALIDATION_PATHS,
    TRUSTED_VALIDATION_PATH,
    DomainTokenPolicy,
    Permission,
    TokenPolicyError,
    TokenRejectedError,
)

TEST_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool"
TEST_CLIENT_ID = "test-allowlisted-client"
TEST_KID = "test-key-1"


def _rsa_keypair():
    """Generate a throwaway RSA keypair and its JWKS entry.

    Generated per test session and never written to disk: there is no key
    material in this repository, and none of these values is a credential for
    anything that exists.
    """
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    # Issue #5601 (S02): PyJWT's `RSAAlgorithm.to_jwk` replaces
    # `jose.jwk.construct(pem, "RS256").to_dict()`. It returns a JSON STRING rather
    # than a mapping, and its members are already `str`, so the bytes-decoding pass
    # the jose version needed is gone. The result is the same public JWK the user
    # pool would publish — `kty`, `n`, `e` — which is what `jwks_cache` holds and
    # what `verify_access_token` builds its key from.
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk_dict.update({"kid": TEST_KID, "alg": "RS256", "use": "sig"})
    return pem, jwk_dict


@pytest.fixture(scope="module")
def rsa_keys():
    return _rsa_keypair()


@pytest.fixture
def enforcing(rsa_keys, monkeypatch):
    """Turn domain enforcement ON with a real signing key, for one test.

    Rebuilds the app's policy the way startup does, so a test exercises the same
    object graph production gets rather than a hand-assembled one.
    """
    private_pem, public_jwk = rsa_keys
    monkeypatch.setattr(settings, "domain_auth_enforced", True)
    monkeypatch.setattr(settings, "cognito_issuer", TEST_ISSUER)
    monkeypatch.setattr(settings, "domain_auth_allowed_client_ids", [TEST_CLIENT_ID])
    monkeypatch.setattr(settings, "cognito_jwks_url", "https://example.invalid/jwks")

    domain_auth.jwks_cache.load([public_jwk])
    previous = getattr(fastapi_app.state, "domain_policy", None)
    fastapi_app.state.domain_policy = domain_auth.build_domain_policy()
    yield private_pem
    fastapi_app.state.domain_policy = previous
    domain_auth.jwks_cache.clear()


def _mint(private_pem: str, **overrides) -> str:
    """Mint a signed token whose claims default to a valid access token."""
    now = datetime.now(UTC)
    claims = {
        "sub": "user-abc",
        "token_use": "access",
        "iss": TEST_ISSUER,
        "client_id": TEST_CLIENT_ID,
        "custom:org_id": str(uuid.uuid4()),
        "custom:account_type": "human",
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "iat": int(now.timestamp()),
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": TEST_KID})


async def _seed_workspace(
    permissions: str | None,
    principal="user-abc",
    org_id=None,
    principal_type="human",
):
    """Create an org + workspace, optionally granting `principal` on it."""
    from tests.conftest import async_session_test

    org_uuid = org_id or uuid.uuid4()
    workspace_id = uuid.uuid4()
    async with async_session_test() as session:
        # Name is unique, so it is derived from the id: a test that seeds two
        # organizations must not collide on it.
        session.add(
            Organization(
                id=org_uuid, name=f"test-org-{org_uuid.hex[:8]}", billing_plan="free"
            )
        )
        session.add(
            Workspace(
                id=workspace_id,
                org_id=org_uuid,
                name="ws",
                isolation_mode="shared",
                status="active",
            )
        )
        if permissions is not None:
            session.add(
                WorkspaceGrantRecord(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    org_id=org_uuid,
                    principal=principal,
                    principal_type=principal_type,
                    permissions=permissions,
                )
            )
        await session.commit()
    return org_uuid, workspace_id


async def _seed_organization_grant(org_id, principal, permissions):
    """Create an organization plus one organization-level grant on it.

    Separate from `_seed_workspace` because the case it serves is the absence of a
    workspace: an organization administrator provisioning the org's FIRST workspace
    has no workspace row and can have no workspace grant (the grant's
    `workspace_id` is a foreign key to `workspaces.id`).
    """
    from tests.conftest import async_session_test

    async with async_session_test() as session:
        session.add(
            Organization(
                id=org_id, name=f"test-org-{org_id.hex[:8]}", billing_plan="free"
            )
        )
        session.add(
            OrganizationGrantRecord(
                org_id=org_id,
                principal=principal,
                principal_type="human",
                permissions=permissions,
                granted_by="bootstrap",
            )
        )
        await session.commit()


class _CapturedActor:
    """Records the acting principal from inside a request, via a dependency.

    Deliberately NOT by swapping the handler out. The first version of this helper
    replaced `route.endpoint`, which fails for a different reason than it appears
    to: FastAPI keeps the route's `response_model`, so a stub returning `{"ok":
    True}` raises `ResponseValidationError` against `WorkspaceResponse`. Satisfying
    that schema would mean hand-building a full response body in the test — a
    fixture that has to be updated whenever the schema changes, for a test that is
    not about the schema at all.

    An extra dependency appended to the route reads the contextvar at the same
    point a handler would, leaves the real handler and its response model in place,
    and needs no knowledge of either. `app/main.py`'s global guard registration is
    still the thing under test, because the route is the real one on the real app.
    """

    def __init__(self) -> None:
        self.acting = None
        self._route = None
        self._previous = None

    async def _run(self) -> None:
        """What the injected dependency does. Overridden to also raise."""
        from app.adapters.operation_authority_source import acting_principal

        self.acting = acting_principal()

    def attach(self, template: str, method: str = "GET"):
        """Append the probe to one real route's dependency list."""
        from fastapi import Depends
        from fastapi.dependencies.utils import get_parameterless_sub_dependant

        async def _probe():
            await self._run()

        for route in fastapi_app.routes:
            if getattr(route, "path", None) == template and method in getattr(
                route, "methods", set()
            ):
                self._route = route
                # Copied, not aliased: `restore` writes the list back in place, so
                # holding a reference to the live list would restore it to itself.
                self._previous = list(route.dependant.dependencies)
                route.dependant.dependencies.append(
                    get_parameterless_sub_dependant(
                        depends=Depends(_probe), path=template
                    )
                )
                return self
        raise AssertionError(f"no {method} route matching {template!r} on the app")

    def restore(self) -> None:
        if self._route is not None:
            self._route.dependant.dependencies[:] = self._previous


class _GuardRun:
    """Drives the real guard dependency directly, so its context writes are visible.

    WHY NOT THROUGH THE HTTP CLIENT. MEASURED, and this helper exists only because
    of the measurement: on the real app an unreset contextvar write inside a request
    is invisible BOTH to the test function and to every later request, so no
    client-driven assertion can distinguish "reset correctly" from "never reset".
    Bisected to the cause — `BaseHTTPMiddleware` runs the downstream app in a child
    anyio task, which copies the context, so writes below it cannot propagate back
    out. `app/main.py` installs three (`AuditMiddleware`,
    `QuotaEnforcementMiddleware`, `RateLimitMiddleware`); with zero the write escapes,
    with one or more it does not.

    Two consequences, both load-bearing for how the tests below are written:

    * An earlier version of these tests asserted the absence of a leak from a
      SUBSEQUENT request. That is exactly what the isolation makes unobservable, so
      those assertions passed against a guard with its `finally` deleted. Asserting
      a leak is absent at a point where no leak could ever appear is a test of the
      middleware stack, not of this guard.
    * The `finally` in `enforce_domain_authorization` is therefore defense in depth
      today rather than the only thing standing between two tenants — the middleware
      stack would contain a leak anyway. It is still worth keeping and worth testing:
      it holds for direct in-process callers (the harness composition calls the
      resolver outside any request), and it does not depend on a middleware stack
      that a future change could flatten. What it must not do is be *claimed* as the
      isolation boundary.

    So the generator is driven here as FastAPI drives it — `__anext__` then
    `aclose()` — and the contextvar is read at both points in the same context the
    guard runs in. Verified to fail against the mutant that deletes the reset.
    """

    def __init__(self, token: str, path: str, template: str, method: str = "GET"):
        self._token = token
        self._path = path
        self._template = template
        self._method = method
        self.inside = None
        self.after = None

    def _request(self):
        from fastapi import Request

        route = next(
            r
            for r in fastapi_app.routes
            if getattr(r, "path", None) == self._template
            and self._method in getattr(r, "methods", set())
        )
        # A real Request over a hand-built scope, carrying the two things the guard
        # reads from the routing layer: the matched route and its parsed path
        # params. Both are supplied the way Starlette supplies them, which is the
        # reason `_resolve_workspace_id` can be exercised at all.
        return Request(
            {
                "type": "http",
                "method": self._method,
                "path": self._path,
                "headers": [(b"authorization", f"Bearer {self._token}".encode())],
                "query_string": b"",
                "app": fastapi_app,
                "route": route,
                "path_params": _path_params_for(self._template, self._path),
                "state": {},
            }
        )

    async def run(self, inside=None):
        """Enter the guard, observe, then close it and observe again.

        `inside` is an optional coroutine function called while the principal is
        bound; raising from it exercises the unwind path.
        """
        from app.adapters.operation_authority_source import acting_principal
        from app.domain_guard import enforce_domain_authorization
        from fastapi.security import HTTPAuthorizationCredentials

        from tests.conftest import async_session_test

        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=self._token
        )
        async with async_session_test() as db:
            generator = enforce_domain_authorization(self._request(), credentials, db)
            try:
                await generator.__anext__()
                self.inside = acting_principal()
                if inside is not None:
                    await inside()
            finally:
                # `aclose()` and not `athrow()`: FastAPI closes the dependency's
                # context manager on both the success and the failure path, so
                # closing here reproduces the unwind the server performs.
                await generator.aclose()
                self.after = acting_principal()
        return self


def _path_params_for(template: str, path: str) -> dict:
    """Parse `{name}` segments out of a matched template. No routing guesswork."""
    params = {}
    for expected, actual in zip(
        template.strip("/").split("/"), path.strip("/").split("/"), strict=True
    ):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = actual
    return params


async def _seed_two_tenant_research():
    """Create owned, foreign, and unowned rows for the isolation matrix."""
    from tests.conftest import async_session_test

    org_a, workspace_a = await _seed_workspace("workspace:administer")
    org_b, workspace_b = await _seed_workspace("workspace:administer")
    finding_a, finding_b, unowned_finding = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    proposal_a, proposal_b, unowned_proposal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with async_session_test() as session:
        session.add_all(
            [
                ResearchFinding(
                    id=finding_a,
                    workspace_id=workspace_a,
                    source="github",
                    source_url="https://example.invalid/a",
                    title="Tenant A finding",
                    relevance_score=80,
                    scanned_at=now,
                ),
                ResearchFinding(
                    id=finding_b,
                    workspace_id=workspace_b,
                    source="github",
                    source_url="https://example.invalid/b",
                    title="Tenant B finding",
                    relevance_score=90,
                    scanned_at=now,
                ),
                ResearchFinding(
                    id=unowned_finding,
                    workspace_id=None,
                    source="github",
                    source_url="https://example.invalid/legacy",
                    title="Unowned legacy finding",
                    relevance_score=100,
                    scanned_at=now,
                ),
                ResearchProposal(
                    id=proposal_a,
                    workspace_id=workspace_a,
                    title="Tenant A proposal",
                    objective="Validate tenant A behavior",
                    hypothesis="Tenant A remains isolated",
                    status="proposed",
                ),
                ResearchProposal(
                    id=proposal_b,
                    workspace_id=workspace_b,
                    title="Tenant B proposal",
                    objective="Validate tenant B behavior",
                    hypothesis="Tenant B remains isolated",
                    status="proposed",
                ),
                ResearchProposal(
                    id=unowned_proposal,
                    workspace_id=None,
                    title="Unowned legacy proposal",
                    objective="Validate legacy behavior",
                    hypothesis="Unowned data remains hidden",
                    status="proposed",
                ),
            ]
        )
        await session.commit()
    return {
        "org_a": org_a,
        "org_b": org_b,
        "workspace_a": workspace_a,
        "workspace_b": workspace_b,
        "finding_a": finding_a,
        "finding_b": finding_b,
        "proposal_a": proposal_a,
        "proposal_b": proposal_b,
    }


# -- Token policy: the shapes that must never be admitted -------------------


class TestDomainTokenPolicy:
    """R5: which tokens the domain API admits, and which it refuses."""

    def _policy(self):
        return DomainTokenPolicy(
            allowed_client_ids=[TEST_CLIENT_ID], expected_issuer=TEST_ISSUER
        )

    def _claims(self, **overrides):
        base = {
            "sub": "user-abc",
            "token_use": "access",
            "iss": TEST_ISSUER,
            "client_id": TEST_CLIENT_ID,
            "custom:org_id": "org-1",
            "custom:account_type": "human",
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def test_valid_access_token_is_admitted(self):
        principal = self._policy().admit(
            self._claims(), validation_path=TRUSTED_VALIDATION_PATH
        )
        assert principal.subject == "user-abc"
        assert principal.org_id == "org-1"

    def test_id_token_presented_as_access_token_is_refused(self):
        """An ID token is not an API credential, even correctly signed."""
        with pytest.raises(TokenRejectedError, match="access token"):
            self._policy().admit(
                self._claims(token_use="id"), validation_path=TRUSTED_VALIDATION_PATH
            )

    def test_missing_token_use_is_refused(self):
        with pytest.raises(TokenRejectedError, match="access token"):
            self._policy().admit(
                self._claims(token_use=None), validation_path=TRUSTED_VALIDATION_PATH
            )

    def test_wrong_issuer_is_refused(self):
        with pytest.raises(TokenRejectedError, match="issuer"):
            self._policy().admit(
                self._claims(iss="https://evil.example.com/"),
                validation_path=TRUSTED_VALIDATION_PATH,
            )

    def test_non_allowlisted_client_is_refused(self):
        with pytest.raises(TokenRejectedError, match="client"):
            self._policy().admit(
                self._claims(client_id="some-other-app"),
                validation_path=TRUSTED_VALIDATION_PATH,
            )

    def test_denial_does_not_echo_the_client_id(self):
        """A denial must not confirm which values are close to allowlisted."""
        with pytest.raises(TokenRejectedError) as exc:
            self._policy().admit(
                self._claims(client_id="nearly-right-client"),
                validation_path=TRUSTED_VALIDATION_PATH,
            )
        assert "nearly-right-client" not in str(exc.value)

    def test_missing_org_claim_is_refused(self):
        with pytest.raises(TokenRejectedError, match="organization"):
            self._policy().admit(
                self._claims(**{"custom:org_id": None}),
                validation_path=TRUSTED_VALIDATION_PATH,
            )

    def test_unknown_account_type_is_refused(self):
        with pytest.raises(TokenRejectedError, match="account type"):
            self._policy().admit(
                self._claims(**{"custom:account_type": "robot"}),
                validation_path=TRUSTED_VALIDATION_PATH,
            )

    @pytest.mark.parametrize("path", sorted(REJECTED_VALIDATION_PATHS))
    def test_permissive_alternate_validator_cannot_admit(self, path):
        """The weaker validator must not satisfy the stricter policy.

        This is the "alternate validator must not bypass policy" requirement:
        the API-authorizer path checks neither token_use nor a client allowlist,
        so claims it produced are refused before any claim is read — even when
        every claim would otherwise pass.
        """
        with pytest.raises(TokenRejectedError):
            self._policy().admit(self._claims(), validation_path=path)

    def test_unknown_validation_path_is_refused(self):
        with pytest.raises(TokenRejectedError, match="unknown validation path"):
            self._policy().admit(self._claims(), validation_path="something.invented")


class TestStartupRefusesWeakConfiguration:
    """A missing allowlist is an operator error, not a permissive default."""

    def test_empty_allowlist_fails_to_start(self):
        with pytest.raises(TokenPolicyError):
            DomainTokenPolicy(allowed_client_ids=[], expected_issuer=TEST_ISSUER)

    def test_allowlist_of_blanks_fails_to_start(self):
        """Whitespace entries must not count as an allowlist."""
        with pytest.raises(TokenPolicyError):
            DomainTokenPolicy(
                allowed_client_ids=["", "   "], expected_issuer=TEST_ISSUER
            )

    def test_missing_issuer_fails_to_start(self):
        with pytest.raises(TokenPolicyError):
            DomainTokenPolicy(allowed_client_ids=[TEST_CLIENT_ID], expected_issuer="")

    def test_build_policy_raises_when_enforced_without_allowlist(self, monkeypatch):
        """The app-level builder inherits the same refusal."""
        monkeypatch.setattr(settings, "domain_auth_enforced", True)
        monkeypatch.setattr(settings, "cognito_issuer", TEST_ISSUER)
        monkeypatch.setattr(settings, "domain_auth_allowed_client_ids", [])
        with pytest.raises(TokenPolicyError):
            domain_auth.build_domain_policy()

    def test_build_policy_is_none_when_not_enforced(self, monkeypatch):
        monkeypatch.setattr(settings, "domain_auth_enforced", False)
        assert domain_auth.build_domain_policy() is None


# -- Signature verification -------------------------------------------------


class TestTokenSignatureVerification:
    """Authenticity, verified against real keys."""

    def test_valid_signature_returns_claims(self, enforcing):
        claims = domain_auth.verify_access_token(_mint(enforcing))
        assert claims["sub"] == "user-abc"

    def test_unsigned_alg_none_token_is_refused(self, enforcing):
        """`alg: none` must never verify — the classic bypass.

        Hand-assembled rather than minted: the signing library refuses to
        *produce* an `alg: none` token (this held for python-jose and holds for
        PyJWT), but an attacker is under no such constraint, so building the bytes
        directly is the only way to actually probe the verifier instead of probing
        the signing library.
        """
        import base64
        import json

        def b64(payload: dict) -> str:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        forged = (
            f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': TEST_KID})}."
            f"{b64({'sub': 'attacker', 'token_use': 'access', 'exp': 9999999999})}."
        )
        with pytest.raises(TokenRejectedError):
            domain_auth.verify_access_token(forged)

    def test_hs256_token_is_refused(self, enforcing):
        """The algorithm is pinned, so the legacy HS256 token cannot verify here.

        Also covers HS256-with-the-public-key-as-secret confusion: honouring the
        header's algorithm choice is what makes that attack possible.
        """
        forged = jwt.encode(
            {"sub": "attacker", "token_use": "access"},
            key="any-shared-secret",
            algorithm="HS256",
            headers={"kid": TEST_KID},
        )
        with pytest.raises(TokenRejectedError, match="RS256"):
            domain_auth.verify_access_token(forged)

    def test_token_signed_by_a_different_key_is_refused(self, enforcing):
        """A well-formed token from the wrong signer does not verify."""
        other_pem, _ = _rsa_keypair()
        forged = jwt.encode(
            {"sub": "attacker", "token_use": "access", "exp": 9999999999},
            other_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )
        with pytest.raises(TokenRejectedError):
            domain_auth.verify_access_token(forged)

    def test_unknown_kid_is_refused(self, enforcing):
        forged = jwt.encode(
            {"sub": "a", "exp": 9999999999},
            enforcing,
            algorithm="RS256",
            headers={"kid": "not-published"},
        )
        with pytest.raises(TokenRejectedError, match="key id"):
            domain_auth.verify_access_token(forged)

    def test_token_without_kid_is_refused(self, enforcing):
        forged = jwt.encode({"sub": "a", "exp": 9999999999}, enforcing, "RS256")
        # The library omits `kid` only if not supplied; assert on behaviour rather
        # than on that promise, so the test survives a library change either way.
        header = jwt.get_unverified_header(forged)
        if "kid" not in header:
            with pytest.raises(TokenRejectedError, match="key id"):
                domain_auth.verify_access_token(forged)

    def test_expired_token_is_refused(self, enforcing):
        expired = _mint(
            enforcing,
            exp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
        )
        with pytest.raises(TokenRejectedError):
            domain_auth.verify_access_token(expired)

    def test_garbage_token_is_refused_not_crashed(self, enforcing):
        with pytest.raises(TokenRejectedError):
            domain_auth.verify_access_token("not-a-jwt")

    def test_non_string_kid_is_refused_not_a_type_error(self, enforcing):
        """A JWT header is attacker-controlled JSON, so `kid` may be any type.

        `{"kid": {...}}` is a well-formed header that `get_unverified_header`
        returns happily, and an unhashable value then raised `TypeError` out of
        the key-cache dict lookup — a 500 on a path reachable with no credential
        at all, where this module's contract is that a bad token is a 401.

        HAND-ASSEMBLED, for the same reason as the `alg: none` case above. This test
        used to mint the token with the signing library, which worked under
        python-jose. PyJWT validates its own `kid` on the ENCODE side
        (`PyJWS._validate_kid`) and raises `InvalidTokenError` rather than producing
        the token, so after issue #5601 (S02) minting it here failed in the test
        helper and never reached the verifier at all.

        That is a signing-side courtesy, NOT a defence: an attacker writes the bytes
        directly and PyJWT's encode-side check never runs. So the bytes are built by
        hand to keep probing the verifier rather than the signing library.

        WHICH BRANCH REFUSES IT NOW, stated precisely because it moved. PyJWT also
        validates `kid` on the DECODE side, inside `get_unverified_header`, so the
        forged token is refused one branch earlier than before — as "token header is
        unreadable" rather than by this module's own `isinstance(kid, str)` check.
        Under python-jose the header parsed fine and that check was the only thing
        between an unhashable `kid` and a `TypeError` out of the cache lookup.

        The assertion is therefore on the OUTCOME — a `TokenRejectedError`, i.e. a
        401 — and not on the message, because the message now names a different
        branch and pinning it would assert an implementation detail of the library.
        This module's guard is kept as defence in depth: it is what holds if a future
        library version stops validating `kid` for us, and it costs one `isinstance`.
        """
        import base64
        import json

        def b64(payload: dict) -> str:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        # The signature is deliberately junk: `kid` is rejected before any key
        # lookup or signature check, so a real signature would prove nothing here.
        forged = (
            f"{b64({'alg': 'RS256', 'typ': 'JWT', 'kid': {'nested': 'object'}})}."
            f"{b64({'sub': 'a', 'exp': 9999999999})}."
            "c2lnbmF0dXJl"
        )
        with pytest.raises(TokenRejectedError):
            domain_auth.verify_access_token(forged)

    def test_module_kid_guard_still_holds_if_the_library_stops_checking(
        self, enforcing, monkeypatch
    ):
        """The defence-in-depth half of the test above, exercised directly.

        The previous test can no longer reach this module's `isinstance(kid, str)`
        guard, because PyJWT rejects a non-string `kid` inside
        `get_unverified_header` first. That makes the guard unreachable in practice
        and therefore untested — and untested code is what quietly stops working.

        So the library's check is stubbed out to simulate a future version that does
        not perform it, leaving this module's guard as the only thing between an
        unhashable `kid` and a `TypeError` out of the `jwks_cache` dict lookup. That
        TypeError would be an unauthenticated 500; the contract is a 401.
        """
        monkeypatch.setattr(
            domain_auth.jwt,
            "get_unverified_header",
            lambda token: {"alg": "RS256", "kid": {"nested": "object"}},
        )
        with pytest.raises(TokenRejectedError, match="key id"):
            domain_auth.verify_access_token("irrelevant-the-header-is-stubbed")

    def test_unusable_published_key_is_refused_not_a_server_error(
        self, enforcing, monkeypatch
    ):
        """A structurally unusable published key is a denial, not a 500.

        Both cases are reachable by an unauthenticated request — the JWKS content
        is the user pool's, not the caller's, but the caller chooses the `kid` that
        selects which entry is used — so neither may surface as a server error.

        Under python-jose these escaped through two DIFFERENT holes, which is why
        both are still asserted: `JWKError` was a SIBLING of `JWTError` rather than
        a subclass, so it passed straight through a `JWTError`-only handler, and the
        underlying key construction raised a bare `ValueError` that was in no jose
        hierarchy at all. Issue #5601 (S02) moved this path to PyJWT, where both now
        raise `InvalidKeyError` under the single `PyJWTError` root. The test is kept
        at full breadth rather than narrowed to the new library's behaviour: it
        pins the OUTCOME (a denial) for the same two malformed keys, so it would
        still catch a regression if the key construction moved back out of the
        handled hierarchy.
        """
        # `kty: oct` is the wrong key type for RS256 ("Not an RSA key"). A short `n`
        # on an RSA key fails the construction itself ("e must be >= 3 and < n").
        for unusable in (
            {"kty": "oct", "kid": TEST_KID, "k": "c2VjcmV0"},
            {"kty": "RSA", "kid": TEST_KID, "n": "AQAB", "e": "AQAB"},
        ):
            monkeypatch.setattr(domain_auth.jwks_cache, "_keys", {TEST_KID: unusable})
            with pytest.raises(TokenRejectedError):
                domain_auth.verify_access_token(_mint(enforcing))

    def test_unreachable_jwks_is_a_denial_not_a_server_error(
        self, enforcing, monkeypatch
    ):
        """An unreachable key endpoint is an outage, but not a 500 to the caller.

        A missing JWKS *URL* stays a `TokenPolicyError` (operator
        misconfiguration); a transport failure fetching a configured URL is
        answered as a denial and logged with its real cause.
        """
        cache = type(domain_auth.jwks_cache)()

        def _boom() -> None:
            raise OSError("connection refused")

        monkeypatch.setattr(cache, "_fetch", _boom)
        monkeypatch.setattr(domain_auth, "jwks_cache", cache)
        with pytest.raises(TokenRejectedError, match="unavailable"):
            domain_auth.verify_access_token(_mint(enforcing))


# -- Workspace authorization over HTTP, through the real guard --------------


class TestWorkspaceAuthorizationEnforcement:
    """R6: authority is a server-held grant, re-read per operation."""

    @pytest.mark.asyncio
    async def test_signed_access_token_passes_but_signed_id_token_is_401(
        self, client, enforcing
    ):
        org_id, workspace_id = await _seed_workspace("workspace:read")
        claims = {"custom:org_id": str(org_id)}
        access_token = _mint(enforcing, **claims)
        id_token = _mint(
            enforcing, **claims, token_use="id", client_id=None, aud=TEST_CLIENT_ID
        )

        access_response = await client.get(
            f"/workspaces/{workspace_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        id_response = await client.get(
            f"/workspaces/{workspace_id}",
            headers={"Authorization": f"Bearer {id_token}"},
        )

        assert access_response.status_code == 200
        assert id_response.status_code == 401
        assert id_response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        ("client_id", "audience"),
        [("another-client", None), (None, TEST_CLIENT_ID)],
    )
    @pytest.mark.asyncio
    async def test_signed_non_allowlisted_client_is_refused(
        self, client, enforcing, client_id, audience
    ):
        org_id, workspace_id = await _seed_workspace("workspace:read")
        org_claim = {"custom:org_id": str(org_id)}
        token = _mint(enforcing, **org_claim, client_id=client_id, aud=audience)
        response = await client.get(
            f"/workspaces/{workspace_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.asyncio
    async def test_internal_credential_cannot_authenticate_domain_route(
        self, client, enforcing, internal_token_header
    ):
        _, workspace_id = await _seed_workspace("workspace:read")
        response = await client.get(
            f"/workspaces/{workspace_id}", headers=internal_token_header
        )

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.asyncio
    async def test_no_token_is_401(self, client, enforcing):
        _, workspace_id = await _seed_workspace("workspace:read")
        response = await client.get(f"/workspaces/{workspace_id}")
        assert response.status_code == 401
        # A 401 must say how to authenticate; a bare 401 is a worse API and
        # tempts clients into retry loops.
        assert "WWW-Authenticate" in response.headers

    @pytest.mark.asyncio
    async def test_org_member_without_a_grant_is_403(self, client, enforcing):
        """The regression that motivated the story.

        The caller is a legitimate, fully authenticated member of the org that
        owns the workspace. Before this change the org claim alone was the
        authority, so this call succeeded. It must now be refused: org
        membership is not workspace authority.
        """
        org_id, workspace_id = await _seed_workspace(None)
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            f"/workspaces/{workspace_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_grant_with_insufficient_permission_is_403(self, client, enforcing):
        """READ does not imply PROVISION.

        `POST /kubeconfig` returns live cluster credentials, so a read-only
        grant must not reach it even on a workspace the caller may read.
        """
        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.post(
            f"/workspaces/{workspace_id}/kubeconfig",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_peer_workspace_grant_cannot_authorize_kubeconfig(
        self, client, enforcing
    ):
        from tests.conftest import async_session_test

        org_id, _ = await _seed_workspace("workspace:provision")
        target_workspace = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=target_workspace,
                    org_id=org_id,
                    name="ungranted-peer",
                    isolation_mode="shared",
                    status="active",
                )
            )
            await session.commit()
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.post(
            f"/workspaces/{target_workspace}/kubeconfig",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_refusals_are_indistinguishable_across_causes(
        self, client, enforcing
    ):
        """Cross-org, nonexistent and ungranted must be one identical answer.

        A 404-vs-403 split would let a caller enumerate which workspace ids exist
        in other organizations — but so would an identical status code with a
        differing ``detail`` string, which is what an earlier version of this
        code did. All three causes must return the same status AND the same body.
        """
        _, other_org_workspace = await _seed_workspace("workspace:administer")
        own_org, ungranted = await _seed_workspace(None)

        cross_org_token = _mint(enforcing, **{"custom:org_id": str(uuid.uuid4())})
        own_org_token = _mint(enforcing, **{"custom:org_id": str(own_org)})

        cross_org = await client.get(
            f"/workspaces/{other_org_workspace}",
            headers={"Authorization": f"Bearer {cross_org_token}"},
        )
        nonexistent = await client.get(
            f"/workspaces/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {cross_org_token}"},
        )
        no_grant = await client.get(
            f"/workspaces/{ungranted}",
            headers={"Authorization": f"Bearer {own_org_token}"},
        )

        assert cross_org.status_code == nonexistent.status_code == 403
        assert no_grant.status_code == 403
        assert cross_org.json() == nonexistent.json() == no_grant.json(), (
            "the refusal reason distinguishes these cases, which makes workspace "
            "ids and grant state enumerable"
        )

    @pytest.mark.asyncio
    async def test_permission_revoked_after_admission_is_denied(
        self, client, enforcing
    ):
        """R6's core claim: authority is re-checked when the operation runs.

        The token stays valid the whole time — it is not re-issued and not
        expired. Only the server-held grant changes. If authority were inherited
        from sign-in, the second call would still succeed.
        """
        from sqlalchemy import update

        from tests.conftest import async_session_test

        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        headers = {"Authorization": f"Bearer {token}"}

        first = await client.get(f"/workspaces/{workspace_id}", headers=headers)
        assert first.status_code not in (401, 403), (
            "precondition: the granted caller must be authorized before revocation"
        )

        async with async_session_test() as session:
            await session.execute(
                update(WorkspaceGrantRecord)
                .where(WorkspaceGrantRecord.workspace_id == workspace_id)
                .values(revoked_at=datetime.now(UTC))
            )
            await session.commit()

        second = await client.get(f"/workspaces/{workspace_id}", headers=headers)
        assert second.status_code == 403

    @pytest.mark.asyncio
    async def test_unrecognized_stored_permission_grants_nothing(
        self, client, enforcing
    ):
        """A permission value this build cannot reason about confers no access."""
        org_id, workspace_id = await _seed_workspace("workspace:invented_power")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            f"/workspaces/{workspace_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_service_credential_cannot_use_a_grant_issued_to_a_human(
        self, client, enforcing
    ):
        """R5 acc. 6-7: a grant is bound to a principal AND its verified type.

        The grant here was issued to the human ``user-abc``. A caller presenting
        the identical subject string but authenticated as a service must not be
        able to exercise it — a service's ability to authenticate never delegates
        a human's authority to it. Before this fix, `load_workspace_authorization`
        looked the grant up by subject alone, so this call succeeded.
        """
        org_id, workspace_id = await _seed_workspace(
            "workspace:read", principal="user-abc", principal_type="human"
        )
        token = _mint(
            enforcing,
            sub="user-abc",
            **{"custom:org_id": str(org_id), "custom:account_type": "service"},
        )
        response = await client.get(
            f"/workspaces/{workspace_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_human_credential_cannot_use_a_grant_issued_to_a_service(
        self, client, enforcing
    ):
        """The converse of the case above, for the same reason.

        The grant here was issued to a service principal. A human presenting the
        same subject string must not inherit it.
        """
        org_id, workspace_id = await _seed_workspace(
            "workspace:read", principal="worker-svc", principal_type="service"
        )
        token = _mint(
            enforcing,
            sub="worker-svc",
            **{"custom:org_id": str(org_id), "custom:account_type": "human"},
        )
        response = await client.get(
            f"/workspaces/{workspace_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_service_grant_is_scoped_and_rechecked(self, client, enforcing):
        from sqlalchemy import update
        from tests.conftest import async_session_test

        org_id, workspace_id = await _seed_workspace(
            "workspace:read", principal="worker-svc", principal_type="service"
        )
        token = _mint(
            enforcing,
            sub="worker-svc",
            **{"custom:org_id": str(org_id), "custom:account_type": "service"},
        )
        headers = {"Authorization": f"Bearer {token}"}

        allowed = await client.get(f"/workspaces/{workspace_id}", headers=headers)
        denied = await client.post(
            f"/workspaces/{workspace_id}/kubeconfig", headers=headers
        )
        assert allowed.status_code == 200
        assert denied.status_code == 403

        async with async_session_test() as session:
            await session.execute(
                update(WorkspaceGrantRecord)
                .where(WorkspaceGrantRecord.workspace_id == workspace_id)
                .values(revoked_at=datetime.now(UTC))
            )
            await session.commit()

        revoked = await client.get(f"/workspaces/{workspace_id}", headers=headers)
        assert revoked.status_code == 403

    @pytest.mark.asyncio
    async def test_role_claims_never_replace_workspace_grants(self, client, enforcing):
        """Synthetic role-like claims do not confer production authority."""
        org_id, workspace_id = await _seed_workspace(
            "workspace:read", principal="granted-user"
        )
        granted_token = _mint(
            enforcing,
            sub="granted-user",
            **{"custom:org_id": str(org_id), "role": "workspace_viewer"},
        )
        claimed_owner = _mint(
            enforcing,
            sub="ungranted-user",
            **{
                "custom:org_id": str(org_id),
                "role": "workspace_owner",
                "custom:role": "workspace_owner",
            },
        )
        path = f"/workspaces/{workspace_id}"
        allowed = await client.get(
            path, headers={"Authorization": f"Bearer {granted_token}"}
        )
        denied = await client.get(
            path, headers={"Authorization": f"Bearer {claimed_owner}"}
        )
        assert allowed.status_code == 200
        assert denied.status_code == 403

    @pytest.mark.asyncio
    async def test_administer_implies_read_via_policy_closure(self, client, enforcing):
        """The implication closure comes from the policy, not from this service."""
        org_id, workspace_id = await _seed_workspace("workspace:administer")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            f"/workspaces/{workspace_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code not in (401, 403)

    @pytest.mark.asyncio
    async def test_malformed_workspace_id_is_403_not_422(self, client, enforcing):
        """The guard runs before the handler and must not leak existence."""
        response = await client.get(
            "/workspaces/not-a-uuid",
            headers={"Authorization": f"Bearer {_mint(enforcing)}"},
        )
        assert response.status_code in (401, 403)


class TestNoPermissionUnionAcrossOrganizationsOrWorkspaces:
    """DESIGN.md 2.2 acceptance: same subject/org is necessary, never sufficient.

    Two scenarios the issue names explicitly:

    1. One human, ADP-authenticated into two different organizations, must not
       carry either organization's workspace access into the other — selecting
       organization A must not expose what was only ever granted in B.
    2. Two different principals in the SAME organization, each granted a
       different workspace, must not reach each other's workspace even though
       both hold a real grant and both authenticate for the same org.
    """

    @pytest.mark.asyncio
    async def test_one_human_in_two_organizations_never_unions_their_grants(
        self, client, enforcing
    ):
        """Same verified subject, two organizations, disjoint authority.

        The subject `shared-human` holds `workspace:administer` on a workspace
        in org A and nothing at all in org B. Authenticating with org B in the
        token's organization claim must refuse both — the workspace in A
        (org mismatch) and any workspace in B (no grant there) — even though
        it is genuinely the same ADP-verified human both times.
        """
        subject = "shared-human"
        org_a, workspace_a = await _seed_workspace(
            "workspace:administer", principal=subject
        )
        org_b, workspace_b = await _seed_workspace(None, principal=subject)

        token_as_org_a = _mint(enforcing, sub=subject, **{"custom:org_id": str(org_a)})
        token_as_org_b = _mint(enforcing, sub=subject, **{"custom:org_id": str(org_b)})

        # Selecting org A: the grant that belongs there is honoured.
        as_a = await client.get(
            f"/workspaces/{workspace_a}",
            headers={"Authorization": f"Bearer {token_as_org_a}"},
        )
        assert as_a.status_code not in (401, 403)

        # The SAME human, now selecting org B, must not reach A's workspace —
        # the token's organization claim does not match A's, so A's grant does
        # not apply.
        cannot_reach_a_as_b = await client.get(
            f"/workspaces/{workspace_a}",
            headers={"Authorization": f"Bearer {token_as_org_b}"},
        )
        assert cannot_reach_a_as_b.status_code == 403

        # And selecting org B grants nothing there either — no grant was ever
        # issued to this subject in B, so B's own membership is not a fallback.
        cannot_reach_b_as_b = await client.get(
            f"/workspaces/{workspace_b}",
            headers={"Authorization": f"Bearer {token_as_org_b}"},
        )
        assert cannot_reach_b_as_b.status_code == 403

    @pytest.mark.asyncio
    async def test_same_organization_disjoint_workspace_grants_stay_isolated(
        self, client, enforcing
    ):
        """Two principals, one organization, non-overlapping workspace grants.

        `user-u` may administer workspace U; `user-v` may administer workspace V.
        Both belong to the same organization and both hold a real, unrevoked
        grant — so this is not the "no grant at all" case R6 already covers.
        Neither may reach the other's workspace.
        """
        from tests.conftest import async_session_test

        org_id, workspace_u = await _seed_workspace(
            "workspace:administer", principal="user-u"
        )
        # A second workspace + grant in the SAME organization, added directly
        # rather than through `_seed_workspace` again: that helper (re)creates
        # the Organization row every call and would collide on its unique name.
        workspace_v = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=workspace_v,
                    org_id=org_id,
                    name="ws-v",
                    isolation_mode="shared",
                    status="active",
                )
            )
            session.add(
                WorkspaceGrantRecord(
                    id=uuid.uuid4(),
                    workspace_id=workspace_v,
                    org_id=org_id,
                    principal="user-v",
                    principal_type="human",
                    permissions="workspace:administer",
                )
            )
            await session.commit()

        token_u = _mint(enforcing, sub="user-u", **{"custom:org_id": str(org_id)})
        token_v = _mint(enforcing, sub="user-v", **{"custom:org_id": str(org_id)})

        u_reads_own = await client.get(
            f"/workspaces/{workspace_u}",
            headers={"Authorization": f"Bearer {token_u}"},
        )
        assert u_reads_own.status_code not in (401, 403)

        v_reads_own = await client.get(
            f"/workspaces/{workspace_v}",
            headers={"Authorization": f"Bearer {token_v}"},
        )
        assert v_reads_own.status_code not in (401, 403)

        u_reads_v = await client.get(
            f"/workspaces/{workspace_v}",
            headers={"Authorization": f"Bearer {token_u}"},
        )
        assert u_reads_v.status_code == 403

        v_reads_u = await client.get(
            f"/workspaces/{workspace_u}",
            headers={"Authorization": f"Bearer {token_v}"},
        )
        assert v_reads_u.status_code == 403


@pytest.mark.asyncio
async def test_explicit_adp_org_switch_and_current_grant_revocation(client, enforcing, monkeypatch):
    from app.current_identity import CurrentIdentity
    from app.config import settings

    monkeypatch.setattr(settings, "current_identity_enforced", True)

    class Memberships:
        async def read(self, *, subject, principal_type, adp_org_id):
            return CurrentIdentity(subject, principal_type, adp_org_id, "membership-1", True, True)

    monkeypatch.setattr(fastapi_app.state, "current_identity_reader", Memberships(), raising=False)
    from datetime import UTC, datetime
    from sqlalchemy import select
    from tests.conftest import async_session_test

    scopes = [await _seed_workspace("workspace:read") for _ in range(2)]
    async with async_session_test() as session:
        for index, (org_id, _) in enumerate(scopes):
            org = await session.get(Organization, org_id)
            org.adp_org_id = f"adp-selected-{index}"
        await session.commit()
    for index, (_, workspace_id) in enumerate(scopes):
        token = _mint(enforcing, **{"custom:org_id": f"adp-selected-{index}"})
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.get(f"/workspaces/{workspace_id}", headers=headers)
        ).status_code == 200
        peer = scopes[1 - index][1]
        assert (
            await client.get(f"/workspaces/{peer}", headers=headers)
        ).status_code == 403
    async with async_session_test() as session:
        grant = await session.scalar(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == scopes[1][1]
            )
        )
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    # Same signed token, fresh request/session: no cached grant survives.
    assert (
        await client.get(f"/workspaces/{scopes[1][1]}", headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("account_type", ["human", "service"])
async def test_bound_user_management_never_mutates_global_cognito(
    client, enforcing, monkeypatch, account_type
):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.models.user import User
    from tests.conftest import async_session_test
    from app.current_identity import CurrentIdentity
    from app.config import settings

    monkeypatch.setattr(settings, "current_identity_enforced", True)

    class Memberships:
        async def read(self, *, subject, principal_type, adp_org_id):
            return CurrentIdentity(
                subject, principal_type, adp_org_id, "membership-1", True, True,
                "delegation-1" if principal_type == "service" else None,
            )

    monkeypatch.setattr(fastapi_app.state, "current_identity_reader", Memberships(), raising=False)

    org_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _seed_organization_grant(org_id, "user-abc", "organization:administer")
    async with async_session_test() as session:
        org = await session.get(Organization, org_id)
        org.adp_org_id = "adp-managed-membership"
        grant = await session.scalar(
            select(OrganizationGrantRecord).where(
                OrganizationGrantRecord.org_id == org_id
            )
        )
        grant.principal_type = account_type
        session.add(
            User(
                id=member_id,
                org_id=org_id,
                email="member@example.com",
                cognito_sub="immutable-member",
                role="developer",
                status="active",
            )
        )
        await session.commit()
    create = AsyncMock()
    disable, change_role = AsyncMock(), AsyncMock()
    monkeypatch.setattr("app.routers.users.admin_create_user", create)
    monkeypatch.setattr("app.routers.users.admin_disable_user", disable)
    monkeypatch.setattr("app.routers.users.admin_update_user_role", change_role)
    token = _mint(
        enforcing,
        **{
            "custom:org_id": "adp-managed-membership",
            "custom:account_type": account_type,
        },
    )
    response = await client.post(
        "/users/invite",
        json={"email": "new-member@example.com", "role": "developer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == (409 if account_type == "human" else 403)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.patch(
        f"/users/{member_id}/role", json={"role": "workspace-admin"}, headers=headers
    )
    assert response.status_code == (409 if account_type == "human" else 403)
    response = await client.delete(f"/users/{member_id}", headers=headers)
    assert response.status_code == (409 if account_type == "human" else 403)
    create.assert_not_awaited()
    disable.assert_not_awaited()
    change_role.assert_not_awaited()


class TestIdentitySpoofing:
    """Identity comes from verified claims only — never from the request."""

    @pytest.mark.asyncio
    async def test_spoofed_identity_headers_do_not_grant_access(
        self, client, enforcing
    ):
        """Client-supplied identity headers must not become authority.

        The grant belongs to `victim-user`; the caller's verified subject does
        not. Naming the victim in every identity header the platform recognizes
        must not move the decision.
        """
        org_id, workspace_id = await _seed_workspace(
            "workspace:administer", principal="victim-user"
        )
        token = _mint(enforcing, sub="attacker-user", **{"custom:org_id": str(org_id)})
        response = await client.get(
            f"/workspaces/{workspace_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-ADP-Principal": "victim-user",
                "X-Caller-Id": "victim-user",
                "X-Superplane-User": "victim-user",
                "X-Auth-Subject": "victim-user",
                "X-Forwarded-User": "victim-user",
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_spoofed_organization_headers_cannot_switch_tenant(
        self, client, enforcing
    ):
        own_org, own_workspace = await _seed_workspace("workspace:read")
        foreign_org, foreign_workspace = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(own_org)})
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Org-Id": str(foreign_org),
            "X-ADP-Org-Id": str(foreign_org),
            "X-Forwarded-Org-Id": str(foreign_org),
        }
        own = await client.get(f"/workspaces/{own_workspace}", headers=headers)
        foreign = await client.get(f"/workspaces/{foreign_workspace}", headers=headers)

        assert own.status_code == 200
        assert foreign.status_code == 403

    @pytest.mark.asyncio
    async def test_identity_headers_are_stripped_from_request_state(
        self, client, enforcing
    ):
        """The sanitized mapping actually drops the spoofable headers."""
        from superplane_auth.policy import strip_identity_headers

        safe = strip_identity_headers(
            {
                "authorization": "Bearer x",
                "content-type": "application/json",
                "x-adp-principal": "victim",
                "x-caller-id": "victim",
                "x-forwarded-user": "victim",
            }
        )
        assert "content-type" in safe
        assert not [k for k in safe if k.lower().startswith(("x-adp-", "x-caller-"))]
        assert "x-forwarded-user" not in {k.lower() for k in safe}

    @pytest.mark.asyncio
    async def test_mounted_route_exposes_only_sanitized_headers(
        self, client, enforcing, monkeypatch
    ):
        observed = []
        verify = domain_auth.require_verified_caller

        async def capture(request, credentials):
            caller = await verify(request, credentials)
            observed.append((request.state.safe_headers, caller))
            return caller

        monkeypatch.setattr(domain_auth, "require_verified_caller", capture)
        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            f"/workspaces/{workspace_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-ADP-Principal": "other-user",
                "X-Caller-Id": "other-user",
                "X-Org-Id": "other-org",
                "X-Forwarded-User": "other-user",
                "X-Probe-Metadata": "kept",
            },
        )

        assert response.status_code == 200
        assert len(observed) == 1
        safe_headers, caller = observed[0]
        assert safe_headers == caller.safe_headers
        assert safe_headers["x-probe-metadata"] == "kept"
        assert all(
            header not in safe_headers
            for header in (
                "x-adp-principal", "x-caller-id", "x-org-id", "x-forwarded-user"
            )
        )
        assert caller.principal.subject == "user-abc"
        assert caller.principal.org_id == str(org_id)

    @pytest.mark.asyncio
    async def test_body_supplied_approver_is_not_trusted(self, client, enforcing):
        """The legacy `approved_by` body field must not become the actor.

        Proposal approval recorded whatever the caller typed. With enforcement
        on, an unauthorized caller cannot reach the handler at all — so the
        field cannot be used to attribute an approval to someone else.
        """
        response = await client.patch(
            f"/api/v1/research/proposals/{uuid.uuid4()}/approve",
            json={"approved_by": "someone-elses-name"},
            headers={"Authorization": f"Bearer {_mint(enforcing)}"},
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_recorded_actor_prefers_verified_caller_over_body(self):
        """When a caller IS verified, the body value is ignored outright."""
        from types import SimpleNamespace

        from app.routers.research import _recorded_actor

        verified = SimpleNamespace(
            state=SimpleNamespace(
                caller=SimpleNamespace(
                    principal=SimpleNamespace(subject="verified-subject")
                )
            )
        )
        assert _recorded_actor(verified, "claimed-by-body") == "verified-subject"


class TestUnauthenticatedRoutesAreRefused:
    """Every previously-open surface now requires a credential."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/internal/heartbeat"),
            ("post", "/internal/cost-reconcile"),
        ],
    )
    async def test_internal_routes_require_the_shared_token(self, client, method, path):
        """These two had no authentication at all before this story.

        Asserted as 401/403 rather than "not 200": a required header would make
        this a 422, which is an unauthenticated request being reported as a
        malformed one.
        """
        response = await getattr(client, method)(path, json={})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_internal_route_rejects_a_wrong_token(
        self, client, internal_token_header
    ):
        response = await client.post(
            "/internal/heartbeat", json={}, headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/research/findings"),
            ("get", "/api/v1/research/proposals"),
            ("post", "/api/v1/research/scan"),
        ],
    )
    async def test_research_routes_require_a_domain_token(
        self, client, enforcing, method, path
    ):
        """Fifteen research routes had no auth dependency before this story."""
        kwargs = {"json": {}} if method == "post" else {}
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code in (401, 403)


# -- Route inventory: the property that keeps enforcement total -------------


class TestRouteInventoryCoverage:
    """Every mounted route must carry a recorded authorization decision."""

    def _mounted(self):
        """The routes the app really serves.

        Delegates to `app/endpoint_inventory.py::mounted_operations`, which is
        shared with the other enumeration sites and refuses to return an empty
        set. This helper used to do its own `isinstance(route, APIRoute)` walk over
        `app.routes` and, after FastAPI started storing included routers lazily,
        found ZERO routes — so every assertion below passed while examining
        nothing. See issue #5682 (A02).
        """
        return mounted_operations(fastapi_app)

    def test_enumeration_finds_the_routes_it_is_supposed_to_check(self):
        """The guard on the guard.

        Every other test in this class compares the mounted set against the
        inventory, and `mounted - inventoried` is empty when `mounted` is empty.
        So an enumeration that breaks makes this whole class pass vacuously —
        which is exactly what happened. Asserting a plausible floor here means a
        future framework change fails loudly instead of going quietly green.
        """
        mounted = self._mounted()
        assert len(mounted) > 50, (
            f"only {len(mounted)} routes enumerated; the app serves far more, so "
            "the inventory checks in this class are not examining the real surface"
        )
        assert ("GET", "/api/v1/research/findings") in mounted
        assert ("POST", "/workspaces/{workspace_id}/kubeconfig") in mounted

    def test_enumeration_refuses_to_report_an_empty_app_as_success(self):
        """An app with no routes raises rather than returning an empty set."""
        from fastapi import FastAPI

        from app.endpoint_inventory import NoRoutesEnumerated

        empty = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        with pytest.raises(NoRoutesEnumerated):
            mounted_operations(empty)

    def test_every_mounted_route_is_inventoried(self):
        """A new route cannot ship without a decision — collection fails first.

        This is the test that makes the inventory an inventory rather than a
        list. Adding a route without classifying it fails CI here, which is
        strictly better than failing closed in production (which it also does).
        """
        missing = sorted(self._mounted() - all_inventoried())
        assert not missing, (
            "these routes have no recorded authorization decision in "
            f"app/endpoint_inventory.py: {missing}"
        )

    def test_inventory_has_no_routes_the_app_does_not_serve(self):
        """A stale entry is dead configuration that reads as coverage."""
        stale = sorted(all_inventoried() - self._mounted())
        assert not stale, f"inventory lists routes the app does not serve: {stale}"

    def test_every_route_classifies_without_raising(self):
        for method, path in sorted(self._mounted()):
            route_class, requirement = classify(method, path)
            assert route_class is not None
            if requirement is not None:
                scope, permission = requirement
                assert isinstance(permission, Permission)

    def test_sensitive_routes_are_not_classified_public(self):
        """Pin the classifications whose misfiling would be most costly."""
        for method, path in [
            ("POST", "/workspaces/{workspace_id}/kubeconfig"),
            ("DELETE", "/workspaces/{workspace_id}"),
            ("POST", "/auth/token"),
            ("PATCH", "/api/v1/research/proposals/{proposal_id}/approve"),
        ]:
            route_class, requirement = classify(method, path)
            assert route_class.value == "domain", (
                f"{method} {path} is not a domain route"
            )
            assert requirement is not None

    @pytest.mark.parametrize(
        "method,path,expected_class,expected_scope",
        [
            ("GET", "/health", "public", None),
            ("GET", "/workspaces", "domain", "organization"),
            ("POST", "/workspaces", "domain", "organization"),
            ("GET", "/accounts", "domain", "organization"),
            ("GET", "/workspaces/{workspace_id}", "domain", "workspace"),
            (
                "PATCH",
                "/api/v1/research/proposals/{proposal_id}/approve",
                "domain",
                "organization",
            ),
            (
                "POST",
                "/operation-approvals/{approval_id}/decision",
                "domain",
                "organization",
            ),
            ("POST", "/internal/heartbeat", "internal", None),
            ("POST", "/internal/cost-reconcile", "internal", None),
            ("POST", "/internal/provider-operations", "internal", None),
        ],
    )
    def test_mounted_route_families_retain_their_class_and_scope(
        self, method, path, expected_class, expected_scope
    ):
        assert (method, path) in self._mounted()
        route_class, requirement = classify(method, path)
        assert route_class.value == expected_class
        assert (requirement[0].value if requirement else None) == expected_scope

    def test_kubeconfig_requires_provision_not_read(self):
        """It reads like a getter and it hands out live cluster credentials."""
        _, (_, permission) = classify("POST", "/workspaces/{workspace_id}/kubeconfig")
        assert permission is Permission.PROVISION

    def test_no_domain_route_is_also_public_or_internal(self):
        """Two classifications for one route means one of them is not enforced."""
        from app.endpoint_inventory import INTERNAL_ROUTES, PUBLIC_ROUTES

        domain = set(DOMAIN_ROUTES) | set(PRIVATE_DOMAIN_ROUTES)
        assert not set(DOMAIN_ROUTES) & set(PRIVATE_DOMAIN_ROUTES)
        assert not domain & PUBLIC_ROUTES
        assert not domain & INTERNAL_ROUTES
        assert not PUBLIC_ROUTES & INTERNAL_ROUTES

    def test_the_research_route_count_in_the_docs_matches_reality(self):
        """The prose count is load-bearing, so it is asserted rather than trusted.

        `app/endpoint_inventory.py` and `app/domain_guard.py` both justify this
        design by naming how many `/api/v1/research/*` routes shipped with no
        authentication. An earlier draft said "fifteen" while the app serves
        twelve, and a wrong number in the rationale is how a reader concludes
        the inventory was checked against something it was not. If the surface
        changes, update the count in BOTH docstrings and here.
        """
        import app.domain_guard as guard_module
        from app import endpoint_inventory

        served = {
            (method, path)
            for method, path in self._mounted()
            if path.startswith("/api/v1/research/")
        }
        assert len(served) == 13, (
            f"the research surface is now {len(served)} routes; update the count "
            "in endpoint_inventory.py and domain_guard.py docstrings"
        )
        assert served <= set(DOMAIN_ROUTES), (
            "a research route escaped the domain classification"
        )
        for module in (endpoint_inventory, guard_module):
            assert "twelve ``/api/v1/research/*``" in (module.__doc__ or ""), (
                f"{module.__name__}'s docstring no longer states the real count"
            )


class TestResearchTenantIsolation:
    """Research rows are owned through their server-held workspace record."""

    @staticmethod
    def _headers(enforcing, org_id):
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        return {"Authorization": f"Bearer {token}"}

    @pytest.mark.asyncio
    async def test_lists_exclude_other_tenants_and_unowned_rows(
        self, client, enforcing
    ):
        seeded = await _seed_two_tenant_research()
        headers = self._headers(enforcing, seeded["org_a"])

        findings = await client.get("/api/v1/research/findings", headers=headers)
        proposals = await client.get("/api/v1/research/proposals", headers=headers)

        assert findings.status_code == 200
        assert findings.json()["total"] == 1
        assert {item["id"] for item in findings.json()["items"]} == {
            str(seeded["finding_a"])
        }
        assert proposals.status_code == 200
        assert proposals.json()["total"] == 1
        assert {item["id"] for item in proposals.json()["items"]} == {
            str(seeded["proposal_a"])
        }

    @pytest.mark.asyncio
    async def test_stats_are_tenant_scoped(self, client, enforcing):
        seeded = await _seed_two_tenant_research()
        headers = self._headers(enforcing, seeded["org_a"])

        finding_stats = await client.get("/api/v1/research/stats", headers=headers)
        proposal_stats = await client.get(
            "/api/v1/research/proposals/stats", headers=headers
        )

        assert finding_stats.status_code == 200
        assert finding_stats.json()["total_findings"] == 1
        assert finding_stats.json()["findings_by_source"] == {"github": 1}
        assert proposal_stats.status_code == 200
        assert proposal_stats.json()["total_proposals"] == 1
        assert proposal_stats.json()["proposed_count"] == 1

    @pytest.mark.asyncio
    async def test_direct_ids_from_another_tenant_are_not_visible(
        self, client, enforcing
    ):
        seeded = await _seed_two_tenant_research()
        headers = self._headers(enforcing, seeded["org_a"])

        own = await client.get(
            f"/api/v1/research/findings/{seeded['finding_a']}", headers=headers
        )
        foreign_finding = await client.get(
            f"/api/v1/research/findings/{seeded['finding_b']}", headers=headers
        )
        foreign_proposal = await client.get(
            f"/api/v1/research/proposals/{seeded['proposal_b']}", headers=headers
        )

        assert own.status_code == 200
        assert foreign_finding.status_code == 404
        assert foreign_proposal.status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_approve_another_tenants_proposal(self, client, enforcing):
        seeded = await _seed_two_tenant_research()
        headers = self._headers(enforcing, seeded["org_a"])

        response = await client.patch(
            f"/api/v1/research/proposals/{seeded['proposal_b']}/approve",
            headers=headers,
            json={"approved_by": "spoofed-actor"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_reject_another_tenants_proposal(self, client, enforcing):
        seeded = await _seed_two_tenant_research()
        headers = self._headers(enforcing, seeded["org_a"])

        response = await client.patch(
            f"/api/v1/research/proposals/{seeded['proposal_b']}/reject",
            headers=headers,
            json={"rejected_by": "spoofed-actor", "reason": "not mine"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_writes_require_a_workspace_owned_by_the_verified_tenant(
        self, client, enforcing
    ):
        seeded = await _seed_two_tenant_research()
        headers = self._headers(enforcing, seeded["org_a"])
        proposal = {
            "workspace_id": str(seeded["workspace_b"]),
            "title": "Foreign workspace proposal",
            "objective": "This write must be rejected",
            "hypothesis": "Tenant ownership is checked server-side",
        }

        foreign = await client.post(
            "/api/v1/research/proposals", headers=headers, json=proposal
        )
        foreign_scan = await client.post(
            "/api/v1/research/scan",
            headers=headers,
            json={"sources": [], "workspace_id": str(seeded["workspace_b"])},
        )
        foreign_generate = await client.post(
            "/api/v1/research/proposals/generate",
            headers=headers,
            json={"workspace_id": str(seeded["workspace_b"])},
        )
        missing = await client.post(
            "/api/v1/research/scan", headers=headers, json={"sources": []}
        )

        assert foreign.status_code == 403
        assert foreign_scan.status_code == 403
        assert foreign_generate.status_code == 403
        assert missing.status_code == 403


class TestBuildContextHygiene:
    """Staged sibling packages must not become second sources of truth — and must
    actually cover every sibling package `app/` imports at runtime."""

    def test_vendored_auth_package_is_not_committed(self):
        """Staged into the build context at build time; git-ignored always.

        If this directory were committed it would be an editable second copy of
        the policy, and the two would drift silently — which is the failure the
        single-definition design exists to prevent. Covers the whole of `vendor/`,
        so it applies to every package the staging script adds, not only the first.
        """
        import subprocess
        from pathlib import Path

        component = Path(__file__).resolve().parent.parent
        tracked = subprocess.run(
            ["git", "ls-files", "vendor/"],
            cwd=component,
            capture_output=True,
            text=True,
            check=False,
        )
        assert tracked.stdout.strip() == "", (
            "vendor/ is build scratch and must not be tracked; "
            f"tracked files: {tracked.stdout!r}"
        )

    def test_every_sibling_package_app_imports_is_staged_and_guarded(self):
        """Issue #5053 (U7b): the staging list is derived, not remembered.

        `superplane_contracts` was the second instance of one bug: a package `app/`
        imports at module scope, absent from `pyproject.toml`, living outside the
        pinned Docker context, and supplied in the test lane only because CI
        pip-installs it from its path. The image raised ModuleNotFoundError at
        `app/main.py:11`; nothing caught it because nothing compared what `app/`
        imports against what the build stages.

        This is that comparison. It scans the shipped source for top-level imports
        that resolve to source maintained in this repository, and requires each one
        to be staged by the script AND checked by the Dockerfile, so the next
        sibling package fails here rather than in a container.
        """
        import importlib.util
        import re
        import sys
        from pathlib import Path

        component = Path(__file__).resolve().parent.parent
        app_dir = component / "app"

        # WHY THIS SCANS BY ORIGIN AND NOT BY NAME (issue #5535, W6)
        # ----------------------------------------------------------
        # This scanner used to match `superplane_[a-z0-9_]+`, which made it blind to
        # exactly the case it exists to catch. `harness_jobs` (#5527) is an
        # in-repository package `app/` imports at module scope, absent from
        # pyproject.toml, outside the pinned Docker context — the same bug in every
        # respect except the package's name — and the prefix excluded it. The
        # staging entry was missing for months and this test stayed green.
        #
        # The distinguishing property was never the name. It is that the package is
        # maintained in THIS REPOSITORY and therefore is not installed by
        # `pip install .` from pyproject.toml. So the scanner now collects every
        # top-level import and keeps the ones that resolve to a path inside the
        # repository; anything from site-packages is a declared dependency and the
        # image gets it from pyproject.toml.
        #
        # Deliberately not "every import not in pyproject.toml": distribution names
        # and import names differ (`pyjwt` imports `jwt`, `python-jose` imports
        # `jose`), so that comparison needs a hand-maintained mapping and a wrong
        # entry fails open. Resolution needs no mapping.
        imported: set[str] = set()
        pattern = re.compile(
            r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.MULTILINE
        )
        for source in app_dir.rglob("*.py"):
            for match in pattern.finditer(source.read_text()):
                imported.add(match.group(1).split(".")[0])

        repo_root = component.parents[4]

        def _is_in_repository(module: str) -> bool:
            """True when this import resolves to source maintained in this repo."""
            if module in sys.stdlib_module_names:
                return False
            try:
                spec = importlib.util.find_spec(module)
            except (ImportError, ValueError):
                return False
            if spec is None or not spec.origin:
                return False
            try:
                # `resolve()` matters: an editable install can reach the package
                # through a symlink, and an unresolved path would not compare
                # against the repository root.
                return Path(spec.origin).resolve().is_relative_to(repo_root)
            except (OSError, ValueError):
                return False

        in_repository = {
            module
            for module in imported
            if module not in ("app", "tests") and _is_in_repository(module)
        }

        # Sanity check on the scanner itself: if this set is empty the assertions
        # below would pass vacuously, which is the failure mode of every
        # scan-the-source test. Named explicitly rather than only counted, because a
        # scanner that found one package and missed two would satisfy a count.
        assert in_repository, (
            "found no in-repository imports under app/ — the scanner is broken, or "
            "the sibling packages are installed from outside the repository"
        )
        assert "superplane_contracts" in in_repository
        assert "superplane_auth" in in_repository
        assert "harness_jobs" in in_repository, (
            "app/ no longer imports harness_jobs, or it resolves from outside the "
            "repository; #5535 composes the operation facade and inventory "
            "authority from it, so its absence means those ports are uncomposed"
        )
        imported = in_repository

        staging_script = (component / "scripts" / "stage-domain-auth.sh").read_text()
        dockerfile = (component / "Dockerfile").read_text()

        # Parse the script's `packages=(...)` table rather than searching the file
        # for the package name. Searching the whole file passes on a script whose
        # table is empty but whose header comments still discuss the package — which
        # is not a hypothetical: this file's header names `superplane_contracts`
        # three times, so deleting its table entry left an earlier version of this
        # test green. The staging behaviour lives in the table; assert on the table.
        table = re.search(r"^packages=\((.*?)^\)", staging_script, re.MULTILINE | re.S)
        assert table, (
            "could not find the `packages=(...)` table in stage-domain-auth.sh; "
            "this test asserts on that table and cannot verify anything without it"
        )
        staged_packages = set(re.findall(r'"[^":]+:([^":]+):', table.group(1)))
        assert staged_packages, "the staging table parsed as empty"

        for package in sorted(imported):
            distribution = package.replace("_", "-")
            assert package in staged_packages, (
                f"app/ imports {package} but scripts/stage-domain-auth.sh does not "
                f"stage it (table stages: {sorted(staged_packages)}); the image "
                "will fail at import"
            )
            assert f"vendor/{distribution}" in dockerfile, (
                f"app/ imports {package} but the Dockerfile does not COPY "
                f"vendor/{distribution} into the build context"
            )
            # Note on scope, established by mutation: deleting only the COPY line
            # leaves this assertion failing but is ALSO caught by the build itself —
            # the `test -f` guard below the COPYs fails first, with the named cause.
            # Deleting only the guard is the dangerous half, because a missing
            # package then reaches runtime. Hence the next assertion.
            # A COPY without a check produces an image that crash-loops on import
            # instead of a build that fails with a named cause.
            assert f"{distribution}/{package}/" in dockerfile, (
                f"the Dockerfile copies vendor/{distribution} but does not verify "
                f"the {package} package inside it is present"
            )


class TestOrganizationScopedAuthorization:
    """Org-scoped endpoints need org authority, not one workspace grant."""

    @pytest.mark.asyncio
    async def test_single_workspace_grant_is_not_org_authority(self, client, enforcing):
        """The policy refuses org endpoints against a workspace grant, by design.

        A member holding ADMINISTER on one of the org's two workspaces must not
        reach an org-wide collection: that is how a single-workspace member
        reads the whole organization.
        """
        from tests.conftest import async_session_test

        org_id, _ = await _seed_workspace("workspace:administer")
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    name="second-ws",
                    isolation_mode="shared",
                    status="active",
                )
            )
            await session.commit()

        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            "/orgs/current/quota", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_no_grant_at_all_is_refused_for_org_endpoint(self, client, enforcing):
        org_id, _ = await _seed_workspace(None)
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            "/orgs/current/quota", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_grant_across_every_workspace_is_org_authority(
        self, client, enforcing
    ):
        """The conservative definition: authority on every active workspace."""
        org_id, _ = await _seed_workspace("workspace:administer")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            "/orgs/current/quota", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code not in (401, 403)

    @pytest.mark.asyncio
    async def test_a_torn_down_workspace_does_not_revoke_org_authority(
        self, client, enforcing
    ):
        """`DELETE /workspaces/{id}` is a SOFT delete, so the row survives.

        It survives with `status="Teardown"` and never acquires a grant, so
        counting it toward "the caller holds this permission across the
        organization" made org authority monotonically harder to hold: every
        workspace the org ever deleted became a permanent denial for every
        member, on a check with no way to grant past it. The caller here holds
        ADMINISTER on every LIVE workspace and must be admitted.
        """
        from tests.conftest import async_session_test

        org_id, _ = await _seed_workspace("workspace:administer")
        async with async_session_test() as session:
            for dead_status in ("Teardown", "Deleted"):
                session.add(
                    Workspace(
                        id=uuid.uuid4(),
                        org_id=org_id,
                        name=f"gone-{dead_status}",
                        isolation_mode="shared",
                        status=dead_status,
                    )
                )
            await session.commit()

        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            "/orgs/current/quota", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code not in (401, 403)

    @pytest.mark.asyncio
    async def test_a_live_ungranted_workspace_still_refuses_org_authority(
        self, client, enforcing
    ):
        """The complement: excluding torn-down rows must not weaken the check.

        A workspace in a provisioning-lifecycle state is still live, so a caller
        without a grant on it does not hold organization authority.
        """
        from tests.conftest import async_session_test

        org_id, _ = await _seed_workspace("workspace:administer")
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    name="still-live",
                    isolation_mode="shared",
                    status="max_retries_exceeded",
                )
            )
            await session.commit()

        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.get(
            "/orgs/current/quota", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_insufficient_permission_is_refused_for_org_endpoint(
        self, client, enforcing
    ):
        """READ across the org is not ADMINISTER over the org."""
        org_id, _ = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        response = await client.patch(
            "/orgs/current/quota",
            json={"max_nodes": 10},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_malformed_org_claim_is_denied_not_a_server_error(
        self, client, enforcing
    ):
        """A verified-but-unusable org claim is a 403, never a 500."""
        token = _mint(enforcing, **{"custom:org_id": "not-a-uuid"})
        response = await client.get(
            "/orgs/current/quota", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403


class TestAuthorizedWorkspaceFiltering:
    """Collection reads are filtered to the caller's own workspaces."""

    @pytest.mark.asyncio
    async def test_returns_only_workspaces_the_caller_holds(self, enforcing):
        """Without this filter, a listing leaks the existence of other workspaces."""
        from tests.conftest import async_session_test

        org_id, granted = await _seed_workspace("workspace:read")
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    name="unheld-ws",
                    isolation_mode="shared",
                    status="active",
                )
            )
            await session.commit()

        caller = domain_auth.VerifiedCaller(
            principal=type(
                "P",
                (),
                {
                    "subject": "user-abc",
                    "org_id": str(org_id),
                    "client_id": TEST_CLIENT_ID,
                    "account_type": "human",
                },
            )(),
            safe_headers={},
        )
        async with async_session_test() as session:
            visible = await domain_auth.authorized_workspace_ids(
                session, caller, Permission.READ
            )
        assert visible == [granted]

    @pytest.mark.asyncio
    async def test_permission_is_respected_by_the_filter(self, enforcing):
        """A READ grant does not appear in a PROVISION-filtered listing."""
        from tests.conftest import async_session_test

        org_id, _ = await _seed_workspace("workspace:read")
        caller = domain_auth.VerifiedCaller(
            principal=type(
                "P",
                (),
                {
                    "subject": "user-abc",
                    "org_id": str(org_id),
                    "client_id": TEST_CLIENT_ID,
                    "account_type": "human",
                },
            )(),
            safe_headers={},
        )
        async with async_session_test() as session:
            assert (
                await domain_auth.authorized_workspace_ids(
                    session, caller, Permission.PROVISION
                )
                == []
            )


class TestJWKSCache:
    """Key retrieval, including the failure that must not be silent."""

    def test_missing_jwks_url_is_a_startup_class_error(self, monkeypatch):
        """Enforcing without a JWKS URL cannot verify anything, so it raises."""
        monkeypatch.setattr(settings, "cognito_jwks_url", "")
        cache = type(domain_auth.jwks_cache)()
        with pytest.raises(TokenPolicyError, match="JWKS"):
            cache.get("any-kid")

    def test_keys_are_fetched_once_and_reused(self, monkeypatch, rsa_keys):
        """A per-request fetch would put an outage on the authorization path."""
        _, public_jwk = rsa_keys
        calls = []

        cache = type(domain_auth.jwks_cache)()

        def fake_fetch():
            calls.append(1)
            cache.load([public_jwk])

        monkeypatch.setattr(cache, "_fetch", fake_fetch)
        assert cache.get(TEST_KID) is not None
        assert cache.get(TEST_KID) is not None
        assert len(calls) == 1

    def test_clear_forces_a_refetch(self, rsa_keys):
        _, public_jwk = rsa_keys
        cache = type(domain_auth.jwks_cache)()
        cache.load([public_jwk])
        assert cache.loaded
        cache.clear()
        assert not cache.loaded

    def test_keys_without_a_kid_are_ignored(self):
        cache = type(domain_auth.jwks_cache)()
        cache.load([{"kty": "RSA", "n": "x"}])
        assert cache.get("anything") is None


class TestEnforcementDisabledPath:
    """With enforcement off the legacy path decides — and says so."""

    @pytest.mark.asyncio
    async def test_domain_route_without_policy_does_not_use_strict_path(
        self, client, monkeypatch
    ):
        """The guard must not half-enforce when no policy is configured.

        Enforcement off means the legacy validator is authoritative; the guard
        has still classified the route. What must NOT happen is the strict path
        running without a policy, which would refuse every request.
        """
        monkeypatch.setattr(fastapi_app.state, "domain_policy", None, raising=False)
        org_id = uuid.uuid4()
        legacy_token, _ = create_access_token(org_id)
        response = await client.get(
            "/workspaces", headers={"Authorization": f"Bearer {legacy_token}"}
        )
        assert response.status_code not in (401, 403)

    @pytest.mark.asyncio
    async def test_require_verified_caller_refuses_without_a_policy(self):
        """Called directly with no policy configured: refuse, never fall back."""
        from fastapi import HTTPException
        from starlette.datastructures import Headers

        class _Req:
            def __init__(self):
                self.app = type("A", (), {"state": type("S", (), {})()})()
                self.state = type("S", (), {})()
                self.headers = Headers({})

        with pytest.raises(HTTPException) as exc:
            await domain_auth.require_verified_caller(_Req(), None)
        assert exc.value.status_code == 403


class TestUninventoriedRouteFailsClosed:
    """A route nobody classified must be refused, not served."""

    @pytest.mark.parametrize("present_valid_token", [False, True])
    @pytest.mark.asyncio
    async def test_route_absent_from_the_inventory_is_refused(
        self, enforcing, present_valid_token
    ):
        """The property that makes the inventory safe to rely on.

        Registered on a throwaway app carrying the same guard, because the point
        is what happens to a route that was never classified — which cannot be
        demonstrated on the real app without leaving an unclassified route in it
        (and the inventory test would then fail, correctly).
        """
        from fastapi import Depends, FastAPI
        from httpx import ASGITransport, AsyncClient

        from app.database import get_session
        from app.domain_guard import enforce_domain_authorization

        probe = FastAPI(dependencies=[Depends(enforce_domain_authorization)])
        probe.state.domain_policy = fastapi_app.state.domain_policy
        probe.dependency_overrides[get_session] = fastapi_app.dependency_overrides.get(
            get_session
        )

        @probe.get("/never-classified")
        async def _never_classified():
            return {"reached": True}  # pragma: no cover - must be unreachable

        transport = ASGITransport(app=probe)
        headers = (
            {"Authorization": f"Bearer {_mint(enforcing)}"}
            if present_valid_token
            else {}
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/never-classified", headers=headers)

        assert response.status_code == 403
        assert "no recorded authorization decision" in response.text

    @pytest.mark.asyncio
    async def test_unmatched_path_is_404_not_403(self, client, enforcing):
        """A typo must not be reported as an authorization failure."""
        response = await client.get("/no/such/path")
        assert response.status_code == 404


class TestActingPrincipalIsPublishedForTheHarnessPorts:
    """The guard publishes the actor the `operation_facade` port resolves against.

    Issue #5535 (W6). `harness_jobs`'s `PrincipalResolver` treats its `org_id` /
    `workspace_id` arguments as an assertion to check rather than a source of
    authority, so the tenant has to come from a caller this process authenticated.
    Nothing bound one: measured against real PostgreSQL with the real composed
    facade, every operation failed with `ProvisioningRefused: the acting principal
    could not be resolved from the authenticated context` — 100% of requests,
    whatever the grants said.

    These tests pin the request-boundary half of that repair. The resolver's own
    behaviour against real grant rows is `tests/test_operation_authority_postgres.py`;
    what matters here is that a real HTTP request through the real global guard
    leaves a principal the resolver can use, and leaves nothing behind afterwards.
    """

    @pytest.mark.asyncio
    async def test_a_workspace_scoped_request_publishes_the_verified_caller(
        self, client, enforcing
    ):
        """The actor comes from verified claims, and carries the path's workspace.

        Asserted by reading the contextvar from inside a request rather than by
        calling `set_acting_principal` in the test: the thing under test is the
        wiring, and a test that bound the principal itself would pass against a
        guard that binds nothing.
        """
        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})

        captured = _CapturedActor().attach(f"/workspaces/{{{WORKSPACE_PATH_PARAM}}}")
        try:
            response = await client.get(
                f"/workspaces/{workspace_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            captured.restore()

        assert response.status_code == 200
        acting = captured.acting
        assert acting is not None, (
            "no acting principal was published, so every harness port resolves to a "
            "refusal regardless of grants"
        )
        assert acting.subject == "user-abc"
        assert acting.org_id == str(org_id)
        assert acting.workspace_id == str(workspace_id)
        assert acting.account_type == "human"

    @pytest.mark.asyncio
    async def test_an_organization_scoped_request_publishes_no_workspace(
        self, client, enforcing
    ):
        """The zero-workspace case must not invent a workspace id.

        `POST /workspaces` has no workspace in its path because it is creating one.
        The published workspace is empty and the handler asserts the id it is about
        to create; a fabricated one here would be an identifier nothing issued.
        """
        org_id = uuid.uuid4()
        await _seed_organization_grant(org_id, "user-abc", ORGANIZATION_ADMINISTER)
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})

        captured = _CapturedActor().attach("/workspaces", method="POST")
        try:
            await client.post(
                "/workspaces",
                json={"name": "w", "isolation_mode": "dedicated"},
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            captured.restore()

        # The response status is deliberately NOT asserted. Provisioning legitimately
        # refuses here — there is no approval record, which is the honest state this
        # story preserves rather than papers over — and the property under test is
        # what the guard published before the handler ran, which is established
        # whether admission then succeeds or refuses.
        acting = captured.acting
        assert acting is not None
        assert acting.org_id == str(org_id)
        assert acting.workspace_id == "", (
            "an organization-scoped request published a workspace id that no "
            "workspace grant or path parameter established"
        )

    @pytest.mark.asyncio
    async def test_the_principal_is_unbound_when_the_guard_closes(self, enforcing):
        """The `finally` actually runs: bound while yielding, gone after closing.

        Driven directly through `_GuardRun` rather than through the HTTP client. The
        reason is measured and is recorded in full on that helper: the real app's
        `BaseHTTPMiddleware` layers copy the context, so a stranded principal is
        invisible to the test function AND to every later request. Two earlier
        versions of this test — one reading the test's own context, one reading a
        subsequent request — therefore both passed with the `finally` deleted. Both
        were vacuous against the single mutation this test names.

        Asserting `inside is not None` first is what stops the post-condition from
        being trivially true of a guard that binds nothing at all.
        """
        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})

        run = await _GuardRun(
            token,
            f"/workspaces/{workspace_id}",
            f"/workspaces/{{{WORKSPACE_PATH_PARAM}}}",
        ).run()

        assert run.inside is not None, "nothing was bound, so nothing can be unbound"
        assert run.inside.workspace_id == str(workspace_id)
        assert run.after is None, (
            "the acting principal outlived the guard — a later in-process caller of "
            "the harness ports would act as this request's tenant"
        )

    @pytest.mark.asyncio
    async def test_the_principal_is_reset_even_when_the_request_fails(self, enforcing):
        """A failure while the principal is bound must not strand it.

        The `finally` and not the happy path: an exception is exactly when cleanup is
        skipped by code that resets after the `yield` instead of around it. The
        failure is raised from inside the bound window, which is the window the
        `finally` covers.
        """
        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})

        async def fail():
            raise RuntimeError("request failed while the principal was bound")

        run = _GuardRun(
            token,
            f"/workspaces/{workspace_id}",
            f"/workspaces/{{{WORKSPACE_PATH_PARAM}}}",
        )
        with pytest.raises(RuntimeError):
            await run.run(inside=fail)

        assert run.inside is not None, (
            "the principal was never bound, so this test would pass even against a "
            "guard that publishes nothing"
        )
        assert run.after is None, "an actor survived a failed request"

    @pytest.mark.asyncio
    async def test_a_refused_request_publishes_nothing(self, client, enforcing):
        """A caller the guard refuses must never become an acting principal.

        The binding happens after the grant check for this reason: publishing before
        it would give the harness ports a view of "who is acting" for callers that
        were about to be denied.

        Driven twice, because the two halves are only observable in different places.
        Through the client, the probe attached to the route establishes that the
        refused request never reached the handler's dependencies. Through `_GuardRun`,
        the refusal is observed to leave nothing bound in the guard's own context —
        which is where a principal published before the check would be visible, and
        where the HTTP path cannot see.
        """
        from fastapi import HTTPException

        org_id, workspace_id = await _seed_workspace(None)
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        path = f"/workspaces/{workspace_id}"
        template = f"/workspaces/{{{WORKSPACE_PATH_PARAM}}}"

        probe = _CapturedActor().attach(template)
        try:
            response = await client.get(
                path, headers={"Authorization": f"Bearer {token}"}
            )
        finally:
            probe.restore()

        assert response.status_code == 403
        assert probe.acting is None

        run = _GuardRun(token, path, template)
        with pytest.raises(HTTPException) as refusal:
            await run.run()

        assert refusal.value.status_code == 403
        assert run.inside is None, (
            "a caller the guard refused was published as the acting principal"
        )
        assert run.after is None, (
            "a refused caller's identity was published and outlived the refusal"
        )

    @pytest.mark.asyncio
    async def test_a_public_route_still_answers(self, client):
        """The guard became a generator dependency; public routes must survive it.

        MEASURED, and the reason this test exists: FastAPI drives a generator
        dependency as an async context manager, so an early `return` before the
        `yield` raises `RuntimeError: generator didn't yield` and the request
        becomes a 500. Public and internal routes are exactly the early exits, so
        the first draft of this change would have broken every one of them while
        the authorization tests stayed green.
        """
        response = await client.get("/health")

        assert response.status_code == 200


class TestRecordedActorComesFromTheCredential:
    """The approval audit trail is never authored by the request body.

    This class replaces TestLegacyActorFallback, which asserted the opposite:
    that with enforcement off, an `approved_by` field in the request body was
    the recorded actor. That was the weakness A02 (issue #5682) exists to
    close — an anonymous caller could stamp any name onto an approval. The
    body field is no longer read at all, so the property worth pinning now is
    that every branch of _recorded_actor derives from a server-verified
    identity. U21 still owns retiring the legacy HS256 path itself.
    """

    def test_verified_caller_subject_is_preferred(self):
        from types import SimpleNamespace

        from app.routers.research import _recorded_actor

        caller = SimpleNamespace(principal=SimpleNamespace(subject="cognito-sub-1"))
        request = SimpleNamespace(state=SimpleNamespace(caller=caller))
        actor = _recorded_actor(request, {"org_id": uuid.uuid4(), "user_id": None})
        assert actor == "cognito-sub-1"

    def test_legacy_token_user_id_is_used_when_there_is_no_domain_caller(self):
        """The HS256 path has no subject, but its user_id is still server-derived."""
        from types import SimpleNamespace

        from app.routers.research import _recorded_actor

        user_id = uuid.uuid4()
        request = SimpleNamespace(state=SimpleNamespace())
        actor = _recorded_actor(request, {"org_id": uuid.uuid4(), "user_id": user_id})
        assert actor == str(user_id)

    def test_org_scoped_token_records_the_org_not_an_unverified_name(self):
        """Worst case is an honest org attribution, never a caller-chosen string."""
        from types import SimpleNamespace

        from app.routers.research import _recorded_actor

        org_id = uuid.uuid4()
        request = SimpleNamespace(state=SimpleNamespace())
        actor = _recorded_actor(request, {"org_id": org_id, "user_id": None})
        assert actor == f"org:{org_id}"

    def test_a_body_supplied_actor_cannot_be_recorded(self):
        """The regression guard: approve/reject must ignore body-supplied names."""
        import inspect

        from app.routers import research

        source = inspect.getsource(research)
        assert "_recorded_actor(http_request, request.approved_by" not in source
        assert "_recorded_actor(http_request, request.rejected_by" not in source

        signature = inspect.signature(research._recorded_actor)
        assert list(signature.parameters) == ["http_request", "user_context"]


class TestEnvironmentStateIsObservable:
    """R5 acc. 5: the environment's identity state is asserted, not inferred."""

    @pytest.mark.asyncio
    async def test_health_reports_identity_posture(self, client):
        """The flags are readable without a credential, so they can be asserted.

        A deployment check (or a reviewer) can observe what the running process
        actually has, instead of reading this repository's defaults and assuming
        they describe the target environment.
        """
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert "cognito_enabled" in body
        assert "domain_auth_enforced" in body
        assert isinstance(body["cognito_enabled"], bool)

    @pytest.mark.parametrize("cognito_enabled", [False, True])
    @pytest.mark.asyncio
    async def test_health_reports_configured_cognito_state_with_strict_policy(
        self, client, enforcing, monkeypatch, cognito_enabled
    ):
        monkeypatch.setattr(settings, "cognito_enabled", cognito_enabled)
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["cognito_enabled"] is cognito_enabled
        assert response.json()["domain_auth_enforced"] is True

        org_id, workspace_id = await _seed_workspace("workspace:read")
        token = _mint(enforcing, **{"custom:org_id": str(org_id)})
        permitted = await client.get(
            f"/workspaces/{workspace_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        refused = await client.get(
            f"/workspaces/{workspace_id}",
            headers={"Authorization": "Bearer not-a-signed-token"},
        )

        assert permitted.status_code == 200
        assert refused.status_code == 401

    @pytest.mark.asyncio
    async def test_health_reflects_the_loaded_policy_not_the_setting(
        self, client, enforcing
    ):
        """Enforcement is reported from the policy object that decides requests."""
        response = await client.get("/health")
        assert response.json()["domain_auth_enforced"] is True

    @pytest.mark.asyncio
    async def test_enforced_setting_without_a_policy_reports_false(
        self, client, monkeypatch
    ):
        """A truthy setting whose policy failed to build must not read as enforcing.

        This is the case that would otherwise be most dangerous to misreport: an
        operator sets the flag, the policy fails to build, and a status endpoint
        claiming "enforced" would hide that every domain route is running on the
        legacy path.
        """
        monkeypatch.setattr(settings, "domain_auth_enforced", True)
        monkeypatch.setattr(fastapi_app.state, "domain_policy", None, raising=False)
        response = await client.get("/health")
        assert response.json()["domain_auth_enforced"] is False

    @pytest.mark.asyncio
    async def test_health_leaks_no_identity_configuration(self, client, enforcing):
        """Posture booleans only — no issuer, client ids, JWKS URL or keys."""
        body = (await client.get("/health")).text
        assert TEST_ISSUER not in body
        assert TEST_CLIENT_ID not in body
        assert "jwks" not in body.lower()


class TestEveryDomainRouteRefusesUnauthorizedCallers:
    """The acceptance matrix, applied to every domain route rather than a sample.

    The issue's criterion is that EVERY unauthorized case returns 401/403. A
    hand-picked handful of routes cannot establish that, and the routes most
    likely to be missed are the ones nobody thought to sample. So this walks the
    inventory itself: a newly added domain route is covered the moment it is
    inventoried, without anyone remembering to extend this test.
    """

    @staticmethod
    def _concrete(template: str) -> str:
        path = template
        for placeholder in (
            "{workspace_id}",
            "{connection_id}",
            "{dep_id}",
            "{event_id}",
            "{user_id}",
            "{account_id}",
            "{credential_id}",
            "{finding_id}",
            "{proposal_id}",
            "{cluster_id}",
        ):
            path = path.replace(placeholder, str(uuid.uuid4()))
        return path

    @pytest.mark.asyncio
    async def test_no_credential_is_always_401_or_403(self, client, enforcing):
        offenders = []
        for method, template in sorted(set(DOMAIN_ROUTES) | set(PRIVATE_DOMAIN_ROUTES)):
            body = {} if method in {"POST", "PATCH", "PUT"} else None
            response = await client.request(method, self._concrete(template), json=body)
            if response.status_code not in (401, 403):
                offenders.append((method, template, response.status_code))
        assert not offenders, (
            "these domain routes answered an unauthenticated request with "
            f"something other than 401/403: {offenders}"
        )

    @pytest.mark.asyncio
    async def test_forged_credential_is_always_401_or_403(self, client, enforcing):
        """A 422 here would report an auth failure as a validation error."""
        offenders = []
        headers = {"Authorization": "Bearer forged.token.here"}
        for method, template in sorted(set(DOMAIN_ROUTES) | set(PRIVATE_DOMAIN_ROUTES)):
            body = {} if method in {"POST", "PATCH", "PUT"} else None
            response = await client.request(
                method, self._concrete(template), headers=headers, json=body
            )
            if response.status_code not in (401, 403):
                offenders.append((method, template, response.status_code))
        assert not offenders, (
            f"these domain routes accepted or mis-reported a forged token: {offenders}"
        )

    @pytest.mark.asyncio
    async def test_internal_routes_never_accept_a_domain_token(self, client, enforcing):
        """A user token must not reach a machine-to-machine route.

        Machine credentials are separate from domain user tokens on purpose.
        Internal routes use either the shared platform token or the observation
        receiver's workspace-scoped credential, neither of which is a domain
        user identity. Admitting a user token would grant machine authority to a
        user.
        """
        from app.endpoint_inventory import INTERNAL_ROUTES

        offenders = []
        headers = {"Authorization": f"Bearer {_mint(enforcing)}"}
        for method, template in sorted(INTERNAL_ROUTES):
            body = {} if method in {"POST", "PATCH", "PUT"} else None
            response = await client.request(
                method, self._concrete(template), headers=headers, json=body
            )
            if response.status_code not in (401, 403):
                offenders.append((method, template, response.status_code))
        assert not offenders, (
            f"internal routes admitted a domain user token: {offenders}"
        )


def test_private_credential_evidence_retains_domain_workspace_authority():
    from app.endpoint_inventory import INTERNAL_ROUTES, PUBLIC_ROUTES, RouteClass, Scope

    key = (
        "GET",
        "/internal/installation/workspaces/{workspace_id}/credential-evidence/{connection_id}",
    )
    assert key in PRIVATE_DOMAIN_ROUTES
    assert key not in DOMAIN_ROUTES
    assert key not in INTERNAL_ROUTES | PUBLIC_ROUTES
    assert classify(*key) == (
        RouteClass.DOMAIN,
        (Scope.WORKSPACE, Permission.RENEW_CREDENTIAL),
    )
    assert key in all_inventoried()


@pytest.fixture
def jwks_http_fixture(rsa_keys, monkeypatch):
    """Serve real signing keys only from ephemeral loopback HTTP endpoints."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    _, public_jwk = rsa_keys
    state = {
        "status": 200,
        "origin": "cross-origin",
        "destination_requests": 0,
        "source_requests": 0,
        "body": json.dumps({"keys": [public_jwk]}).encode(),
    }

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["destination_requests"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(state["body"])

    target = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/other-keys":
                state["destination_requests"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(state["body"])
                return
            state["source_requests"] += 1
            self.send_response(state["status"])
            if state["status"] in [301, 302, 303, 307, 308]:
                destination = (
                    "/other-keys"
                    if state["origin"] == "same-origin"
                    else f"http://127.0.0.1:{target.server_port}/other-keys"
                )
                self.send_header("Location", destination)
            self.end_headers()
            self.wfile.write(state["body"])

    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    threads = [
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        for server in (source, target)
    ]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{source.server_port}/jwks", state
    finally:
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


class TestJWKSHTTPAuthority:
    @pytest.mark.parametrize("origin", ["same-origin", "cross-origin"])
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_redirected_signing_keys_are_never_requested_or_accepted(
        self, monkeypatch, rsa_keys, jwks_http_fixture, origin, status
    ):
        endpoint, state = jwks_http_fixture
        state.update(status=status, origin=origin)
        cache = type(domain_auth.jwks_cache)()
        monkeypatch.setattr(domain_auth, "jwks_cache", cache)
        monkeypatch.setattr(settings, "cognito_jwks_url", endpoint)
        token = _mint(rsa_keys[0])
        with pytest.raises(TokenRejectedError, match="signing key is unavailable"):
            domain_auth.verify_access_token(token)
        assert state["destination_requests"] == 0
        assert state["source_requests"] == 1
        assert not cache.loaded
        # Failure is not cached: a restored direct configured endpoint recovers.
        state["status"] = 200
        assert domain_auth.verify_access_token(token)["sub"] == "user-abc"
        assert cache.loaded
        assert state["source_requests"] == 2
        assert state["destination_requests"] == 0

    def test_direct_endpoint_verifies_real_signature_and_caches_keys(
        self, monkeypatch, rsa_keys, jwks_http_fixture
    ):
        endpoint, state = jwks_http_fixture
        cache = type(domain_auth.jwks_cache)()
        monkeypatch.setattr(domain_auth, "jwks_cache", cache)
        monkeypatch.setattr(settings, "cognito_jwks_url", endpoint)
        token = _mint(rsa_keys[0])
        assert domain_auth.verify_access_token(token)["sub"] == "user-abc"
        assert domain_auth.verify_access_token(token)["sub"] == "user-abc"
        assert state["source_requests"] == 1
        # A different RSA signer cannot use this endpoint's published key.
        other_pem, _ = _rsa_keypair()
        with pytest.raises(TokenRejectedError):
            domain_auth.verify_access_token(_mint(other_pem))
        assert state["destination_requests"] == 0

    @pytest.mark.parametrize(
        "status,body",
        [(503, b"{}"), (200, b"not-json"), (200, b'{"keys": null}'), (200, b"[]")],
    )
    def test_unavailable_or_malformed_endpoint_refuses_and_can_recover(
        self, monkeypatch, rsa_keys, jwks_http_fixture, status, body
    ):
        endpoint, state = jwks_http_fixture
        valid_body = state["body"]
        state.update(status=status, body=body)
        cache = type(domain_auth.jwks_cache)()
        monkeypatch.setattr(domain_auth, "jwks_cache", cache)
        monkeypatch.setattr(settings, "cognito_jwks_url", endpoint)
        token = _mint(rsa_keys[0])
        with pytest.raises(TokenRejectedError, match="signing key is unavailable"):
            domain_auth.verify_access_token(token)
        assert not cache.loaded
        state.update(status=200, body=valid_body)
        assert domain_auth.verify_access_token(token)["sub"] == "user-abc"
        assert state["destination_requests"] == 0


class TestJWKSEndpointConfiguration:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "file:///tmp/synthetic-signing-keys.json",
            "ftp://example.invalid/jwks",
            "data:application/json,{}",
            "gopher://example.invalid/jwks",
            "/tmp/keys.json",
            "//example.invalid/jwks",
            "https:///jwks",
            "https://user:password@example.invalid/jwks",
            "https://@example.invalid/jwks",
            "https://example.invalid/jwks#keys",
            "https://example.invalid:invalid/jwks",
            "https://example.invalid:99999/jwks",
            "https://example.invalid/\x7fjwks",
            "https://example.invalid/\\jwks",
            "https://example.invalid/\njwks",
        ],
    )
    def test_forbidden_or_malformed_endpoint_never_invokes_a_provider(
        self, monkeypatch, endpoint
    ):
        import urllib.request

        calls = []

        def forbidden_opener(*args, **kwargs):
            calls.append(args)
            raise AssertionError("provider called before configuration validation")

        monkeypatch.setattr(urllib.request, "build_opener", forbidden_opener)
        monkeypatch.setattr(urllib.request, "urlopen", forbidden_opener)
        monkeypatch.setattr(settings, "cognito_jwks_url", endpoint)
        cache = type(domain_auth.jwks_cache)()
        with pytest.raises(TokenPolicyError, match="JWKS URL"):
            cache.get(TEST_KID)
        assert not calls
        assert not cache.loaded
