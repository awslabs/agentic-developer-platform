"""The JWT library's packaging and its security-relevant decode behaviour.

Issue #5601 (S02). Two kinds of test, both about the same change.

WHAT THE STORY WAS. A dependency scan reported ecdsa 0.19.2 in this image with
GHSA-wj6h-64fc-37mp — a timing attack on P-256 signature verification — and no
fixed version published. `ecdsa` is not a direct dependency and nothing here asks
for it: it arrived through `python-jose`, which declares it as an UNCONDITIONAL
requirement (`Requires-Dist: ecdsa!=0.15`, outside every extra). Selecting the
`[cryptography]` extra, as this component already did, adds the modern backend but
does NOT drop `ecdsa`. With no upstream fix to take, removal was the only remedy,
so the component moved to `PyJWT[crypto]` — which installs `cryptography` and
nothing else transitively.

WHY BOTH HALVES ARE TESTED HERE:

1. :class:`TestNoVulnerableTransitiveCryptoDependency` guards the PACKAGING. The
   advisory is unfixable, so a future well-intentioned edit reintroducing a
   jose-shaped requirement would silently bring ecdsa back and the finding would
   reopen. That edit should fail a test, not ship.

2. :class:`TestTokenVerificationSurvivedTheLibrarySwap` guards the BEHAVIOUR that
   the swap could have quietly changed. Swapping the library that checks
   signatures is a change to authentication, so the properties that must not
   loosen are asserted through the application verifier rather than assumed from API
   similarity. The `require_exp` case below is the concrete reason this is not
   paranoia: jose's spelling of "this claim is mandatory" is silently ignored by
   PyJWT, and the failure mode was accepting a token that never expires.

These complement `tests/test_auth.py` with dependency-removal checks and the
missing-expiry regression introduced by changing JWT libraries.
"""

from __future__ import annotations

import base64
import json
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from superplane_auth.policy import TokenRejectedError

COMPONENT_DIR = Path(__file__).resolve().parent.parent

# Distributions that must not appear in this component's dependency tree, with the
# reason each is named. `ecdsa` is the advisory itself; the others are the packages
# that only ever arrived as python-jose's siblings, so their presence is a reliable
# signal that a jose-shaped dependency has crept back in even if `ecdsa` is somehow
# pinned away separately.
FORBIDDEN_DISTRIBUTIONS = {
    "ecdsa": "GHSA-wj6h-64fc-37mp (P-256 timing attack), no fixed version published",
    "python-jose": "requires ecdsa unconditionally; replaced by PyJWT[crypto]",
    "rsa": "python-jose transitive dependency; PyJWT[crypto] does not need it",
}


