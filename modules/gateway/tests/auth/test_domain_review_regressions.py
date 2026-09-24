"""Review regressions at the composed authorization boundary."""

import pytest
from superplane_auth.policy import (
    TRUSTED_VALIDATION_PATH,
    DomainTokenPolicy,
    OperationAuthorization,
    Permission,
    TokenRejectedError,
    WorkspaceAuthorizationModel,
    WorkspaceGrant,
    authorize_request,
)


def request(headers=None, **overrides):
    policy = DomainTokenPolicy(["client"], "issuer")
    model = WorkspaceAuthorizationModel()
    model.record_grant(WorkspaceGrant("w1", "org", "user", frozenset({Permission.READ})))
    args = dict(
        claims={"sub": "user", "custom:org_id": "org", "client_id": "client", "iss": "issuer", "token_use": "access", "custom:account_type": "human"},
        validation_path=TRUSTED_VALIDATION_PATH,
        method="GET",
        path_template="/superplane/v1/workspaces/{workspace}",
        workspace_id="w1",
        workspace_org_id="org",
        authorization=OperationAuthorization("op", "w1", "user", Permission.READ),
        headers=headers,
    )
    args.update(overrides)
    return authorize_request(policy, model, **args)


def test_composed_authorization_returns_only_sanitized_headers():
    hostile = {
        "X-Org-Id": "victim",
        "X-Agent-OrgId": "victim",
        "X-Auth-Source": "iam",
        "X-Amzn-Iam-User-Arn": "forged",
        "X-Forwarded-User": "admin",
        "X-Adp-User": "admin",
        "Content-Type": "application/json",
    }
    _, _, safe = request(hostile)
    assert safe == {"content-type": "application/json"}
    assert hostile["X-Org-Id"] == "victim"


@pytest.mark.parametrize("kind", [None, "", "robot", "service-account"])
def test_account_type_must_be_explicit_and_recognized(kind):
    claims = {"sub": "user", "custom:org_id": "org", "client_id": "client", "iss": "issuer", "token_use": "access", "custom:account_type": kind}
    with pytest.raises(TokenRejectedError):
        request(claims=claims)


# `/events` and `/orgs/cost` join this list because they are org collections, not
# the workspace-filtered queries the policy once assumed (#5637). `/providers` is
# gone: it was never a served route. `/vault/credentials` is the real one.
@pytest.mark.parametrize(
    "path",
    [
        "/superplane/v1/workspaces",
        "/superplane/v1/accounts",
        "/superplane/v1/vault/credentials",
        "/superplane/v1/events",
        "/superplane/v1/orgs/cost",
    ],
)
def test_workspace_grant_cannot_authorize_org_collections(path):
    from superplane_auth.policy import AuthorizationDeniedError

    with pytest.raises(AuthorizationDeniedError, match="org-scoped"):
        request(path_template=path)


def test_a_workspace_scoped_read_still_has_a_positive_path():
    """The negative cases above must not be the only outcome the policy can reach.

    Per-workspace cost is the positive counterpart: it names a workspace in the
    path, so a workspace grant does authorize it. `/events` used to serve this
    role, on the assumption it was a workspace-filtered query; it is an org
    collection, so it now belongs in the refusal list and cannot demonstrate a
    success.
    """
    principal, grant, headers = request(path_template="/superplane/v1/workspaces/{workspace}/cost")
    assert principal.subject == grant.principal == "user"
    assert headers == {}
