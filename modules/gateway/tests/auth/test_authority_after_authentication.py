"""Authentication is not authorization — R5 acc. 6–7 (issue #5044).

A token proves who is calling. It does not carry what they may do, and a token
that authenticates successfully has been *admitted*, not *authorized*. The two
failures this file pins are the ones that follow from conflating them:

1. **A service token is not a delegation.** A service principal can authenticate
   perfectly well and has no authority of its own on workspace W. Its ability to
   present a valid token must not stand in for a user's authority — otherwise any
   component holding a service credential acts as everyone.
2. **An operation with no server-held authorization record is refused.** The
   record is issued and held by the server. Authority is not something that
   travels with the request, because anything travelling with the request is
   caller-supplied.

Together these make the check re-runnable: because the decision consults a stored
record rather than the session that authenticated, running it again at the
operation gives a current answer rather than an echo of the login.
"""

from __future__ import annotations

import pytest
from superplane_auth.policy import (
    TRUSTED_VALIDATION_PATH,
    AuthorizationDeniedError,
    DomainPrincipal,
    DomainTokenPolicy,
    OperationAuthorization,
    Permission,
    WorkspaceAuthorizationModel,
    WorkspaceGrant,
    expand_permissions,
)

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
ALLOWED_CLIENT = "1example23client45id"

ORG = "org-acme"
WORKSPACE = "ws-research-01"
HUMAN = "user-owner"
SERVICE = "svc-provisioner"


@pytest.fixture
def policy() -> DomainTokenPolicy:
    return DomainTokenPolicy(allowed_client_ids=[ALLOWED_CLIENT], expected_issuer=ISSUER)


@pytest.fixture
def model() -> WorkspaceAuthorizationModel:
    """The human owns W. The service principal holds nothing on W."""
    model = WorkspaceAuthorizationModel()
    model.record_grant(
        WorkspaceGrant(WORKSPACE, ORG, HUMAN, expand_permissions([Permission.ADMINISTER])),
    )
    return model


def claims(subject: str, account_type: str) -> dict[str, object]:
    return {
        "sub": subject,
        "iss": ISSUER,
        "client_id": ALLOWED_CLIENT,
        "token_use": "access",
        "custom:org_id": ORG,
        "custom:account_type": account_type,
    }


def service_principal(policy: DomainTokenPolicy) -> DomainPrincipal:
    """A service principal that really did authenticate — via the real policy.

    Deliberately obtained by admitting a token rather than constructing the
    dataclass: the claim under test is that *successful authentication* confers no
    authority, so the test has to actually authenticate.
    """
    principal = policy.admit(claims(SERVICE, "service"), validation_path=TRUSTED_VALIDATION_PATH)
    assert principal.is_service is True
    return principal


# --- 1. a service token carries no delegated authority ----------------------


def test_a_service_token_authenticates_and_is_still_refused(policy, model):
    """Both halves in one test, because the pairing is the property.

    A test that only showed the denial would be indistinguishable from a token
    that failed to validate. The assertion above the denial is what makes this
    "authenticated, then unauthorized".
    """
    principal = service_principal(policy)

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize(principal, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert "no workspace authorization record" in str(denied.value)


def test_a_service_token_in_the_right_org_is_still_refused(policy, model):
    """Same org as the workspace. Still nothing.

    Service principals are typically minted inside the org they serve, so this is
    the realistic configuration rather than an edge case.
    """
    principal = service_principal(policy)
    assert principal.org_id == ORG

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal, WORKSPACE, Permission.READ, workspace_org_id=ORG)


