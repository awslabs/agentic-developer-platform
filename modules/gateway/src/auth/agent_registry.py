"""
DynamoDB Agent Registry Service for IAM-authenticated agents.

Issue #260: Dual-Path API Gateway - NONE auth (humans) + AWS_IAM auth (agents)

When requests come through the /agent/* path with AWS_IAM authorization,
API Gateway populates the IAM identity in request headers. This module
looks up the agent in DynamoDB using the IAM role ARN and returns the
agent's configuration for budget/rate-limit enforcement.

Architecture:
    Agent (SigV4) -> API Gateway /agent/{proxy+} (AWS_IAM auth)
                  -> API Gateway populates X-Amzn-Iam-* headers
                  -> VPC Link -> ALB -> FastAPI
                  -> This module looks up agent by role ARN in DynamoDB
                  -> TokenContext built from agent registry entry
"""

import logging
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from src.shared.config import get_settings
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)


class AgentRegistryEntry(TypedDict):
    """DynamoDB agent registry entry structure."""

    agent_id: str
    role_arn: str
    agent_name: str
    org_id: str
    team_id: str
    owner: str
    scope: str
    budget_config_id: str
    allowed_models: list[str]
    status: str
    description: str
    image_uri: str
    code_repo: str
    workflow_name: str
    created_at: str
    updated_at: str


class AgentRegistryService:
    """
    Service for looking up agents in the DynamoDB registry.

    Uses the by-role-arn GSI to find agents by their IAM role ARN.
    Results are cached briefly to avoid repeated lookups.
    """

    # Maximum number of entries to keep in the cache
    CACHE_MAX_SIZE = 1000

    def __init__(self, table_name: str | None = None):
        """
        Initialize the agent registry service.

        Args:
            table_name: DynamoDB table name. If not provided, uses BG_AGENT_REGISTRY_TABLE.
        """
        settings = get_settings()
        self._table_name = table_name or settings.agent_registry_table
        self._dynamodb = None
        # Use OrderedDict for LRU-style eviction with size limit
        self._cache: OrderedDict[str, tuple[AgentRegistryEntry | None, datetime]] = OrderedDict()
        self._cache_ttl = timedelta(minutes=5)  # Cache entries for 5 minutes

    @property
    def dynamodb(self):
        """Lazy-load DynamoDB client."""
        if self._dynamodb is None:
            settings = get_settings()
            self._dynamodb = boto3.client("dynamodb", region_name=settings.aws_region)
        return self._dynamodb

    def _is_cache_valid(self, cache_time: datetime) -> bool:
        """Check if cached entry is still valid."""
        return datetime.now(UTC) - cache_time < self._cache_ttl

    def _cache_set(self, key: str, value: tuple[AgentRegistryEntry | None, datetime]) -> None:
        """Add an entry to the cache with LRU eviction if at capacity."""
        # If key exists, remove it first (will be re-added at the end)
        if key in self._cache:
            del self._cache[key]
        # Evict oldest entries if at capacity
        while len(self._cache) >= self.CACHE_MAX_SIZE:
            self._cache.popitem(last=False)  # Remove oldest (first) item
        self._cache[key] = value

    def _parse_dynamodb_item(self, item: dict) -> AgentRegistryEntry:
        """Parse DynamoDB item into AgentRegistryEntry."""
        return AgentRegistryEntry(
            agent_id=item.get("agent_id", {}).get("S", ""),
            role_arn=item.get("role_arn", {}).get("S", ""),
            agent_name=item.get("agent_name", {}).get("S", ""),
            org_id=item.get("org_id", {}).get("S", ""),
            team_id=item.get("team_id", {}).get("S", ""),
            owner=item.get("owner", {}).get("S", ""),
            scope=item.get("scope", {}).get("S", ""),
            budget_config_id=item.get("budget_config_id", {}).get("S", ""),
            allowed_models=item.get("allowed_models", {}).get("SS", []),
            status=item.get("status", {}).get("S", ""),
            description=item.get("description", {}).get("S", ""),
            image_uri=item.get("image_uri", {}).get("S", ""),
            code_repo=item.get("code_repo", {}).get("S", ""),
            workflow_name=item.get("workflow_name", {}).get("S", ""),
            created_at=item.get("created_at", {}).get("S", ""),
            updated_at=item.get("updated_at", {}).get("S", ""),
        )

    def get_agent_by_role_arn(self, role_arn: str) -> AgentRegistryEntry | None:
        """
        Look up an agent by their IAM role ARN.

        Uses the by-role-arn GSI to find the agent. Results are cached.

        Args:
            role_arn: The IAM role ARN (e.g., arn:aws:iam::123456789012:role/my-role)

        Returns:
            AgentRegistryEntry if found and active, None otherwise
        """
        if not self._table_name:
            logger.warning("Agent registry table not configured (BG_AGENT_REGISTRY_TABLE)")
            return None

        # Check cache
        if role_arn in self._cache:
            cached_entry, cache_time = self._cache[role_arn]
            if self._is_cache_valid(cache_time):
                logger.debug(f"Cache hit for role_arn: {role_arn}")
                return cached_entry

        try:
            logger.debug(f"Looking up agent by role_arn: {role_arn}")
            response = self.dynamodb.query(
                TableName=self._table_name,
                IndexName="by-role-arn",
                KeyConditionExpression="role_arn = :arn",
                ExpressionAttributeValues={":arn": {"S": role_arn}},
                Limit=1,
            )

            items = response.get("Items", [])
            if not items:
                logger.warning(f"Agent not found for role_arn: {role_arn}")
                self._cache_set(role_arn, (None, datetime.now(UTC)))
                return None

            entry = self._parse_dynamodb_item(items[0])

            # Check if agent is active
            if entry["status"] != "active":
                logger.warning(f"Agent {entry['agent_name']} is not active (status: {entry['status']})")
                self._cache_set(role_arn, (None, datetime.now(UTC)))
                return None

            logger.info(f"Found agent: {entry['agent_name']} (org: {entry['org_id']}, team: {entry['team_id']})")
            self._cache_set(role_arn, (entry, datetime.now(UTC)))
            return entry

        except ClientError as e:
            logger.error(f"DynamoDB error looking up agent: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error looking up agent: {e}")
            return None

    def clear_cache(self):
        """Clear the agent cache."""
        self._cache.clear()


