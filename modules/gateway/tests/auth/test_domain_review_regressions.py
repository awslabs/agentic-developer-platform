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


@pytest.mark.parametrize("path", ["/superplane/v1/workspaces", "/superplane/v1/accounts", "/superplane/v1/providers"])
def test_workspace_grant_cannot_authorize_org_collections(path):
    from superplane_auth.policy import AuthorizationDeniedError

    with pytest.raises(AuthorizationDeniedError, match="org-scoped"):
        request(path_template=path)


def test_query_scoped_workspace_read_still_has_a_positive_path():
    principal, grant, headers = request(path_template="/superplane/v1/events")
    assert principal.subject == grant.principal == "user"
    assert headers == {}
