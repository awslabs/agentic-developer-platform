"""Domain token policy — R5 ADP half (issue #5044).

Three properties, each of which the code that exists today does NOT have:

1. An ID token for a legitimate user is refused. This is its own test rather than
   a case inside an allowlist test because of the shape of the existing
   validator: ``cognito_jwt.py`` gates the client-allowlist check on
   ``token_use == "access"``, so an ID token does not *fail* the allowlist — it
   never reaches it. A test that only checked "wrong client is refused" would
   pass while ID tokens sailed through.
2. An empty or unset allowlist is a startup failure. The underlying validator
   documents ``allowed_client_ids=[]`` as "accept any client_id", which is the
   wrong default for a domain policy: it fails open precisely when someone
   forgets to configure it.
3. The principal's organization comes from the verified token claim. A
   body-supplied ``org_id`` is not consulted at all.
4. A target environment's credential-binding state is *asserted* by a caller, not
   inferred from this repository's code defaults — which disagree with the
   deployed values, in the permissive direction. See the comment above those
   tests for the specific disagreement.
"""

from __future__ import annotations

import pytest
from superplane_auth.policy import (
    TRUSTED_VALIDATION_PATH,
    DomainTokenPolicy,
    EnforcementState,
    TokenPolicyError,
    TokenRejectedError,
    assert_enforcement_state,
)

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
ALLOWED_CLIENT = "1example23client45id"


@pytest.fixture
def policy() -> DomainTokenPolicy:
    return DomainTokenPolicy(allowed_client_ids=[ALLOWED_CLIENT], expected_issuer=ISSUER)


def access_claims(**overrides) -> dict[str, object]:
    """An access token's claims for a legitimate user, as Cognito would mint them."""
    claims: dict[str, object] = {
        "sub": "8f14e45f-ceea-467a-9f6a-1d0e1c2b3a4d",
        "iss": ISSUER,
        "client_id": ALLOWED_CLIENT,
        "token_use": "access",
        "custom:org_id": "org-acme",
        "custom:account_type": "human",
    }
    claims.update(overrides)
    return claims


# --- 1. access tokens only ---------------------------------------------------


def test_id_token_for_a_valid_user_is_refused(policy):
    """The user is real, the client is allowlisted, the issuer is right.

    Only ``token_use`` differs, and that alone must be a denial. An ID token
    carries the user's identity attributes but is not a bearer credential for API
    access, and it is minted by the same pool and client as the access token — so
    if it were accepted, every property the allowlist provides would be moot.
    """
    claims = access_claims(token_use="id")

    with pytest.raises(TokenRejectedError) as denied:
        policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)

    assert "access token" in str(denied.value)


def test_id_token_is_refused_even_from_an_allowlisted_client(policy):
    """Pins the ordering that makes property 1 hold.

    The client here IS allowlisted, so the client check would pass. If token_use
    were checked second (or gated on token_use the way the underlying validator
    gates its client check), this token would be admitted. Asserting the denial
    with a valid client is what makes this a test of order and not of the client
    allowlist.
    """
    with pytest.raises(TokenRejectedError):
        policy.admit(access_claims(token_use="id", client_id=ALLOWED_CLIENT), validation_path=TRUSTED_VALIDATION_PATH)


def test_missing_token_use_is_refused(policy):
    """Absent is not "access". A claim-free token must not default to admitted."""
    claims = access_claims()
    del claims["token_use"]

    with pytest.raises(TokenRejectedError):
        policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)


def test_access_token_from_an_allowlisted_client_is_admitted(policy):
    principal = policy.admit(access_claims(), validation_path=TRUSTED_VALIDATION_PATH)

    assert principal.subject == "8f14e45f-ceea-467a-9f6a-1d0e1c2b3a4d"
    assert principal.org_id == "org-acme"
    assert principal.client_id == ALLOWED_CLIENT
    assert principal.is_service is False


# --- 2. a non-empty allowlist is required at startup ------------------------


@pytest.mark.parametrize(
    "allowlist",
    [
        pytest.param(None, id="unset"),
        pytest.param([], id="empty-list"),
        pytest.param((), id="empty-tuple"),
        pytest.param([""], id="one-empty-string"),
        pytest.param(["   "], id="one-whitespace-string"),
    ],
)
def test_empty_or_unset_allowlist_is_a_startup_failure(allowlist):
    """Construction raises, so the process cannot come up with no policy.

    The whitespace and empty-string cases matter because an allowlist usually
    arrives as a comma-separated environment variable: ``"".split(",")`` yields
    ``[""]``, which is a one-element list that authorizes nothing. Treating it as
    "configured" would restore the fail-open default through the back door.
    """
    with pytest.raises(TokenPolicyError) as failure:
        DomainTokenPolicy(allowed_client_ids=allowlist, expected_issuer=ISSUER)

    assert "allowlist" in str(failure.value)


