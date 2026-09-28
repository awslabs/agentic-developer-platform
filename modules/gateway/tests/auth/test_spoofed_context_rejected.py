"""Client-supplied context is never authority — R5 acc. 6–7 (issue #5044).

Every value in this file is one a caller can set. The property under test is that
none of them changes an authorization outcome:

* **Identity headers are stripped at ingress**, before a principal exists. Not
  validated, not preferred-if-absent — removed. The gateway already treats an
  ``X-Caller-Identity`` on client input as terminal rather than falling through to
  the JWT path (#3985); this generalizes that to the family.
* **A fabricated envelope is not an authorization record.** The record is issued
  and held server-side. Something shaped like one, arriving in a payload, is
  caller input wearing the right shape.

Why stripping by *prefix* and not by enumeration
------------------------------------------------
An enumerated deny-list is correct on the day it is written and wrong the first
time a new plane adds a header. The failure is silent and in the permissive
direction: the new header is trusted by default because nobody remembered to add
it. So the rule is a prefix rule, and
``test_a_newly_invented_header_in_the_family_is_stripped_too`` asserts that a
header nobody has heard of is already covered.
"""

from __future__ import annotations

import pytest
from superplane_auth.policy import (
    STRIPPED_IDENTITY_HEADER_PREFIXES,
    STRIPPED_IDENTITY_HEADERS,
    TRUSTED_VALIDATION_PATH,
    AuthorizationDeniedError,
    DomainTokenPolicy,
    OperationAuthorization,
    Permission,
    TokenRejectedError,
    WorkspaceAuthorizationModel,
    WorkspaceGrant,
    authorize_request,
    expand_permissions,
    strip_identity_headers,
)

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
ALLOWED_CLIENT = "1example23client45id"

ORG = "org-acme"
VICTIM_ORG = "org-victim"
WORKSPACE = "ws-research-01"
VICTIM_WORKSPACE = "ws-victim-09"
ATTACKER = "user-attacker"
VICTIM = "user-victim"


@pytest.fixture
def policy() -> DomainTokenPolicy:
    return DomainTokenPolicy(allowed_client_ids=[ALLOWED_CLIENT], expected_issuer=ISSUER)


@pytest.fixture
def model() -> WorkspaceAuthorizationModel:
    """The attacker owns their own workspace. They hold nothing on the victim's."""
    model = WorkspaceAuthorizationModel()
    model.record_grant(WorkspaceGrant(WORKSPACE, ORG, ATTACKER, expand_permissions([Permission.ADMINISTER])))
    model.record_grant(WorkspaceGrant(VICTIM_WORKSPACE, VICTIM_ORG, VICTIM, expand_permissions([Permission.ADMINISTER])))
    return model


def attacker_claims(**overrides) -> dict[str, object]:
    claims: dict[str, object] = {
        "sub": ATTACKER,
        "iss": ISSUER,
        "client_id": ALLOWED_CLIENT,
        "token_use": "access",
        "custom:org_id": ORG,
        "custom:account_type": "human",
    }
    claims.update(overrides)
    return claims


def spoofed_headers() -> dict[str, str]:
    """Every identity header a caller might try, in one request."""
    return {
        "Authorization": "Bearer real-token",
        "Content-Type": "application/json",
        "X-Caller-Identity": VICTIM,
        "X-Authenticated-User": VICTIM,
        "X-Org-Id": VICTIM_ORG,
        "X-Workspace-Id": VICTIM_WORKSPACE,
        "X-On-Behalf-Of": VICTIM,
        "X-Adp-Principal": VICTIM,
        "X-Superplane-Workspace": VICTIM_WORKSPACE,
        "Authorization-Context": '{"org_id": "org-victim"}',
    }


# --- identity headers are stripped ------------------------------------------


def test_every_spoofed_identity_header_is_removed():
    kept = strip_identity_headers(spoofed_headers())

    for name in spoofed_headers():
        if name.lower() in {"authorization", "content-type"}:
            continue
        assert name.lower() not in kept, name


def test_ordinary_headers_survive():
    """Stripping is targeted. A blanket removal would break the request.

    ``Authorization`` in particular must survive: it is the *token*, which is the
    one identity input that IS trusted — after verification. Removing it would
    make the endpoint unauthenticated rather than secure.
    """
    kept = strip_identity_headers(spoofed_headers())

    assert kept["authorization"] == "Bearer real-token"
    assert kept["content-type"] == "application/json"


