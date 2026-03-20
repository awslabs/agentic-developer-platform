"""
Pre Token Generation Lambda Trigger (V2) for AWS Cognito.

This Lambda function injects custom claims into access tokens for both:
1. Human users (TokenGeneration_Authentication): Copy custom attributes to access token
2. Agents/Services (TokenGeneration_ClientCredentials): Look up org/team from DynamoDB

Issue #119: Unified Cognito JWT Auth
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize DynamoDB client
dynamodb = boto3.resource("dynamodb")
AGENT_CLIENTS_TABLE = os.environ.get("AGENT_CLIENTS_TABLE", "agent_clients")


def handler(event: dict, context) -> dict:
    """
    Pre Token Generation Lambda handler (V2 trigger).

    This function is called before Cognito issues tokens and allows us to
    customize the claims in the access token.

    Args:
        event: Cognito trigger event containing user/client information
        context: Lambda context

    Returns:
        Modified event with custom claims added
    """
    logger.info(f"Pre Token Generation trigger: {event.get('triggerSource')}")
    logger.debug(f"Full event: {json.dumps(event, default=str)}")

    trigger_source = event.get("triggerSource", "")

    try:
        if trigger_source == "TokenGeneration_Authentication":
            # Human user authentication - copy custom attributes to access token
            return handle_user_token_generation(event)
        elif trigger_source == "TokenGeneration_ClientCredentials":
            # M2M client credentials - look up agent metadata
            return handle_client_credentials_token_generation(event)
        elif trigger_source == "TokenGeneration_RefreshTokens":
            # Token refresh - same as authentication
            return handle_user_token_generation(event)
        else:
            logger.warning(f"Unknown trigger source: {trigger_source}")
            return event

    except Exception as e:
        logger.error(f"Error in pre token generation: {e}")
        # Return unmodified event on error to not block authentication
        return event


def handle_user_token_generation(event: dict) -> dict:
    """
    Handle token generation for human user authentication.

    Copies custom attributes from user attributes to access token claims.

    Args:
        event: Cognito trigger event

    Returns:
        Modified event with custom claims
    """
    user_attributes = event.get("request", {}).get("userAttributes", {})

    # Extract custom attributes
    org_id = user_attributes.get("custom:org_id", "")
    team_id = user_attributes.get("custom:team_id", "")
    department_id = user_attributes.get("custom:department_id", "")
    role = user_attributes.get("custom:role", "")

    logger.info(f"User token generation for org_id={org_id}, team_id={team_id}")

    # Build claims to add to access token
    claims_to_add = {
        "custom:org_id": org_id,
        "custom:team_id": team_id,
        "custom:department_id": department_id,
        "custom:role": role,
        "custom:account_type": "human",
    }

    # Remove empty claims
    claims_to_add = {k: v for k, v in claims_to_add.items() if v}

    # Set the claims override in the response
    event["response"] = event.get("response", {})
    event["response"]["claimsAndScopeOverrideDetails"] = {"accessTokenGeneration": {"claimsToAddOrOverride": claims_to_add}}

    logger.info(f"Added claims to access token: {list(claims_to_add.keys())}")
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
