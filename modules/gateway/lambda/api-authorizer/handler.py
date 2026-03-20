# =============================================================================
# API Gateway Lambda Authorizer (Issue #239)
# =============================================================================
# Authenticates requests to API Gateway using either:
# 1. JWT tokens (validated against Cognito JWKS)
# 2. IAM credentials (mapped to agents via DynamoDB registry)
#
# Sets headers for downstream processing:
# - X-Auth-Source: jwt | iam
# - X-Agent-Id: agent name (for IAM) or sub (for JWT)
# - X-Agent-OrgId: organization ID
# - X-Agent-TeamId: team ID
# - X-Agent-UserId: owner/sub
# - X-Agent-AccountType: service | user
# - X-Agent-Scope: shared | personal
# - X-Agent-BudgetConfigId: budget config ID
# - X-Agent-AllowedModels: comma-separated list of allowed models
# =============================================================================

import json
import logging
import os
import re
from typing import Any
from urllib.error import URLError

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION", "us-east-1")
AGENT_REGISTRY_TABLE = os.environ.get("AGENT_REGISTRY_TABLE", "")
AUTHORIZER_CACHE_TTL = int(os.environ.get("AUTHORIZER_CACHE_TTL", "300"))

# DynamoDB client
_dynamodb_client = None


def get_dynamodb_client() -> "boto3.client":
    """Get or create DynamoDB client."""
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def validate_jwt(token: str) -> dict[str, Any] | None:
    """
    Validate JWT token against Cognito JWKS.

    Returns decoded claims if valid, None otherwise.
    """
    try:
        # Import jwt here to allow tests to mock it
        import jwt
        from jwt import PyJWKClient
        from jwt.exceptions import PyJWTError

        jwks_url = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"

        # Use PyJWKClient for automatic key management
        jwks_client = PyJWKClient(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        # Decode and validate the token
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=None,  # Cognito doesn't set aud for access tokens
            options={
                "verify_aud": False,
                "require": ["sub", "iss", "exp", "iat"],
            },
        )

        # Verify issuer is our Cognito User Pool
        expected_issuer = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
        if claims.get("iss") != expected_issuer:
            logger.warning(f"Invalid issuer: {claims.get('iss')}")
            return None

        logger.info(f"JWT validated for sub: {claims.get('sub')}")
        return claims

    except PyJWTError as e:
        logger.error(f"JWT validation failed: {e}")
        return None
    except (URLError, TimeoutError) as e:
        logger.error(f"Failed to fetch JWKS: {e}")
        return None


def parse_role_arn_from_user_arn(user_arn: str) -> str | None:
    """
    Parse IAM role ARN from the userArn in the request context.

    Handles assumed-role format (with optional path):
    arn:aws:sts::ACCOUNT:assumed-role/ROLE_NAME/SESSION_NAME
    -> arn:aws:iam::ACCOUNT:role/ROLE_NAME

    arn:aws:sts::ACCOUNT:assumed-role/path/to/my-role/SESSION_NAME
    -> arn:aws:iam::ACCOUNT:role/path/to/my-role

    Also handles direct role ARNs:
    arn:aws:iam::ACCOUNT:role/ROLE_NAME
    arn:aws:iam::ACCOUNT:role/path/to/ROLE_NAME
    """
    if not user_arn:
        return None

    # Pattern for assumed-role format: extract account and full path after assumed-role/
    assumed_role_pattern = r"arn:aws:sts::(\d+):assumed-role/(.+)"
    match = re.match(assumed_role_pattern, user_arn)
    if match:
        account_id = match.group(1)
        # The path contains role_path/role_name/session_name
        # We need to strip the last component (session_name)
        path_parts = match.group(2).rsplit("/", 1)
        if len(path_parts) >= 1:
            role_path = path_parts[0]
            return f"arn:aws:iam::{account_id}:role/{role_path}"

    # Pattern for direct role ARN
    role_pattern = r"arn:aws:iam::(\d+):role/(.+)"
    match = re.match(role_pattern, user_arn)
    if match:
        return user_arn

    logger.warning(f"Could not parse role ARN from: {user_arn}")
    return None


def lookup_agent_in_registry(role_arn: str) -> dict[str, Any] | None:
    """
    Look up agent configuration in DynamoDB registry by role_arn.

    Issue #248: Now uses the by-role-arn GSI instead of direct PK lookup.
    The primary key is agent_id (UUID), with role_arn as a GSI.

    Returns agent config if found and active, None otherwise.
    """
    if not AGENT_REGISTRY_TABLE:
        logger.error("AGENT_REGISTRY_TABLE not configured")
        return None

    try:
        client = get_dynamodb_client()
        # Query the by-role-arn GSI instead of direct get_item
        response = client.query(
            TableName=AGENT_REGISTRY_TABLE,
            IndexName="by-role-arn",
            KeyConditionExpression="role_arn = :role_arn",
            ExpressionAttributeValues={":role_arn": {"S": role_arn}},
            Limit=1,
        )

        items = response.get("Items", [])
        if not items:
            logger.warning(f"Agent not found in registry: {role_arn}")
            return None

        item = items[0]

        # Check if agent is active
        status = item.get("status", {}).get("S", "disabled")
        if status != "active":
            logger.warning(f"Agent is disabled: {role_arn}")
            return None

        # Parse the DynamoDB item into a dict
        # Issue #248: Now includes agent_id as the primary identity
        agent = {
            "agent_id": item.get("agent_id", {}).get("S", ""),
            "agent_name": item.get("agent_name", {}).get("S", ""),
            "org_id": item.get("org_id", {}).get("S", ""),
            "team_id": item.get("team_id", {}).get("S", ""),
            "owner": item.get("owner", {}).get("S", ""),
            "scope": item.get("scope", {}).get("S", "personal"),
            "budget_config_id": item.get("budget_config_id", {}).get("S", ""),
            "allowed_models": item.get("allowed_models", {}).get("SS", []),
            "status": status,
        }

        logger.info(f"Agent found: {agent['agent_id']} ({agent['agent_name']}) (org: {agent['org_id']})")
        return agent

    except ClientError as e:
        logger.error(f"DynamoDB lookup failed: {e}")
        return None


