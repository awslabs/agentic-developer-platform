"""Shared Cognito role-claim sync, used by every path that changes a user's role.

Extracted verbatim from ``admin/onboarding/approval.py`` (issue #4019) so the
admin role-update endpoint can reuse it instead of re-implementing the
``AdminUpdateUserAttributes`` call. Re-implementing is a trap: the username
fallback below is invisible in a mocked unit test and fails for every
GitHub-login user in a real deployment (see :func:`sync_cognito_role_claims`).

Scope note: these attributes are a *claims cache*, not authority. Org-level
authority lives in ``tenant_memberships.role`` (#3987/#3998) and platform
authority in the token's ``is_admin`` claim (#3981). Writing here changes what a
freshly-minted token *says*; it grants nothing on its own.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def cognito_user_pool_id() -> str:
    """Resolve the Cognito user pool id from either env-var spelling.

    Matches admin/cognito_service.py + onboarding/handler.py: the configmap sets
    BG_COGNITO_USER_POOL_ID; some deployments also export the bare name.
    """
    return os.environ.get("BG_COGNITO_USER_POOL_ID") or os.environ.get("COGNITO_USER_POOL_ID", "")


def emit_metric(namespace: str, metric_name: str, value: float = 1.0) -> None:
    """Best-effort CloudWatch metric emission."""
    try:
        import boto3

        client = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        client.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:
        logger.exception("Failed to emit CloudWatch metric %s/%s", namespace, metric_name)


def resolve_username_by_sub(client, pool_id: str, cognito_sub: str) -> str:
    """Resolve a Cognito user's username from its `sub` via an exact-match filter.

    Returns the username, or "" if no user matches. Cognito enforces sub
    uniqueness, so the exact `sub = "<sub>"` filter cannot alias another user.
    """
    resp = client.list_users(
        UserPoolId=pool_id,
        Filter=f'sub = "{cognito_sub}"',
        Limit=1,
    )
    users = resp.get("Users", [])
    if not users:
        return ""
    return users[0].get("Username", "")


def sync_cognito_role_claims(
    *,
    cognito_sub: str,
    org_id: str,
    role: str,
    team_id: str,
    department_id: str = "",
    metric_namespace: str = "ADP/Onboarding",
    metric_prefix: str = "OnboardingApproval",
    only_if_current_org: bool = False,
    previous_role: str | None = None,
    workspace_roles: dict[str, str] | None = None,
) -> bool:
    """Write role/org/team onto the Cognito user's custom: attributes.

    The pre-token-generation Lambda copies custom:role / custom:org_id /
    custom:team_id / custom:department_id from the Cognito USER ATTRIBUTES into
    the access token — it does NOT read Postgres. So approving a request (which
    only writes the DB rows) leaves the user's token with an empty role/org →
    the SPA hides nav items and the dashboard errors ("undefined reading map").
    Setting the attributes here makes a fresh login mint a correct token.

    Best-effort + idempotent: logs + emits a metric on failure rather than
    rolling back the (already-committed) caller transaction. Returns false when
    synchronization could not complete, and true on success or an intentional
    current-workspace no-op. Callers can record reconciliation separately.

    Username handling: AdminUpdateUserAttributes takes a *Username*, which is
    only equal to the user's `sub` for email-signup users (Cognito assigns them
    a UUID username that happens to match). GitHub-broker-provisioned users get
    username `GitHub_<github_id>` (see lambda/github-auth-broker/
    cognito_provisioner.py), so an update keyed on the sub throws
    UserNotFoundException. We therefore try the sub first (correct + zero extra
    calls for email-signup users) and, on UserNotFoundException, resolve the
    real username by an exact `sub` filter (Cognito enforces sub uniqueness, so
    this cannot alias another user) and retry once.

    Args:
        cognito_sub: The target user's Cognito ``sub``.
        org_id: Value for ``custom:org_id``.
        role: Value for ``custom:role``.
        team_id: Value for ``custom:team_id``.
        department_id: Optional value for ``custom:department_id``.
        only_if_current_org: For role edits, update only the role in the selected
            org. Do not replace workspace claims from a different membership.
            Callers serialize this against workspace selection using the login
            row lock.
        previous_role: Display role before this edit, for explicit global-role changes.
        workspace_roles: Current membership roles by org, used on global demotion.
        metric_namespace: CloudWatch namespace for failure/skip metrics.
        metric_prefix: Metric-name prefix identifying the calling flow, so an
            operator can tell an onboarding sync failure from a role-update one.
    """
    pool_id = cognito_user_pool_id()
    if not pool_id:
        logger.warning("Cannot sync Cognito role claims: no BG_COGNITO_USER_POOL_ID / COGNITO_USER_POOL_ID set")
        emit_metric(metric_namespace, f"{metric_prefix}.CognitoClaimSyncSkipped")
        return False
    attrs = [
        {"Name": "custom:role", "Value": role},
        {"Name": "custom:org_id", "Value": org_id},
        {"Name": "custom:team_id", "Value": team_id},
    ]
    if department_id:
        attrs.append({"Name": "custom:department_id", "Value": department_id})
    try:
        import boto3

        client = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        if only_if_current_org:
            users = client.list_users(UserPoolId=pool_id, Filter=f'sub = "{cognito_sub}"', Limit=1).get("Users", [])
            if not users:
                raise RuntimeError("Cognito login not found for role synchronization")
            current = {item["Name"]: item["Value"] for item in users[0].get("Attributes", [])}
            if current.get("sub") != cognito_sub:
                raise RuntimeError("Cognito login does not match the role target")
            platform_roles = {"platform_admin", "admin"}
            current_org = current.get("custom:org_id", "")
            if role in platform_roles:
                # A platform-admin grant applies across all workspaces.
                effective_role = role
            elif previous_role in platform_roles:
                # Global demotion must clear the platform claim even if the
                # edited account is in a different org. Restore the SELECTED
                # org's membership role, not the edited org's role.
                effective_role = (workspace_roles or {}).get(current_org, "member")
            elif current.get("custom:role") in platform_roles:
                # Editing an org-local member role cannot revoke global admin.
                return True
            elif current_org != org_id:
                return True
            else:
                effective_role = role
            client.admin_update_user_attributes(
                UserPoolId=pool_id, Username=users[0]["Username"], UserAttributes=[{"Name": "custom:role", "Value": effective_role}]
            )
            return True
        try:
            client.admin_update_user_attributes(
                UserPoolId=pool_id,
                Username=cognito_sub,
                UserAttributes=attrs,
            )
            logger.info("Synced Cognito role claims for sub=%s (role=%s org=%s)", cognito_sub, role, org_id)
            return True
        except Exception as e:
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if error_code != "UserNotFoundException":
                raise
            # sub != username (e.g. GitHub-federated user `GitHub_<id>`): resolve
            # the real username by an exact sub filter and retry once.
            username = resolve_username_by_sub(client, pool_id, cognito_sub)
            if not username:
                logger.error(
                    "Failed to sync Cognito role claims: no Cognito user found for sub=%s "
                    "(AdminUpdateUserAttributes by sub raised UserNotFoundException and "
                    "ListUsers sub-filter returned no match)",
                    cognito_sub,
                )
                emit_metric(metric_namespace, f"{metric_prefix}.CognitoClaimSyncFailure")
                return False
            client.admin_update_user_attributes(
                UserPoolId=pool_id,
                Username=username,
                UserAttributes=attrs,
            )
            logger.info(
                "Synced Cognito role claims for sub=%s via resolved username=%s (role=%s org=%s)",
                cognito_sub,
                username,
                role,
                org_id,
            )
    except Exception:
        logger.exception("Failed to sync Cognito role claims for sub=%s", cognito_sub)
        emit_metric(metric_namespace, f"{metric_prefix}.CognitoClaimSyncFailure")

        return False
    return True
