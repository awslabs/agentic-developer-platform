"""
Pre Token Generation Lambda Trigger (V2) for AWS Cognito.

This Lambda function injects custom claims into access tokens for both:
1. Human users (TokenGeneration_Authentication): Copy custom attributes to access token
2. Agents/Services (TokenGeneration_ClientCredentials): Inject minimal service claims

Issue #119: Unified Cognito JWT Auth
Issue #377: Decommissioned agent-clients DynamoDB table. Agent metadata is now
resolved via the organizations identity-index in the gateway admin API. The
Pre Token Generation Lambda no longer performs DynamoDB lookups for client
credentials tokens — it injects a minimal "service" account_type claim, and
the gateway backend resolves full org context from the identity-index at
request time.

GitHub Sign-In Decision Logic (Issue #309):
-------------------------------------------
When a user signs in via GitHub (federated identity provider), Cognito fires
TokenGeneration_Authentication with the same event shape as email/password users.
The key difference: GitHub-federated users may not have custom:org_id set yet
(it's assigned by an admin after first sign-up). In that case, the claims dict
will be empty for org_id/team_id/department_id — the backend treats this as an
"unassigned" user with limited access until an admin assigns them to an org.

The Pre-Sign-Up Lambda (separate function) runs BEFORE this one and handles:
- Allowlist enforcement (org membership, explicit username list, or open mode)
- Auto-confirming the user (external providers skip email verification)
- Linking external identity attributes (GitHub username -> custom:github_username)

This Lambda does NOT need to differentiate between auth methods — it simply
copies whatever custom attributes exist on the user record into the access token.
Both email/password and GitHub users flow through handle_user_token_generation().
"""

import json
import logging
import os

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


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
            # M2M client credentials - inject service account claims
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

    Issue #377: The agent-clients DynamoDB table has been decommissioned.
    This function now injects minimal service claims. Full org/team context
    is resolved by the gateway backend at request time via the identity-index.

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

    # Inject minimal service claims — the gateway resolves full org context
    # from the identity-index at request time using the client_id
    claims_to_add = {
        "custom:account_type": "service",
        "custom:client_id": client_id,
    }

    # Set the claims override in the response
    event["response"] = event.get("response", {})
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {
            "claimsToAddOrOverride": claims_to_add,
        }
    }

    logger.info(f"Added service claims to access token: {list(claims_to_add.keys())}")
    return event
