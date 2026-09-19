"""Per-workspace authorization — R6 ADP half (issue #5044).

The behaviour being replaced: no membership concept exists on either side. The
upstream workspace model has no member association and handlers filter on
``Workspace.org_id == org_id`` alone, so **every org-mate reaches every
workspace**. ``GET /workspaces/{W}/kubeconfig`` therefore succeeds for anyone in
W's organization — cluster credentials, on the strength of being a colleague.

Two properties, both of which are denials that today's code allows:

1. **Same-org membership is insufficient.** Authority comes from a per-workspace
   record. Sharing an organization with a workspace grants nothing.
2. **A read-authorized caller cannot spend, provision or renew.** Holding *some*
   authority on W is not holding *this* authority on W.

On the regression baseline
--------------------------
The story asks for R6 acc. 1 as a regression test with a known-failing baseline:
the kubeconfig call succeeding before the change and failing after. That
before-state cannot be demonstrated here, and this file does not pretend
otherwise. The endpoint lives in the upstream domain API, which is not in this
repository; the model is what ADP owns, and U14 wires it into that API. What is
asserted here is the decision the endpoint will consult, including the exact
org-mate case — see ``test_an_org_mate_with_no_grant_is_refused_a_kubeconfig``.
"""

from __future__ import annotations

import pytest
from superplane_auth.policy import (
    ADP_ROLE_PERMISSIONS,
    TRUSTED_VALIDATION_PATH,
    AuthorizationDeniedError,
    DomainPrincipal,
    DomainTokenPolicy,
    OperationAuthorization,
    Permission,
    WorkspaceAuthorizationModel,
    WorkspaceGrant,
    expand_permissions,
    permissions_for_adp_role,
    required_permission,
)

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
ALLOWED_CLIENT = "1example23client45id"

ORG = "org-acme"
WORKSPACE = "ws-research-01"

OWNER = "user-owner"
ORG_MATE = "user-org-mate"
READER = "user-reader"


def principal(subject: str, org_id: str = ORG, account_type: str = "human") -> DomainPrincipal:
    """A principal as the token policy would produce it.

    Built directly rather than through a token in most tests: these are
    authorization tests, and minting a token in each one would make a policy
    change look like an authorization failure.
    """
    return DomainPrincipal(subject=subject, org_id=org_id, client_id=ALLOWED_CLIENT, account_type=account_type)


@pytest.fixture
def model() -> WorkspaceAuthorizationModel:
    """A workspace with one owner and one read-only member.

    ORG_MATE is deliberately NOT granted anything: they are in the same
    organization as the workspace, which is the whole point.
    """
    model = WorkspaceAuthorizationModel()
    model.record_grant(
        WorkspaceGrant(
            workspace_id=WORKSPACE,
            org_id=ORG,
            principal=OWNER,
            permissions=expand_permissions([Permission.ADMINISTER]),
        )
    )
    model.record_grant(
        WorkspaceGrant(
            workspace_id=WORKSPACE,
            org_id=ORG,
            principal=READER,
            permissions=expand_permissions([Permission.READ]),
        )
    )
    return model


def authorization(subject: str, permission: Permission, workspace: str = WORKSPACE) -> OperationAuthorization:
    """The server-held record for one operation."""
    return OperationAuthorization(operation_id="op-1", workspace_id=workspace, principal=subject, permission=permission)


# --- 1. same-org membership is insufficient ---------------------------------


def test_a_non_member_org_mate_is_refused(model):
    """The caller is authenticated, in the right org, and still denied.

    Everything today's handler checks is satisfied here: the token is valid and
    ``principal.org_id == workspace_org_id``. The denial comes from the absence of
    a grant, which is the record R6 introduces.
    """
    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize(principal(ORG_MATE), WORKSPACE, Permission.READ, workspace_org_id=ORG)

    assert "no workspace authorization record" in str(denied.value)


def test_an_org_mate_with_no_grant_is_refused_a_kubeconfig(model):
    """R6 acc. 1's case, as this repository can express it.

    This is the operation that succeeds today for any org-mate. The permission is
    resolved from the endpoint inventory rather than hardcoded, so the test also
    pins that a kubeconfig is treated as cluster provisioning and not as a read —
    if someone re-files it as READ, this stops testing what it claims to.

    Scope note: this asserts the *decision*. The endpoint that must start
    honouring it is upstream (U14); see this module's docstring.
    """
    permission = required_permission("GET", "/superplane/v1/workspaces/{workspace}/kubeconfig")
    assert permission is Permission.PROVISION

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal(ORG_MATE), WORKSPACE, permission, workspace_org_id=ORG)