def _declared_dependencies() -> list[str]:
    """This component's declared runtime requirements, verbatim."""
    with (COMPONENT_DIR / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["dependencies"]


class TestNoVulnerableTransitiveCryptoDependency:
    """The unfixable advisory must stay out of the tree."""

    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_DISTRIBUTIONS))
    def test_not_declared_as_a_direct_dependency(self, forbidden: str) -> None:
        """No forbidden distribution is named in `pyproject.toml`.

        Checks the declaration rather than the installed environment, because the
        declaration is what the image builds from: `pip install .` in the Dockerfile
        resolves exactly this list, so a name reappearing here is the thing that
        would put ecdsa back into a shipped image.
        """
        # Requirement strings carry extras, specifiers and markers
        # ("PyJWT[crypto]>=2.13.0,<3"), so compare on the distribution name only.
        declared = {
            requirement.split("[")[0]
            .split(">")[0]
            .split("<")[0]
            .split("=")[0]
            .split("!")[0]
            .split(";")[0]
            .strip()
            .lower()
            for requirement in _declared_dependencies()
        }
        assert forbidden not in declared, (
            f"{forbidden} must not be a dependency of this component: "
            f"{FORBIDDEN_DISTRIBUTIONS[forbidden]}. See issue #5601 (S02)."
        )

    def test_the_jwt_library_is_pyjwt(self) -> None:
        """Positive half: the replacement is actually declared, with its extra.

        Asserted alongside the negative checks because "no jose" and "a working JWT
        library" are different facts, and a dependency edit that dropped the JWT
        requirement altogether would satisfy every test above while breaking every
        authenticated request.

        The `[crypto]` extra is required, not cosmetic: without it PyJWT cannot do
        RS256 at all, so the domain identity path would fail to verify Cognito
        tokens while the HS256 organization path kept working — a partial outage
        that a bare `PyJWT` requirement would not reveal until runtime.
        """
        declared = _declared_dependencies()
        pyjwt = [r for r in declared if r.split("[")[0].strip().lower() == "pyjwt"]
        assert len(pyjwt) == 1, f"expected exactly one PyJWT requirement, got {pyjwt}"
        assert "[crypto]" in pyjwt[0], (
            f"PyJWT needs the `crypto` extra for RS256 verification; got {pyjwt[0]!r}"
        )

    def test_the_installed_environment_has_no_forbidden_distribution(self) -> None:
        """And the resolved environment agrees with the declaration.

        The declaration check above cannot see a TRANSITIVE reintroduction — some
        other requirement growing a dependency on ecdsa — which is precisely how
        ecdsa got here in the first place. This inspects what is actually installed,
        so the test lane's own resolved tree is evidence for the finding's closure.
        """
        from importlib.metadata import distributions

        installed = {
            (dist.metadata["Name"] or "").lower()
            for dist in distributions()
            if dist.metadata["Name"]
        }
        present = sorted(installed & set(FORBIDDEN_DISTRIBUTIONS))
        assert not present, (
            f"forbidden distribution(s) present in the environment: {present}. "
            + "; ".join(f"{name}: {FORBIDDEN_DISTRIBUTIONS[name]}" for name in present)
        )

    def test_no_module_imports_jose(self) -> None:
        """No source file still imports the removed library.

        An import surviving the dependency removal is a `ModuleNotFoundError` at
        startup, not a dependency problem — the class of failure where the test lane
        and the image disagree, which is exactly what this component's build-context
        guards exist for.
        """
        offenders = [
            str(path.relative_to(COMPONENT_DIR))
            for path in list((COMPONENT_DIR / "app").rglob("*.py"))
            + list((COMPONENT_DIR / "tests").rglob("*.py"))
            if any(
                line.startswith(("from jose", "import jose"))
                for line in path.read_text().splitlines()
            )
        ]
        assert not offenders, (
            f"these files still import the removed `jose` library: {offenders}"
        )


# ---------------------------------------------------------------------------
# Behaviour that the library swap must not have loosened
# ---------------------------------------------------------------------------

