"""Which validation paths may admit a domain-API call — R5 (issue #5044).

ADP has two token-validation paths and they are not equally strict. If the domain
API accepted "authenticated by some ADP path", the weaker one would satisfy the
stricter one's policy and the client allowlist would be decorative. That is why
the story requires the in-scope paths to be *named* rather than left implicit.

The asymmetry, verified against the two files in this repository:

===============================  ========  =======  ==========  ===============
path                             signature issuer   token_use   client allowlist
===============================  ========  =======  ==========  ===============
src/auth/cognito_jwt.py          yes       yes      yes         yes (optional)
lambda/api-authorizer/handler.py yes       yes       **no**      **no**
===============================  ========  =======  ==========  ===============

So a Cognito **ID** token from any app client in the pool satisfies the
authorizer. That is acceptable for the routes it guards, and it is a bypass of
this policy — hence a named boundary rather than a reused component.

These tests also read the authorizer source, so the table above cannot quietly
become wrong: if someone adds a ``token_use`` check there, the test that asserts
its absence fails and this boundary gets revisited deliberately.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from superplane_auth.policy import (
    IN_SCOPE_VALIDATION_PATHS,
    REJECTED_VALIDATION_PATHS,
    TRUSTED_VALIDATION_PATH,
    DomainTokenPolicy,
    TokenRejectedError,
)

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
ALLOWED_CLIENT = "1example23client45id"

_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_AUTHORIZER = _GATEWAY_ROOT / "lambda" / "api-authorizer" / "handler.py"
_COGNITO_JWT = _GATEWAY_ROOT / "src" / "auth" / "cognito_jwt.py"


@pytest.fixture
def policy() -> DomainTokenPolicy:
    return DomainTokenPolicy(allowed_client_ids=[ALLOWED_CLIENT], expected_issuer=ISSUER)


def valid_access_claims() -> dict[str, object]:
    """Claims that the trusted path admits, so path is the ONLY variable below."""
    return {
        "sub": "8f14e45f-ceea-467a-9f6a-1d0e1c2b3a4d",
        "iss": ISSUER,
        "client_id": ALLOWED_CLIENT,
        "token_use": "access",
        "custom:org_id": "org-acme",
        "custom:account_type": "human",
    }


# --- the in-scope set is enumerated -----------------------------------------


def test_in_scope_validation_paths_are_enumerated():
    """Both paths are named. Neither is left implicit."""
    assert IN_SCOPE_VALIDATION_PATHS == {"gateway.cognito_jwt", "gateway.api_authorizer"}


def test_exactly_one_path_may_admit_a_domain_call():
    """A single trusted path, and the other explicitly rejected.

    Asserted as a partition so a future path cannot be added to the in-scope set
    without landing on one side or the other.
    """
    assert TRUSTED_VALIDATION_PATH not in REJECTED_VALIDATION_PATHS
    assert IN_SCOPE_VALIDATION_PATHS == {TRUSTED_VALIDATION_PATH} | REJECTED_VALIDATION_PATHS


# --- the weaker path cannot satisfy a domain-API call -----------------------


def test_the_weaker_authorizer_path_cannot_satisfy_a_domain_call(policy):
    """Identical claims, admitted on one path and refused on the other.

    The claims are the ones the trusted path accepts, so the only difference is
    which validator produced them. That isolation is the point: it shows the
    boundary is enforced on provenance, not on some property of this token.
    """
    claims = valid_access_claims()

    admitted = policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)
    assert admitted.org_id == "org-acme"

    with pytest.raises(TokenRejectedError) as denied:
        policy.admit(claims, validation_path="gateway.api_authorizer")

    message = str(denied.value)
    assert "token_use" in message
    assert "allowlist" in message


def test_an_unknown_validation_path_is_refused(policy):
    """A path nobody has classified is a denial, not a pass-through.

    This is the fail-closed direction for a *new* validator: it is untrusted
    until someone adds it to the in-scope set on purpose.
    """
    with pytest.raises(TokenRejectedError) as denied:
        policy.admit(valid_access_claims(), validation_path="some.new.validator")

    assert "unknown validation path" in str(denied.value)


@pytest.mark.parametrize("path", ["", None, "GATEWAY.COGNITO_JWT", "cognito_jwt"])
def test_an_absent_or_misspelled_path_is_refused(policy, path):
    """Case and near-misses do not resolve to the trusted path.

    Included because a caller passing the wrong string is the realistic mistake,
    and it must fail loudly rather than resolve to something permissive.
    """
    with pytest.raises(TokenRejectedError):
        policy.admit(valid_access_claims(), validation_path=path)


def test_the_path_check_precedes_every_claim_check(policy):
    """A token that is garbage in every way still fails on the path first.

    Ordering matters for a reason beyond tidiness: it means a token arriving from
    the weaker path is refused before any of its claims are trusted enough to be
    read, so no claim-parsing code runs on input from an untrusted validator.
    """
    with pytest.raises(TokenRejectedError) as denied:
        policy.admit({}, validation_path="gateway.api_authorizer")

    assert "validation path" in str(denied.value)


# --- the asymmetry that motivates the boundary is real ----------------------


def test_the_authorizer_enforces_no_token_use_check():
    """The claim that makes the authorizer weaker, asserted against its source.

    If this fails because a ``token_use`` check was added, that is good news — but
    the boundary above was drawn on this asymmetry, so it should be revisited
    deliberately rather than left stale.
    """
    source = _AUTHORIZER.read_text()

    assert "token_use" not in source


def test_the_authorizer_enforces_no_client_allowlist():
    source = _AUTHORIZER.read_text()

    assert "allowed_client_ids" not in source
    assert "client_id" not in source


def test_the_authorizer_does_verify_the_issuer():
    """It is a real authenticator, not a no-op — which is why it is a bypass risk.

    A validator that rejected everything would be harmless. This one issues a
    genuine allow for a genuine pool token, so the only thing standing between it
    and the domain policy is the boundary this module tests.
    """
    source = _AUTHORIZER.read_text()

    assert "expected_issuer" in source
    assert 'claims.get("iss") != expected_issuer' in source


def test_the_trusted_path_is_the_one_that_gates_on_token_use():
    """Pins TRUSTED_VALIDATION_PATH to the validator that actually checks it."""
    source = _COGNITO_JWT.read_text()

    assert 'token_use == "access"' in source
    assert "allowed_client_ids" in source