def test_a_service_token_cannot_use_a_record_issued_to_a_human(policy, model):
    """The delegation attempt, stated directly.

    The record is real and covers the operation — it was simply issued to someone
    else. A model that checked only "is there an authorization for this operation?"
    would allow this, which is how a service credential becomes an
    act-as-anyone credential.
    """
    principal = service_principal(policy)
    issued_to_human = OperationAuthorization(
        operation_id="op-provision-1",
        workspace_id=WORKSPACE,
        principal=HUMAN,
        permission=Permission.PROVISION,
    )

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize_operation(principal, issued_to_human, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert "different principal" in str(denied.value)


def test_a_service_principal_with_its_own_grant_is_allowed(policy, model):
    """Service principals are not banned — they are just not implicitly trusted.

    Without this, the tests above would also pass for a model that rejected every
    service token outright, which would be a different (and unusable) policy.
    """
    model.record_grant(WorkspaceGrant(WORKSPACE, ORG, SERVICE, expand_permissions([Permission.PROVISION])))
    principal = service_principal(policy)

    grant = model.authorize(principal, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert grant.principal == SERVICE


def test_a_service_grant_is_scoped_to_its_workspace(policy, model):
    """A service principal authorized on one workspace is not authorized on all.

    The plausible mistake for a background worker: grant it once, let it run
    everywhere.
    """
    model.record_grant(WorkspaceGrant(WORKSPACE, ORG, SERVICE, expand_permissions([Permission.PROVISION])))
    principal = service_principal(policy)

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal, "ws-other-02", Permission.PROVISION, workspace_org_id=ORG)


# --- 2. an operation needs a server-held authorization record ----------------


def test_an_operation_with_no_authorization_record_is_refused(policy, model):
    """``None`` is refused even for the workspace owner.

    The owner would pass every permission check. The denial is about the missing
    record, so this isolates the record requirement from the permission
    requirement — the caller here lacks nothing except the binding.
    """
    owner = policy.admit(claims(HUMAN, "human"), validation_path=TRUSTED_VALIDATION_PATH)

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize_operation(owner, None, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert "no server-held authorization record" in str(denied.value)


def test_a_record_for_another_workspace_does_not_authorize_this_one(policy, model):
    """Right principal, right permission, wrong workspace.

    Prevents the replay of a legitimate authorization against a different
    workspace — the record has to name the workspace it authorizes.
    """
    owner = policy.admit(claims(HUMAN, "human"), validation_path=TRUSTED_VALIDATION_PATH)
    elsewhere = OperationAuthorization("op-1", "ws-other-02", HUMAN, Permission.PROVISION)

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize_operation(owner, elsewhere, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert "different workspace" in str(denied.value)


def test_a_record_for_a_lesser_permission_does_not_authorize_a_greater_one(policy, model):
    """A record must cover the operation being attempted, not merely exist.

    Uses READ → PROVISION because READ is the permission every caller has, so a
    read-scoped record is the one most likely to be lying around.
    """
    owner = policy.admit(claims(HUMAN, "human"), validation_path=TRUSTED_VALIDATION_PATH)
    read_only = OperationAuthorization("op-1", WORKSPACE, HUMAN, Permission.READ)

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize_operation(owner, read_only, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert "does not cover this operation" in str(denied.value)


def test_a_valid_record_still_defers_to_the_workspace_grant(policy, model):
    """The record does not replace the grant check; both must hold.

    This is the one that keeps the record from becoming a bypass of its own. A
    record issued before a grant was revoked must not authorize afterwards, so the
    grant is consulted at the operation even when the record checks out.
    """
    owner = policy.admit(claims(HUMAN, "human"), validation_path=TRUSTED_VALIDATION_PATH)
    record = OperationAuthorization("op-1", WORKSPACE, HUMAN, Permission.PROVISION)

    assert model.authorize_operation(owner, record, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    del model.grants[(WORKSPACE, HUMAN)]

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize_operation(owner, record, WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert "no workspace authorization record" in str(denied.value)


def test_an_authorization_record_is_immutable(policy):
    """A record cannot be re-pointed at another principal or permission in flight."""
    record = OperationAuthorization("op-1", WORKSPACE, HUMAN, Permission.READ)

    with pytest.raises((AttributeError, TypeError)):
        record.principal = SERVICE
    with pytest.raises((AttributeError, TypeError)):
        record.permission = Permission.ADMINISTER


def test_the_same_record_authorizes_each_operation_it_covers(policy, model):
    """Re-checking is not single-use: the record is not a nonce.

    Worth stating so nobody "fixes" the re-check by consuming records, which would
    break every legitimate repeat call while providing no additional safety — the
    per-operation grant lookup is what makes the repeat meaningful.
    """
    owner = policy.admit(claims(HUMAN, "human"), validation_path=TRUSTED_VALIDATION_PATH)
    record = OperationAuthorization("op-1", WORKSPACE, HUMAN, Permission.SPEND)

    for _ in range(3):
        assert model.authorize_operation(owner, record, WORKSPACE, Permission.SPEND, workspace_org_id=ORG)