# Module-level singleton
_agent_registry_service: AgentRegistryService | None = None


def get_agent_registry_service() -> AgentRegistryService:
    """Get the singleton agent registry service."""
    global _agent_registry_service
    if _agent_registry_service is None:
        _agent_registry_service = AgentRegistryService()
    return _agent_registry_service


def parse_assumed_role_arn(user_arn: str) -> str | None:
    """
    Parse an assumed-role ARN to extract the role ARN.

    API Gateway AWS_IAM auth returns the caller identity in assumed-role format:
        arn:aws:sts::ACCOUNT:assumed-role/ROLE_NAME/SESSION_NAME

    We need to convert this to the IAM role ARN format for DynamoDB lookup:
        arn:aws:iam::ACCOUNT:role/ROLE_NAME

    Args:
        user_arn: The userArn from API Gateway (assumed-role format)

    Returns:
        The IAM role ARN, or None if parsing fails
    """
    if not user_arn:
        return None

    # Match assumed-role ARN format
    # arn:aws:sts::123456789012:assumed-role/my-role/session-name
    pattern = r"^arn:aws:sts::(\d+):assumed-role/([^/]+)/.*$"
    match = re.match(pattern, user_arn)
    if match:
        account_id = match.group(1)
        role_name = match.group(2)
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        logger.debug(f"Parsed assumed-role ARN: {user_arn} -> {role_arn}")
        return role_arn

    # Maybe it's already an IAM role ARN?
    # arn:aws:iam::123456789012:role/my-role
    if user_arn.startswith("arn:aws:iam::") and ":role/" in user_arn:
        logger.debug(f"ARN is already in IAM role format: {user_arn}")
        return user_arn

    logger.warning(f"Could not parse ARN format: {user_arn}")
    return None


def agent_entry_to_token_context(entry: AgentRegistryEntry) -> TokenContext:
    """
    Convert an agent registry entry to a TokenContext.

    Args:
        entry: Agent registry entry from DynamoDB

    Returns:
        TokenContext for the agent
    """
    return TokenContext(
        user_id=entry["agent_name"],
        org_id=entry["org_id"],
        team_id=entry["team_id"],
        department_id="",  # Agents don't have department_id
        account_type="service",  # All agents are service accounts
        is_admin=False,  # Agents are never admins
        expires_at=datetime.now(UTC) + timedelta(hours=1),  # Token context valid for 1 hour
        auth_source="iam",
        # Issue #3985 (A2): carry the registered plane through so internal-plane
        # routes can authorize on it. Previously dropped here, which left the
        # internal plane with no way to distinguish an internal principal from
        # any other registered agent.
        scope=entry.get("scope", ""),
    )
