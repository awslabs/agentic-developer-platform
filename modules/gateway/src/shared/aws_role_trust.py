"""Target restrictions shared by personal connection verification and delivery."""

import os
import re


def validate_customer_role(role_arn):
    from src.admin.agent_registry_schemas import is_reserved_role_arn

    match = re.fullmatch(r"arn:(aws(?:-[a-z]+)*):iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})", role_arn or "")
    gateway = os.environ.get("ADP_GATEWAY_ROLE_ARN", "")
    gateway_match = re.fullmatch(r"arn:aws(?:-[a-z]+)*:iam::([0-9]{12}):role/.+", gateway)
    explicit = os.environ.get("ADP_GATEWAY_ACCOUNT_ID", "")
    accounts = {value for value in (explicit, gateway_match[1] if gateway_match else "") if value}
    if not accounts or any(not re.fullmatch(r"[0-9]{12}", value) or value == "000000000000" for value in accounts):
        raise ValueError("Gateway account identity is not configured")
    from src.shared.config import get_settings

    platform_bedrock = get_settings().platform_bedrock_account_id
    if platform_bedrock:
        accounts.add(platform_bedrock)
    if not match or match[2] in accounts:
        raise ValueError("Role is not an allowed customer role")
    # ADP-Agent-* is the customer Quick-Create namespace. Platform accounts
    # are denied above, including roles with this name or an IAM path.
    if is_reserved_role_arn(role_arn) and not match[3].rsplit("/", 1)[-1].lower().startswith("adp-agent-"):
        raise ValueError("Role is not an allowed customer role")