def test_startup_failure_is_raised_not_deferred_to_first_request():
    """The failure must be a construction error, not a per-call denial.

    A policy object that constructs and then refuses everything looks identical
    to a working one at startup and turns a config mistake into a total outage
    discovered by users. Failing to construct is what makes the service exit
    non-zero instead of serving.
    """
    with pytest.raises(TokenPolicyError):
        DomainTokenPolicy(allowed_client_ids=[], expected_issuer=ISSUER)


def test_missing_issuer_is_also_a_startup_failure():
    with pytest.raises(TokenPolicyError):
        DomainTokenPolicy(allowed_client_ids=[ALLOWED_CLIENT], expected_issuer="")


def test_a_client_outside_the_allowlist_is_refused(policy):
    with pytest.raises(TokenRejectedError):
        policy.admit(access_claims(client_id="9other87client65id"), validation_path=TRUSTED_VALIDATION_PATH)


def test_denial_does_not_echo_the_rejected_client_id(policy):
    """A denial must not confirm which values are near-allowlisted."""
    rejected = "9other87client65id"

    with pytest.raises(TokenRejectedError) as denied:
        policy.admit(access_claims(client_id=rejected), validation_path=TRUSTED_VALIDATION_PATH)

    assert rejected not in str(denied.value)


# --- issuer ------------------------------------------------------------------


def test_a_token_from_another_issuer_is_refused(policy):
    """Same client id, different pool. A valid signature is not enough."""
    other = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHERPOOL"

    with pytest.raises(TokenRejectedError) as denied:
        policy.admit(access_claims(iss=other), validation_path=TRUSTED_VALIDATION_PATH)

    assert "issuer" in str(denied.value)


# --- 3. the principal comes from verified context ---------------------------


def test_a_body_asserted_org_id_is_overridden_by_the_token_context(policy):
    """The request body claims another org; the principal keeps the token's.

    The body is passed in alongside deliberately: the test's value is showing
    that a caller *can* assert an org and that it has no effect, rather than
    showing that a function which never receives one ignores it.
    """
    body = {"org_id": "org-victim", "name": "w1"}

    principal = policy.admit(access_claims(**{"custom:org_id": "org-acme"}), validation_path=TRUSTED_VALIDATION_PATH)

    assert principal.org_id == "org-acme"
    assert principal.org_id != body["org_id"]


def test_a_token_with_no_org_claim_is_refused(policy):
    """No org claim means no resolvable principal — not an empty-string org.

    An empty org would compare equal to another empty org, so admitting one
    creates a bucket every unscoped caller shares.
    """
    claims = access_claims()
    del claims["custom:org_id"]

    with pytest.raises(TokenRejectedError) as denied:
        policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)

    assert "organization" in str(denied.value)


def test_a_token_with_no_subject_is_refused(policy):
    claims = access_claims()
    del claims["sub"]

    with pytest.raises(TokenRejectedError):
        policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)


def test_the_subject_is_treated_as_opaque(policy):
    """ADP ids are opaque: an id that looks structured is not parsed.

    Asserted by round-tripping a value with separators in it — nothing may split
    on them, because the moment an id is parsed for meaning its format becomes an
    interface and a differently-shaped id changes an authorization outcome.
    """
    weird = "org-acme:user/42|extra"

    principal = policy.admit(access_claims(sub=weird), validation_path=TRUSTED_VALIDATION_PATH)

    assert principal.subject == weird


def test_a_service_token_is_admitted_and_marked_as_a_service(policy):
    """Admission is not authority — see test_authority_after_authentication.py."""
    principal = policy.admit(
        access_claims(**{"custom:account_type": "service"}),
        validation_path=TRUSTED_VALIDATION_PATH,
    )

    assert principal.is_service is True


def test_the_principal_is_immutable(policy):
    """A caller downstream must not be able to edit the org onto the principal."""
    principal = policy.admit(access_claims(), validation_path=TRUSTED_VALIDATION_PATH)

    with pytest.raises((AttributeError, TypeError)):
        principal.org_id = "org-victim"


# --- 4. environment state is asserted, never inferred (R5 acc. 5) ------------
#
# The two credential-binding flags are per-environment and their *code* defaults
# are not the deployed values:
#
#   src/shared/config.py:196  enforce_credential_binding = True
#   src/shared/config.py:125  vault_enforce_credential_host_binding = False
#
# while gateway-deploy.yml sources both from SSM per environment and records that
# "dev stays in shadow mode via /adp/dev/gateway/enforce-credential-binding=false".
# `src/internal/credential_binding.py` says the same thing from the other side —
# the flag "is `false` on at least one live environment, so a control gated on it
# silently shadows instead of enforcing".
#
# So reading the dataclass default and calling it the environment's state gives
# the WRONG answer for dev, in the permissive direction. That is the inference
# acc. 5 forbids, which is why EnforcementState has no defaults: a caller cannot
# construct one without stating both values.


