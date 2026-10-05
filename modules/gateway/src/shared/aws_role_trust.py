"""Target restrictions shared by personal connection verification and delivery."""

import os
import re


class CustomerRoleValidationError(ValueError):
    """Safe, actionable rejection details for the connection owner."""

    def __init__(self, reason: str, message: str, hint: str, *, status_code: int = 422):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.hint = hint
        self.status_code = status_code


def validate_customer_role(role_arn):
    from src.admin.agent_registry_schemas import is_reserved_role_arn

    match = re.fullmatch(r"arn:(aws(?:-[a-z]+)*):iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})", role_arn or "")
    gateway = os.environ.get("ADP_GATEWAY_ROLE_ARN", "")
    gateway_match = re.fullmatch(r"arn:aws(?:-[a-z]+)*:iam::([0-9]{12}):role/.+", gateway)
    explicit = os.environ.get("ADP_GATEWAY_ACCOUNT_ID", "")
    accounts = {value for value in (explicit, gateway_match[1] if gateway_match else "") if value}
    if not accounts or any(not re.fullmatch(r"[0-9]{12}", value) or value == "000000000000" for value in accounts):
        raise CustomerRoleValidationError(
            "platform_identity_unavailable",
            "ADP cannot check AWS connections because its platform account configuration is incomplete.",
            "Ask your ADP platform administrator to check the gateway AWS account configuration, then try again. "
            "Changing your AWS role permissions will not resolve this error.",
            status_code=503,
        )
    from src.shared.config import get_settings

    platform_bedrock = get_settings().platform_bedrock_account_id
    if platform_bedrock:
        accounts.add(platform_bedrock)
    if not match:
        raise CustomerRoleValidationError(
            "invalid_role_arn",
            "The supplied value is not an IAM role ARN.",
            "Copy the role ARN from AWS IAM, for example arn:aws:iam::123456789012:role/MyRole. "
            "Do not use an IAM user ARN or an STS assumed-role session ARN.",
        )
    if match[2] in accounts:
        raise CustomerRoleValidationError(
            "platform_account_not_allowed",
            "This AWS account is used by the ADP platform. Roles in this account cannot be added as personal connections "
            "to keep platform access separate from personal access.",
            "Connect a different AWS account. If your agents need resources in the platform account, ask your ADP "
            "platform administrator to arrange scoped access for the task. Changing the role name or IAM permissions "
            "will not make this account eligible for a personal connection.",
        )
    # ADP-Agent-* is the customer Quick-Create namespace. Platform accounts
    # are denied above, including roles with this name or an IAM path.
    if is_reserved_role_arn(role_arn) and not match[3].rsplit("/", 1)[-1].lower().startswith("adp-agent-"):
        raise CustomerRoleValidationError(
            "reserved_role_name",
            "This IAM role name uses a prefix reserved for ADP platform roles.",
            "Ask your AWS administrator for a dedicated customer role with a different name, such as MyTeamAgentRole, "
            "or use ADP's CloudFormation setup to create an ADP-Agent-* role in a separate AWS account. "
            "Changing the connection nickname or IAM role path does not change the existing role's name.",
        )
