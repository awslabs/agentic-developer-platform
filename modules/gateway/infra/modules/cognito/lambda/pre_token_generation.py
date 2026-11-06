"""Cognito V3 pre-token trigger with protected human and registry-bound service claims."""

import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
AGENT_CLIENTS_TABLE = os.environ.get("AGENT_CLIENTS_TABLE", "agent_clients")

HUMAN_AUTHORITY_ATTRIBUTES = (
    "custom:org_id",
    "custom:team_id",
    "custom:department_id",
    "custom:role",
)


def handler(event: dict, context) -> dict:
    trigger_source = event.get("triggerSource", "")
    try:
        if trigger_source in (
            "TokenGeneration_Authentication",
            "TokenGeneration_HostedAuth",
            "TokenGeneration_RefreshTokens",
            "TokenGeneration_NewPasswordChallenge",
        ):
            return handle_user_token_generation(event)
        if trigger_source == "TokenGeneration_ClientCredentials":
            return handle_client_credentials_token_generation(event)
        logger.warning("Unknown trigger source: %s", trigger_source)
        return event
    except Exception:
        logger.error("Pre-token claim authority unavailable")
        raise


def _protected_user_attributes(event: dict) -> dict[str, str]:
    pool_id = event.get("userPoolId")
    username = event.get("userName")
    request = event.get("request")
    event_attributes = request.get("userAttributes") if isinstance(request, dict) else None
    subject = event_attributes.get("sub") if isinstance(event_attributes, dict) else None
    if not all(isinstance(value, str) and value for value in (pool_id, username, subject)):
        raise ValueError("Cognito identity is incomplete")

    user = boto3.client("cognito-idp").admin_get_user(
        UserPoolId=pool_id, Username=username
    )
    if not isinstance(user, dict) or user.get("Username") != username or user.get("Enabled") is False:
        raise ValueError("Cognito user identity does not match")
    attributes = user.get("UserAttributes")
    if not isinstance(attributes, list):
        raise ValueError("Cognito user attributes unavailable")
    protected = {}
    for attribute in attributes:
        if (
            not isinstance(attribute, dict)
            or not isinstance(attribute.get("Name"), str)
            or not isinstance(attribute.get("Value"), str)
            or attribute["Name"] in protected
        ):
            raise ValueError("Cognito user attributes invalid")
        protected[attribute["Name"]] = attribute["Value"]
    if protected.get("sub") != subject:
        raise ValueError("Cognito subject does not match")
    return protected


def handle_user_token_generation(event: dict) -> dict:
    protected = _protected_user_attributes(event)
    organization = protected.get("custom:org_id")
    claims = {
        attribute: protected[attribute]
        for attribute in HUMAN_AUTHORITY_ATTRIBUTES
        if organization and protected.get(attribute)
    }
    claims["custom:account_type"] = "human"
    suppressed = [
        attribute for attribute in HUMAN_AUTHORITY_ATTRIBUTES if attribute not in claims
    ]
    event["response"] = event.get("response") or {}
    event["response"]["claimsAndScopeOverrideDetails"] = {
        token: {"claimsToAddOrOverride": dict(claims), "claimsToSuppress": list(suppressed)}
        for token in ("idTokenGeneration", "accessTokenGeneration")
    }
    return event


def handle_client_credentials_token_generation(event: dict) -> dict:
    """
    Handle token generation for client credentials (M2M) flow.

    Looks up the agent/service client in DynamoDB to get org/team context.

    Args:
        event: Cognito trigger event

    Returns:
        Modified event with custom claims
    """
    caller_context = event.get("callerContext", {})
    client_id = caller_context.get("clientId", "")

    if not client_id:
        logger.warning("No client_id found in caller context")
        return event

    logger.info(f"Client credentials token generation for client_id={client_id}")

    # Look up agent metadata from DynamoDB
    agent_metadata = get_agent_metadata(client_id)

    if not agent_metadata:
        logger.warning(f"No agent metadata found for client_id={client_id}")
        # Return with minimal claims for unregistered clients
        event["response"] = event.get("response", {})
        event["response"]["claimsAndScopeOverrideDetails"] = {
            "accessTokenGeneration": {
                "claimsToAddOrOverride": {
                    "custom:account_type": "service",
                    "custom:client_id": client_id,
                }
            }
        }
        return event

    # Build claims from agent metadata
    claims_to_add = {
        "custom:org_id": agent_metadata.get("org_id", ""),
        "custom:team_id": agent_metadata.get("team_id", ""),
        "custom:department_id": agent_metadata.get("department_id", ""),
        "custom:agent_name": agent_metadata.get("agent_name", ""),
        "custom:account_type": "service",
        "custom:client_id": client_id,
    }

    # Remove empty claims
    claims_to_add = {k: v for k, v in claims_to_add.items() if v}

    # Set the claims override in the response
    event["response"] = event.get("response", {})
    event["response"]["claimsAndScopeOverrideDetails"] = {"accessTokenGeneration": {"claimsToAddOrOverride": claims_to_add}}

    logger.info(f"Added agent claims to access token: {list(claims_to_add.keys())}")
    return event


def get_agent_metadata(client_id: str) -> dict | None:
    """
    Look up agent metadata from DynamoDB.

    Args:
        client_id: Cognito App Client ID

    Returns:
        Agent metadata dict or None if not found
    """
    try:
        table = dynamodb.Table(AGENT_CLIENTS_TABLE)
        response = table.get_item(Key={"client_id": client_id})
        return response.get("Item")

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceNotFoundException":
            logger.warning(f"Agent clients table not found: {AGENT_CLIENTS_TABLE}")
        else:
            logger.error(f"DynamoDB error looking up agent: {e}")
        return None
    except Exception as e:
        logger.error(f"Error looking up agent metadata: {e}")
        return None