TEST_KID = "dependency-test-key"


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, dict]:
    """A throwaway RSA keypair and the public JWK a user pool would publish.

    Generated per test session and never written to disk: there is no key material
    in this repository, and none of these values is a credential for anything that
    exists.
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": TEST_KID, "alg": "RS256", "use": "sig"})
    return pem, public_jwk


def _verify_domain_token(token: str, public_jwk: dict) -> dict:
    """Exercise the application verifier with an isolated published test key."""
    from app import auth

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            auth.jwks_cache,
            "get",
            lambda kid: public_jwk if kid == public_jwk["kid"] else None,
        )
        return auth.verify_access_token(token)


def _valid_claims(**overrides) -> dict:
    now = datetime.now(UTC)
    claims = {
        "sub": "user-abc",
        "token_use": "access",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
        "client_id": "test-allowlisted-client",
        "custom:org_id": str(uuid.uuid4()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "iat": int(now.timestamp()),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


class TestTokenVerificationSurvivedTheLibrarySwap:
    """Positive and negative checks through the replacement application verifier."""

    def test_a_validly_signed_token_verifies(self, rsa_keypair) -> None:
        """The positive case. Without it, every negative test below is vacuous —
        a verifier that refused everything would pass all of them."""
        private_pem, public_jwk = rsa_keypair
        claims = _valid_claims()
        token = jwt.encode(
            claims, private_pem, algorithm="RS256", headers={"kid": TEST_KID}
        )
        verified = _verify_domain_token(token, public_jwk)
        assert verified["sub"] == "user-abc"
        assert verified["custom:org_id"] == claims["custom:org_id"]

    def test_token_without_exp_is_rejected(self, rsa_keypair) -> None:
        """THE REGRESSION THIS FILE EXISTS FOR.

        python-jose spelled "expiry is mandatory" as `require_exp: True`. PyJWT
        spells it `require: ["exp"]` and has no `require_exp` option — and, crucially,
        does NOT error on an unrecognised option name: it is silently ignored.

        So a mechanical port that carried `require_exp` across would have looked
        correct, passed every existing test (they all mint tokens WITH an `exp`),
        and accepted a validly signed token carrying no expiry at all as a token
        that never expires. A leaked token would then be valid forever.

        This asserts the option actually in the implementation does the job.
        """
        private_pem, public_jwk = rsa_keypair
        no_exp = jwt.encode(
            _valid_claims(exp=None),
            private_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )
        with pytest.raises(TokenRejectedError):
            _verify_domain_token(no_exp, public_jwk)

    def test_expired_token_is_rejected(self, rsa_keypair) -> None:
        private_pem, public_jwk = rsa_keypair
        expired = jwt.encode(
            _valid_claims(
                exp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
            ),
            private_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )
        with pytest.raises(TokenRejectedError):
            _verify_domain_token(expired, public_jwk)

    def test_token_signed_by_another_key_is_rejected(self, rsa_keypair) -> None:
        """A well-formed token from the wrong signer does not verify."""
        _, public_jwk = rsa_keypair
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        forged = jwt.encode(
            _valid_claims(sub="attacker"),
            other_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )
        with pytest.raises(TokenRejectedError):
            _verify_domain_token(forged, public_jwk)

    def test_alg_none_is_rejected(self, rsa_keypair) -> None:
        """The classic bypass: an unsigned token claiming it needs no signature.

        Hand-assembled, because PyJWT refuses to *produce* an `alg: none` token
        while an attacker is under no such constraint. Minting it would probe the
        signing library instead of the verifier.
        """
        _, public_jwk = rsa_keypair

        def b64(payload: dict) -> str:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        forged = (
            f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': TEST_KID})}."
            f"{b64(_valid_claims(sub='attacker'))}."
        )
        with pytest.raises(TokenRejectedError):
            _verify_domain_token(forged, public_jwk)

    def test_hs256_token_is_rejected_by_the_rs256_verifier(self, rsa_keypair) -> None:
        """Algorithm confusion, and cross-path token confusion, in one case.

        The attack this blocks: present an HS256 token signed with the user pool's
        PUBLIC key (which is published, so anyone has it) to a verifier that honours
        the header's `alg`. It would treat that public value as a shared secret and
        verify happily. Pinning `algorithms=["RS256"]` server-side is the defence.

        It is also what keeps the two token paths separate: this service's own
        organization token is HS256, so this asserts it cannot be replayed into the
        domain identity path to claim a verified Cognito identity.
        """
        _, public_jwk = rsa_keypair
        forged = jwt.encode(
            _valid_claims(sub="attacker"),
            "the-public-key-as-a-shared-secret",
            algorithm="HS256",
            headers={"kid": TEST_KID},
        )
        with pytest.raises(TokenRejectedError):
            _verify_domain_token(forged, public_jwk)

    def test_rs256_token_is_rejected_by_the_legacy_hs256_verifier(
        self, rsa_keypair
    ) -> None:
        """The same separation, checked from the other direction.

        A domain RS256 token presented to the organization-token decoder must not
        verify. Asserted through the application's own `decode_token`, since the
        contract that matters is the 401 it returns, not the library's exception.
        """
        from fastapi import HTTPException

        from app.config import settings
        from app.middleware.auth import decode_token

        private_pem, _ = rsa_keypair
        domain_token = jwt.encode(
            _valid_claims(), private_pem, algorithm="RS256", headers={"kid": TEST_KID}
        )
        original = settings.jwt_secret_key
        settings.jwt_secret_key = "a-test-only-signing-key-not-used-anywhere"
        try:
            with pytest.raises(HTTPException) as refused:
                decode_token(domain_token)
        finally:
            settings.jwt_secret_key = original
        assert refused.value.status_code == 401

    def test_tampered_payload_is_rejected(self, rsa_keypair) -> None:
        """Claims cannot be edited after signing.

        Re-encodes a modified payload onto the original header and signature, which
        is what an attacker who wants a different `org_id` would try. A verifier that
        checked structure but not the signature over the payload would admit it.
        """
        private_pem, public_jwk = rsa_keypair
        token = jwt.encode(
            _valid_claims(), private_pem, algorithm="RS256", headers={"kid": TEST_KID}
        )
        header_b64, _, signature_b64 = token.split(".")
        swapped = base64.urlsafe_b64encode(
            json.dumps(
                _valid_claims(**{"custom:org_id": str(uuid.uuid4())}),
                separators=(",", ":"),
            ).encode()
        ).rstrip(b"=")
        tampered = f"{header_b64}.{swapped.decode()}.{signature_b64}"
        with pytest.raises(TokenRejectedError):
            _verify_domain_token(tampered, public_jwk)

    def test_garbage_is_a_denial_not_a_crash(self, rsa_keypair) -> None:
        """Malformed input stays in the application's authentication-denial path."""
        _, public_jwk = rsa_keypair
        for garbage in ("", "not-a-jwt", "a.b.c", "....", "a.b"):
            with pytest.raises(TokenRejectedError):
                _verify_domain_token(garbage, public_jwk)