def test_a_read_authorized_caller_is_also_refused_a_kubeconfig(model):
    """Being a genuine member is not enough either.

    Distinguishes this from a membership check: READER *has* a grant, so a model
    that only asked "is there a record?" would allow this.
    """
    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize(principal(READER), WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert Permission.PROVISION.value in str(denied.value)


def test_a_grant_on_one_workspace_confers_nothing_on_another(model):
    """Grants are per-workspace, not per-org or per-principal."""
    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal(OWNER), "ws-other-02", Permission.READ, workspace_org_id=ORG)


def test_a_caller_from_another_org_is_refused_even_holding_a_grant(model):
    """A grant stored against the wrong org must not authorize.

    Defence in depth: if a bad write ever created a cross-org grant, the org
    comparison refuses it rather than honouring the row.
    """
    model.record_grant(
        WorkspaceGrant(
            workspace_id=WORKSPACE,
            org_id="org-other",
            principal="user-outsider",
            permissions=expand_permissions([Permission.ADMINISTER]),
        )
    )

    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize(principal("user-outsider", org_id="org-other"), WORKSPACE, Permission.READ, workspace_org_id=ORG)

    assert "organization" in str(denied.value)


def test_the_owner_is_allowed(model):
    """The model is not vacuously restrictive."""
    grant = model.authorize(principal(OWNER), WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)

    assert grant.principal == OWNER


# --- 2. a read grant cannot spend, provision or renew -----------------------


@pytest.mark.parametrize(
    "permission",
    [Permission.SPEND, Permission.PROVISION, Permission.RENEW_CREDENTIAL, Permission.ADMINISTER],
)
def test_a_read_authorized_caller_cannot_escalate(model, permission):
    """Each of the three named escalations, plus administration."""
    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal(READER), WORKSPACE, permission, workspace_org_id=ORG)


def test_a_read_authorized_caller_can_read(model):
    assert model.authorize(principal(READER), WORKSPACE, Permission.READ, workspace_org_id=ORG).principal == READER


def test_spend_does_not_confer_provision_and_provision_does_not_confer_spend(model):
    """The two mutating permissions are independent, not a ladder.

    Stated as a test because the intuitive reading of "higher privilege" would
    make one imply the other. Deploying a model and handing out cluster
    credentials are different blast radii, so neither implies the other.
    """
    model.record_grant(
        WorkspaceGrant(WORKSPACE, ORG, "user-spender", expand_permissions([Permission.SPEND])),
    )
    model.record_grant(
        WorkspaceGrant(WORKSPACE, ORG, "user-provisioner", expand_permissions([Permission.PROVISION])),
    )

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal("user-spender"), WORKSPACE, Permission.PROVISION, workspace_org_id=ORG)
    with pytest.raises(AuthorizationDeniedError):
        model.authorize(principal("user-provisioner"), WORKSPACE, Permission.SPEND, workspace_org_id=ORG)


def test_every_permission_implies_read():
    """A caller who may act may describe. The one implication that exists."""
    for permission in (Permission.SPEND, Permission.PROVISION, Permission.RENEW_CREDENTIAL, Permission.ADMINISTER):
        assert Permission.READ in expand_permissions([permission]), permission


def test_administer_implies_every_permission():
    assert expand_permissions([Permission.ADMINISTER]) == set(Permission)


def test_a_denial_does_not_enumerate_what_the_caller_holds(model):
    """A denial names the missing permission, never the caller's whole grant.

    Listing what someone does hold on a workspace is information about the estate,
    and it is the kind of detail that leaks into a client-visible error message.
    """
    with pytest.raises(AuthorizationDeniedError) as denied:
        model.authorize(principal(READER), WORKSPACE, Permission.SPEND, workspace_org_id=ORG)

    message = str(denied.value)
    assert Permission.SPEND.value in message
    assert Permission.READ.value not in message


# --- authority is re-checked at the operation, never inherited --------------


def test_authority_is_rechecked_when_the_grant_is_revoked_mid_session(model):
    """A revocation takes effect on the next operation.

    This is what "re-checked at the operation" buys, expressed as behaviour rather
    than as a code-shape claim: the principal object is unchanged and reused
    across both calls — exactly as a live session would hold it — and the second
    call still denies. A model that resolved authority once and cached it on the
    session would allow the second call.
    """
    caller = principal(OWNER)
    assert model.authorize(caller, WORKSPACE, Permission.SPEND, workspace_org_id=ORG)

    del model.grants[(WORKSPACE, OWNER)]

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(caller, WORKSPACE, Permission.SPEND, workspace_org_id=ORG)