def generate_policy(
    principal_id: str,
    effect: str,
    resource: str,
    context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Generate IAM policy document for API Gateway authorizer response.

    Args:
        principal_id: Identifier for the principal (user/agent)
        effect: Allow or Deny
        resource: API Gateway method ARN
        context: Additional context to pass to downstream
    """
    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
    }

    if context:
        policy["context"] = context

    return policy


def extract_bearer_token(auth_header: str | None) -> str | None:
    """Extract Bearer token from Authorization header."""
    if not auth_header:
        return None

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    return parts[1]


def _redact_sensitive_event(event: dict[str, Any]) -> dict[str, Any]:
    """Create a redacted copy of the event for safe logging."""
    redacted = event.copy()

    # Redact headers
    if "headers" in redacted and redacted["headers"]:
        redacted["headers"] = {
            k: "[REDACTED]" if k.lower() in ("authorization", "x-api-key", "cookie") else v for k, v in redacted["headers"].items()
        }

    # Redact authorizationToken if present (TOKEN authorizer format)
    if "authorizationToken" in redacted:
        redacted["authorizationToken"] = "[REDACTED]"

    return redacted


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda handler for API Gateway authorizer.

    Authenticates requests using either JWT tokens or IAM credentials.
    """
    logger.info(f"Authorizer invoked: {json.dumps(_redact_sensitive_event(event), default=str)}")

    # Get the method ARN for the policy
    method_arn = event.get("methodArn", "*")

    # Try to extract Authorization header
    headers = event.get("headers", {}) or {}
    # Handle both lowercase and mixed case header names
    auth_header = headers.get("Authorization") or headers.get("authorization")

    # Check for Bearer token first
    token = extract_bearer_token(auth_header)
    if token:
        # JWT Authentication
        claims = validate_jwt(token)
        if claims:
            # Build context from JWT claims
            context_map = {
                "X-Auth-Source": "jwt",
                "X-Agent-Id": claims.get("sub", ""),
                "X-Agent-OrgId": claims.get("custom:org_id", "default"),
                "X-Agent-TeamId": claims.get("custom:team_id", ""),
                "X-Agent-UserId": claims.get("sub", ""),
                "X-Agent-AccountType": claims.get("custom:account_type", "user"),
                "X-Agent-Scope": claims.get("custom:scope", "personal"),
                "X-Agent-BudgetConfigId": claims.get("custom:budget_config_id", ""),
                "X-Agent-AllowedModels": "",  # JWT users typically have no model restrictions
            }
            return generate_policy(
                principal_id=claims.get("sub", "jwt-user"),
                effect="Allow",
                resource=method_arn,
                context=context_map,
            )
        else:
            # Invalid JWT - deny
            logger.warning("JWT validation failed - denying request")
            return generate_policy(
                principal_id="unauthorized",
                effect="Deny",
                resource=method_arn,
            )

    # No Bearer token - try IAM authentication
    request_context = event.get("requestContext", {})
    identity = request_context.get("identity", {})
    user_arn = identity.get("userArn")

    if not user_arn:
        logger.warning("No authentication credentials found")
        return generate_policy(
            principal_id="unauthorized",
            effect="Deny",
            resource=method_arn,
        )

    # Parse role ARN from user ARN
    role_arn = parse_role_arn_from_user_arn(user_arn)
    if not role_arn:
        logger.warning(f"Could not parse role ARN from: {user_arn}")
        return generate_policy(
            principal_id="unauthorized",
            effect="Deny",
            resource=method_arn,
        )

    # Look up agent in registry
    agent = lookup_agent_in_registry(role_arn)
    if not agent:
        logger.warning(f"Agent not found or disabled: {role_arn}")
        return generate_policy(
            principal_id="unauthorized",
            effect="Deny",
            resource=method_arn,
        )

    # IAM Authentication successful
    # Issue #248: Use agent_id (UUID) as primary identity
    allowed_models = ",".join(agent.get("allowed_models", []))
    agent_id = agent.get("agent_id", "")
    context_map = {
        "X-Auth-Source": "iam",
        "X-Agent-Id": agent_id,  # Issue #248: Now uses UUID agent_id
        "X-Agent-Name": agent.get("agent_name", ""),  # Issue #248: agent_name as separate header
        "X-Agent-OrgId": agent.get("org_id", "default"),
        "X-Agent-TeamId": agent.get("team_id", ""),
        "X-Agent-UserId": agent.get("owner", ""),
        "X-Agent-AccountType": "service",
        "X-Agent-Scope": agent.get("scope", "shared"),
        "X-Agent-BudgetConfigId": agent.get("budget_config_id", ""),
        "X-Agent-AllowedModels": allowed_models,
    }

    logger.info(f"IAM auth successful for agent: {agent_id} ({agent.get('agent_name')})")
    return generate_policy(
        principal_id=agent_id or agent.get("agent_name", role_arn),
        effect="Allow",
        resource=method_arn,
        context=context_map,
    )