class TestLegacyOrganizationTokenRoundTrip:
    """The HS256 organization-token path still mints and reads its own tokens."""

    def test_round_trip_preserves_the_org_claim(self) -> None:
        """Positive path: what `POST /auth/login` issues, `decode_token` reads back.

        Covers the claim contract, not just the signature: `create_access_token`
        writes `org_id`, `user_id` and `role`, and the response model parses them
        into typed values. A signature-only test would miss a claim name changing.
        """
        from app.config import settings
        from app.middleware.auth import create_access_token, decode_token

        org_id, user_id = uuid.uuid4(), uuid.uuid4()
        original = settings.jwt_secret_key
        settings.jwt_secret_key = "a-test-only-signing-key-not-used-anywhere"
        try:
            token, expires_in = create_access_token(
                org_id, user_id=user_id, role="org-admin"
            )
            payload = decode_token(token)
        finally:
            settings.jwt_secret_key = original

        assert isinstance(token, str), "PyJWT must return str, not bytes"
        assert expires_in == settings.jwt_expire_minutes * 60
        assert payload.org_id == org_id
        assert payload.user_id == user_id
        assert payload.role == "org-admin"

    def test_a_token_signed_with_another_secret_is_refused(self) -> None:
        """Negative path: the shared secret is what grants organization scope."""
        from fastapi import HTTPException

        from app.config import settings
        from app.middleware.auth import create_access_token, decode_token

        original = settings.jwt_secret_key
        settings.jwt_secret_key = "a-test-only-signing-key-not-used-anywhere"
        try:
            token, _ = create_access_token(uuid.uuid4())
            settings.jwt_secret_key = "a-different-test-only-signing-key"
            with pytest.raises(HTTPException) as refused:
                decode_token(token)
        finally:
            settings.jwt_secret_key = original
        assert refused.value.status_code == 401