def test_a_downgrade_mid_session_takes_effect_on_the_next_operation(model):
    """Narrowing a grant is honoured too, not just removing it."""
    caller = principal(OWNER)
    assert model.authorize(caller, WORKSPACE, Permission.RENEW_CREDENTIAL, workspace_org_id=ORG)

    model.record_grant(WorkspaceGrant(WORKSPACE, ORG, OWNER, expand_permissions([Permission.READ])))

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(caller, WORKSPACE, Permission.RENEW_CREDENTIAL, workspace_org_id=ORG)


def test_credential_renewal_is_checked_at_the_renewal(model):
    """Named explicitly because R6 lists renewal as its own re-check point.

    A credential granted once must not stay renewable after the authority behind
    it is withdrawn — the renewal is a fresh decision, not a continuation.
    """
    model.record_grant(WorkspaceGrant(WORKSPACE, ORG, "user-renewer", expand_permissions([Permission.RENEW_CREDENTIAL])))
    caller = principal("user-renewer")
    permission = required_permission("POST", "/superplane/v1/providers")

    assert model.authorize(caller, WORKSPACE, permission, workspace_org_id=ORG)

    model.record_grant(WorkspaceGrant(WORKSPACE, ORG, "user-renewer", expand_permissions([Permission.READ])))

    with pytest.raises(AuthorizationDeniedError):
        model.authorize(caller, WORKSPACE, permission, workspace_org_id=ORG)


def test_a_grant_is_immutable_so_it_cannot_be_widened_in_place(model):
    """Prevents the "add a permission to the object I was handed" escalation."""
    grant = model.grant_for(WORKSPACE, READER)

    with pytest.raises((AttributeError, TypeError)):
        grant.permissions = frozenset(Permission)


# --- ADP roles map to domain permissions without sharing a vocabulary -------


def test_an_unknown_adp_role_grants_nothing():
    """A renamed or new ADP role is unprivileged until someone decides.

    The failure this prevents: mapping by resemblance, so that an upstream rename
    to something that *looks* administrative silently widens access.
    """
    assert permissions_for_adp_role("org-admin") == frozenset()
    assert permissions_for_adp_role("platform_admin") == frozenset()
    assert permissions_for_adp_role("") == frozenset()


def test_the_role_map_is_explicit_and_closed_over_implications():
    assert permissions_for_adp_role("workspace_viewer") == {Permission.READ}
    assert permissions_for_adp_role("workspace_owner") == set(Permission)


def test_no_adp_role_name_is_reused_as_a_permission_value():
    """The two vocabularies are kept separate on purpose.

    If a role name were also a permission value, passing one where the other was
    expected would silently work — and stop working on a rename.
    """
    permission_values = {p.value for p in Permission}

    assert permission_values.isdisjoint(set(ADP_ROLE_PERMISSIONS))


def test_a_role_is_never_read_off_the_token(model):
    """Roles are resolved from stored grants, not from a token claim.

    This is not a style preference: no production-issuable ADP token carries a
    role claim today, which is why the existing ``require_role("org-admin")``
    endpoints are unreachable by any real token. A model that read a role from the
    token would therefore authorize nothing in production while passing tests that
    hand-craft the claim. So a role claim on the token has no effect.
    """
    policy = DomainTokenPolicy(allowed_client_ids=[ALLOWED_CLIENT], expected_issuer=ISSUER)
    caller = policy.admit(
        {
            "sub": ORG_MATE,
            "iss": ISSUER,
            "client_id": ALLOWED_CLIENT,
            "token_use": "access",
            "custom:org_id": ORG,
            "custom:role": "workspace_owner",
            "custom:account_type": "human",
        },
        validation_path=TRUSTED_VALIDATION_PATH,
    )

    assert not hasattr(caller, "role")
    with pytest.raises(AuthorizationDeniedError):
        model.authorize(caller, WORKSPACE, Permission.ADMINISTER, workspace_org_id=ORG)


def test_an_operation_authorization_for_the_wrong_permission_is_refused(model):
    """The server-held record must cover the operation being attempted."""
    with pytest.raises(AuthorizationDeniedError):
        model.authorize_operation(
            principal(OWNER),
            authorization(OWNER, Permission.READ),
            WORKSPACE,
            Permission.PROVISION,
            workspace_org_id=ORG,
        )