@pytest.mark.parametrize("name", sorted(STRIPPED_IDENTITY_HEADERS))
def test_each_enumerated_identity_header_is_stripped(name):
    assert strip_identity_headers({name: "x"}) == {}


@pytest.mark.parametrize("prefix", STRIPPED_IDENTITY_HEADER_PREFIXES)
def test_a_newly_invented_header_in_the_family_is_stripped_too(prefix):
    """A header nobody has thought of yet is already covered.

    This is the whole argument for a prefix rule over an enumeration: the
    suffix here is deliberately meaningless, so the test passes because of the
    rule's shape and not because someone listed this name.
    """
    invented = f"{prefix}some-header-nobody-has-added-yet"

    assert strip_identity_headers({invented: VICTIM}) == {}


def test_stripping_is_case_insensitive():
    """HTTP header names are case-insensitive; a deny-list keyed on case is not.

    Sending ``x-oRg-Id`` is the cheapest possible bypass of a naive check.
    """
    assert strip_identity_headers({"X-ORG-ID": VICTIM_ORG, "x-oRg-Id": VICTIM_ORG}) == {}


def test_stripping_does_not_mutate_the_caller_s_headers():
    """Returns a new mapping — an in-place edit would surprise a middleware."""
    original = spoofed_headers()

    strip_identity_headers(original)

    assert original["X-Org-Id"] == VICTIM_ORG


def test_a_spoofed_org_header_does_not_change_the_principal(policy):
    """The end-to-end version: headers assert the victim, principal stays the attacker.

    Asserted through the real ``admit`` rather than by inspecting the stripped
    mapping, because the risk is not that stripping fails — it is that some later
    step reads the header anyway.
    """
    principal = policy.admit(attacker_claims(), validation_path=TRUSTED_VALIDATION_PATH)

    assert principal.subject == ATTACKER
    assert principal.org_id == ORG


def test_a_spoofed_header_cannot_reach_another_org_s_workspace(policy, model):
    """The attack in full, through the composed entry point.

    Headers name the victim, their org and their workspace. The request is
    authorized against the *token's* principal, so it is refused on the victim's
    workspace — where the attacker holds no grant.
    """
    with pytest.raises(AuthorizationDeniedError):
        authorize_request(
            policy,
            model,
            claims=attacker_claims(),
            validation_path=TRUSTED_VALIDATION_PATH,
            method="POST",
            path_template="/superplane/v1/workspaces/{workspace}/kubeconfig",
            workspace_id=VICTIM_WORKSPACE,
            workspace_org_id=VICTIM_ORG,
            authorization=OperationAuthorization("op-1", VICTIM_WORKSPACE, ATTACKER, Permission.PROVISION),
            headers=spoofed_headers(),
        )


def test_the_attacker_can_still_reach_their_own_workspace(policy, model):
    """The control. Without it, the denial above could be an unrelated failure.

    Same call, same spoofed headers, only the workspace changes — so the previous
    test's denial is attributable to authorization and not to the headers having
    broken the request outright.
    """
    principal, grant, safe_headers = authorize_request(
        policy,
        model,
        claims=attacker_claims(),
        validation_path=TRUSTED_VALIDATION_PATH,
        method="POST",
        path_template="/superplane/v1/workspaces/{workspace}/kubeconfig",
        workspace_id=WORKSPACE,
        workspace_org_id=ORG,
        authorization=OperationAuthorization("op-1", WORKSPACE, ATTACKER, Permission.PROVISION),
        headers=spoofed_headers(),
    )

    assert principal.subject == ATTACKER
    assert grant.workspace_id == WORKSPACE


# --- a request body is not authority ----------------------------------------


def test_a_body_asserted_org_id_does_not_move_the_principal(policy):
    """R5's "principal resolved from verified context, never a body ``org_id``".

    The body is constructed and passed nowhere on purpose: the point is that the
    principal is complete without it, so there is no code path where a body could
    contribute to it.
    """
    body = {"org_id": VICTIM_ORG, "workspace_id": VICTIM_WORKSPACE, "principal": VICTIM}

    principal = policy.admit(attacker_claims(), validation_path=TRUSTED_VALIDATION_PATH)

    assert principal.org_id == ORG
    assert principal.org_id != body["org_id"]