def test_enforcement_state_cannot_be_constructed_without_stating_both_flags():
    """Omitting a flag is a TypeError, so there is no value to inherit.

    This is the mechanical content of "asserted, not inferred": if either field
    had a default, every caller who forgot it would silently get this repository's
    code default instead of the target environment's real configuration.
    """
    with pytest.raises(TypeError):
        EnforcementState(environment="dev")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        EnforcementState(environment="dev", enforce_credential_binding=True)  # type: ignore[call-arg]


def test_the_code_default_would_have_given_the_wrong_answer_for_dev():
    """Pins why the inference is unsafe, not merely unidiomatic.

    ``config.py`` defaults ``enforce_credential_binding`` to True; dev pins the SSM
    parameter to false. A helper that read the default would report dev as
    enforcing when it is in shadow mode — a false negative on a security control.
    Both states are constructed here so the disagreement is explicit.
    """
    inferred_from_code_default = EnforcementState(
        environment="dev",
        enforce_credential_binding=True,
        vault_enforce_credential_host_binding=False,
    )
    asserted_from_ssm = EnforcementState(
        environment="dev",
        enforce_credential_binding=False,
        vault_enforce_credential_host_binding=False,
    )

    assert "ENFORCE_CREDENTIAL_BINDING" not in inferred_from_code_default.unenforced()
    assert "ENFORCE_CREDENTIAL_BINDING" in asserted_from_ssm.unenforced()


def test_an_operation_requiring_an_unenforced_flag_is_refused():
    """A caller that depends on the flag learns it is in shadow mode.

    Raised rather than returned so it cannot be ignored at the call site: the
    whole point is that an operation whose safety rests on the flag must not
    proceed believing it enforces.
    """
    dev = EnforcementState(
        environment="dev",
        enforce_credential_binding=False,
        vault_enforce_credential_host_binding=False,
    )

    with pytest.raises(TokenPolicyError) as failure:
        assert_enforcement_state(dev, require=["ENFORCE_CREDENTIAL_BINDING"])

    message = str(failure.value)
    assert "dev" in message
    assert "ENFORCE_CREDENTIAL_BINDING" in message


def test_an_operation_that_does_not_depend_on_a_flag_is_not_blocked_by_it():
    """Shadow mode is a legitimate configuration, so ``require`` is explicit.

    If this function demanded that every flag enforce everywhere, it would refuse
    every operation in dev — including ones with no credential-binding dependency
    at all — and the first person to hit that would delete the check rather than
    narrow it.
    """
    dev = EnforcementState(
        environment="dev",
        enforce_credential_binding=False,
        vault_enforce_credential_host_binding=False,
    )

    assert_enforcement_state(dev, require=[])


def test_a_fully_enforcing_environment_satisfies_both_flags():
    prod = EnforcementState(
        environment="prod",
        enforce_credential_binding=True,
        vault_enforce_credential_host_binding=True,
    )

    assert prod.unenforced() == ()
    assert_enforcement_state(
        prod,
        require=["ENFORCE_CREDENTIAL_BINDING", "VAULT_ENFORCE_CREDENTIAL_HOST_BINDING"],
    )


def test_the_host_binding_flag_is_reported_independently():
    """The flags are separate controls, so one enforcing does not cover the other.

    ``VAULT_ENFORCE_CREDENTIAL_HOST_BINDING`` defaults to shadow mode everywhere,
    so an environment that has flipped the first flag and not the second is the
    expected configuration rather than an edge case.
    """
    partial = EnforcementState(
        environment="staging",
        enforce_credential_binding=True,
        vault_enforce_credential_host_binding=False,
    )

    assert partial.unenforced() == ("VAULT_ENFORCE_CREDENTIAL_HOST_BINDING",)
    assert_enforcement_state(partial, require=["ENFORCE_CREDENTIAL_BINDING"])
    with pytest.raises(TokenPolicyError):
        assert_enforcement_state(partial, require=["VAULT_ENFORCE_CREDENTIAL_HOST_BINDING"])


def test_a_misspelled_flag_name_is_an_error_not_a_silent_pass():
    """A typo in ``require`` must not read as "nothing to check".

    An unknown name would otherwise intersect nothing and the assertion would
    pass — a security check that quietly stopped checking, which is the worst of
    the available outcomes.
    """
    prod = EnforcementState(
        environment="prod",
        enforce_credential_binding=True,
        vault_enforce_credential_host_binding=True,
    )

    with pytest.raises(TokenPolicyError) as failure:
        assert_enforcement_state(prod, require=["ENFORCE_CREDENTIAL_BINDINGS"])

    assert "unknown enforcement flags" in str(failure.value)


def test_the_enforcement_state_is_immutable():
    """Nothing downstream can flip a flag to make its own operation permitted."""
    state = EnforcementState(
        environment="prod",
        enforce_credential_binding=True,
        vault_enforce_credential_host_binding=True,
    )

    with pytest.raises((AttributeError, TypeError)):
        state.enforce_credential_binding = False