def test_a_forged_org_claim_on_the_token_is_not_a_bypass_of_the_grant(policy, model):
    """If a token's org claim were ever wrong, the grant still refuses.

    Not a test of the signature — a forged claim would fail verification upstream.
    It is defence in depth: authority comes from the stored grant, so even a token
    that lied about its org reaches nothing.
    """
    principal = policy.admit(attacker_claims(**{"custom:org_id": VICTIM_ORG}), validation_path=TRUSTED_VALIDATION_PATH)
    assert principal.org_id == VICTIM_ORG

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal, VICTIM_WORKSPACE, Permission.READ, workspace_org_id=VICTIM_ORG)


# --- a fabricated envelope is not an authorization record --------------------


def test_a_fabricated_envelope_is_not_an_authorization_record(policy, model):
    """A payload that looks like an authorization is still caller input.

    The dict has every field the real record has and the correct values. It is
    refused because the argument must *be* the server-held record — the type is
    load-bearing, so a payload cannot be passed where a record is expected.
    """
    forged = {
        "operation_id": "op-1",
        "workspace_id": VICTIM_WORKSPACE,
        "principal": ATTACKER,
        "permission": Permission.PROVISION,
    }

    with pytest.raises((AuthorizationDeniedError, AttributeError, TypeError)):
        model.authorize_operation(
            policy.admit(attacker_claims(), validation_path=TRUSTED_VALIDATION_PATH),
            forged,  # type: ignore[arg-type]
            VICTIM_WORKSPACE,
            Permission.PROVISION,
            workspace_org_id=VICTIM_ORG,
        )


def test_a_self_issued_record_for_another_workspace_is_refused(policy, model):
    """The attacker constructs a real record object naming their own principal.

    The realistic version of the attack above: not a dict, but a genuine
    ``OperationAuthorization`` the caller built. It fails on the grant, because
    constructing the object is not the same as the server having issued it — which
    is why the grant check runs after the record check rather than instead of it.
    """
    self_issued = OperationAuthorization("op-1", VICTIM_WORKSPACE, ATTACKER, Permission.PROVISION)

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize_operation(
            policy.admit(attacker_claims(), validation_path=TRUSTED_VALIDATION_PATH),
            self_issued,
            VICTIM_WORKSPACE,
            Permission.PROVISION,
            workspace_org_id=VICTIM_ORG,
        )

    assert "no workspace authorization record" in str(denied.value)


def test_a_webhook_style_envelope_cannot_authorize_by_naming_a_sender(policy, model):
    """A webhook's own claim about who sent it is not authority.

    Named because webhook envelopes are the archetype: the payload carries a
    sender, an installation and a repository, all attacker-controllable if the
    endpoint is reachable. Authorization has to come from the token that reached
    the endpoint, so an envelope naming the victim gets the attacker's authority.
    """
    envelope = {"sender": {"login": VICTIM}, "installation": {"id": 42}, "org_id": VICTIM_ORG}

    principal = policy.admit(attacker_claims(), validation_path=TRUSTED_VALIDATION_PATH)

    assert principal.subject == ATTACKER
    assert principal.subject != envelope["sender"]["login"]
    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal, VICTIM_WORKSPACE, Permission.READ, workspace_org_id=VICTIM_ORG)


def test_spoofed_headers_cannot_promote_the_weaker_validation_path(policy, model):
    """A caller cannot claim a better provenance for their own token.

    The composed call is given the weaker path plus headers asserting a trusted
    one. The path argument comes from the server's own wiring, so the headers are
    inert and the refusal is the path refusal.
    """
    headers = spoofed_headers() | {"X-Adp-Validation-Path": TRUSTED_VALIDATION_PATH}

    with pytest.raises(TokenRejectedError) as denied:
        authorize_request(
            policy,
            model,
            claims=attacker_claims(),
            validation_path="gateway.api_authorizer",
            method="GET",
            path_template="/superplane/v1/workspaces/{workspace}",
            workspace_id=WORKSPACE,
            workspace_org_id=ORG,
            authorization=OperationAuthorization("op-1", WORKSPACE, ATTACKER, Permission.READ),
            headers=headers,
        )

    assert "validation path" in str(denied.value)
