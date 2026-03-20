"""
Agent Registry Service for DynamoDB-based agent management.

Issue #248: Admin API for Agent Registry
- Manages agent configurations in DynamoDB for IAM/SigV4 authentication
- UUID-based agent_id as primary key
- role_arn in GSI for Lambda authorizer lookup
- Supports CRUD operations with pagination
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from src.shared.config import get_settings
from src.shared.exceptions import ConflictError, NotFoundError, ValidationError

from .agent_registry_schemas import (
    AgentRegistryCreateRequest,
    AgentRegistryListResponse,
    AgentRegistryResponse,
    AgentRegistryUpdateRequest,
)

logger = logging.getLogger(__name__)


class AgentRegistryService:
    """
    Service for managing agents in the DynamoDB registry.

    The agent registry stores agent configurations that map IAM role ARNs
    to agent identities for the Lambda authorizer.
    """

    def __init__(
        self,
        dynamodb_client=None,
        table_name: str | None = None,
    ):
        """
        Initialize the agent registry service.

        Args:
            dynamodb_client: Boto3 DynamoDB client (low-level)
            table_name: DynamoDB table name for agent registry
        """
        settings = get_settings()
        self.region = settings.aws_region

        # DynamoDB client (low-level for better control)
        self.dynamodb = dynamodb_client or boto3.client(
            "dynamodb",
            region_name=self.region,
        )

        # Table name
        self.table_name = table_name or os.environ.get(
            "AGENT_REGISTRY_TABLE",
            f"{os.environ.get('BG_NAME_PREFIX', 'bedrockgw')}-agent-registry",
        )

        # GSI names
        self.role_arn_index = "by-role-arn"
        self.org_team_index = "by-org-team"
        self.owner_index = "by-owner"

    async def create_agent(self, request: AgentRegistryCreateRequest) -> AgentRegistryResponse:
        """
        Create a new agent in the registry.

        Issue #249: Supports automatic budget creation when budget_monthly_usd is provided.
        Uses Postgres-first, DynamoDB-second pattern with compensating rollback.

        Args:
            request: Agent creation request

        Returns:
            AgentRegistryResponse: Created agent details

        Raises:
            ConflictError: If role_arn already exists
            ValidationError: If request is invalid
        """
        logger.info(f"Creating agent: {request.agent_name} for org: {request.org_id}")

        # Check if role_arn already exists (must be unique)
        existing = await self.get_agent_by_role(request.role_arn)
        if existing:
            raise ConflictError(f"Agent with role_arn '{request.role_arn}' already exists (agent_id: {existing.agent_id})")

        # Validate existing budget_config_id if provided
        if request.budget_config_id:
            from src.admin.budget_helper import budget_helper_service

            if not await budget_helper_service.validate_budget_config_exists(request.budget_config_id, request.org_id):
                raise ValidationError(f"Budget config '{request.budget_config_id}' does not exist or belongs to another organization")

        # Generate UUID for agent_id
        agent_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        # Issue #249: Auto-create budget config if budget_monthly_usd is provided
        # Cross-database consistency: Postgres first, DynamoDB second
        budget_config_id = request.budget_config_id
        auto_created_budget_id: str | None = None

        if request.budget_monthly_usd and not budget_config_id:
            from src.admin.budget_helper import budget_helper_service

            try:
                budget_config_id = await budget_helper_service.create_agent_budget_config(
                    agent_id=agent_id,
                    org_id=request.org_id,
                    monthly_limit_usd=request.budget_monthly_usd,
                    enforcement_mode="hard",
                )
                auto_created_budget_id = budget_config_id
                logger.info(f"Auto-created budget config {budget_config_id} for agent {agent_id}")
            except Exception as e:
                logger.error(f"Failed to create budget config: {e}")
                raise ValidationError(f"Failed to create budget config: {e}")

        # Build DynamoDB item with required fields
        item = {
            "agent_id": {"S": agent_id},
            "agent_name": {"S": request.agent_name},
            "role_arn": {"S": request.role_arn},
            "org_id": {"S": request.org_id},
            "owner": {"S": request.owner},
            "scope": {"S": request.scope},
            "status": {"S": "active"},
            "created_at": {"S": now_iso},
            "updated_at": {"S": now_iso},
        }

        # Add optional fields only if they have values (avoid empty strings in DynamoDB)
        if request.team_id:
            item["team_id"] = {"S": request.team_id}
        if budget_config_id:
            item["budget_config_id"] = {"S": budget_config_id}
        if request.description:
            item["description"] = {"S": request.description}
        if request.image_uri:
            item["image_uri"] = {"S": request.image_uri}
        if request.code_repo:
            item["code_repo"] = {"S": request.code_repo}
        if request.workflow_name:
            item["workflow_name"] = {"S": request.workflow_name}
        if request.allowed_models:
            item["allowed_models"] = {"SS": request.allowed_models}

        try:
            await asyncio.to_thread(
                self.dynamodb.put_item,
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(agent_id)",
            )
        except ClientError as e:
            # Issue #249: Compensating action - delete auto-created budget if DynamoDB fails
            if auto_created_budget_id:
                try:
                    from src.admin.budget_helper import budget_helper_service

                    await budget_helper_service.delete_budget_config(auto_created_budget_id, request.org_id)
                    logger.info(f"Rolled back budget config {auto_created_budget_id} after DynamoDB failure")
                except Exception as rollback_error:
                    logger.error(f"Failed to rollback budget config: {rollback_error}")

            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ConflictError(f"Agent with agent_id '{agent_id}' already exists")
            logger.error(f"DynamoDB error creating agent: {e}")
            raise
        except Exception:
            # Issue #249: Compensating action for any other exception
            if auto_created_budget_id:
                try:
                    from src.admin.budget_helper import budget_helper_service

                    await budget_helper_service.delete_budget_config(auto_created_budget_id, request.org_id)
                    logger.info(f"Rolled back budget config {auto_created_budget_id} after error")
                except Exception as rollback_error:
                    logger.error(f"Failed to rollback budget config: {rollback_error}")
            raise

        logger.info(f"Created agent: {agent_id} ({request.agent_name})")

        return AgentRegistryResponse(
            agent_id=agent_id,
            agent_name=request.agent_name,
            role_arn=request.role_arn,
            org_id=request.org_id,
            team_id=request.team_id,
            owner=request.owner,
            scope=request.scope,
            budget_config_id=budget_config_id,
            allowed_models=request.allowed_models,
            status="active",
            description=request.description,
            image_uri=request.image_uri,
            code_repo=request.code_repo,
            workflow_name=request.workflow_name,
            created_at=now,
            updated_at=now,
        )

    async def get_agent(self, agent_id: str) -> AgentRegistryResponse:
        """
        Get agent by agent_id (primary key).

        Args:
            agent_id: Agent UUID

        Returns:
            AgentRegistryResponse: Agent details

        Raises:
            NotFoundError: If agent not found
        """
        try:
            response = await asyncio.to_thread(
                self.dynamodb.get_item,
                TableName=self.table_name,
                Key={"agent_id": {"S": agent_id}},
            )
        except ClientError as e:
            logger.error(f"DynamoDB error getting agent: {e}")
            raise

        item = response.get("Item")
        if not item:
            raise NotFoundError(f"Agent not found: {agent_id}")

        return self._item_to_response(item)

    async def get_agent_by_role(self, role_arn: str) -> AgentRegistryResponse | None:
        """
        Get agent by role_arn (GSI query).

        Used by Lambda authorizer and for uniqueness validation.

        Args:
            role_arn: IAM role ARN

        Returns:
            AgentRegistryResponse or None if not found
        """
        try:
            response = await asyncio.to_thread(
                self.dynamodb.query,
                TableName=self.table_name,
                IndexName=self.role_arn_index,
                KeyConditionExpression="role_arn = :role_arn",
                ExpressionAttributeValues={":role_arn": {"S": role_arn}},
                Limit=1,
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                # Table or index doesn't exist
                return None
            logger.error(f"DynamoDB error querying by role_arn: {e}")
            raise

        items = response.get("Items", [])
        if not items:
            return None

        return self._item_to_response(items[0])

    async def list_agents(
        self,
        org_id: str | None = None,
        team_id: str | None = None,
        owner: str | None = None,
        page_size: int = 50,
        last_key: str | None = None,
    ) -> AgentRegistryListResponse:
        """
        List agents with optional filtering and pagination.

        Args:
            org_id: Filter by organization ID (uses by-org-team GSI)
            team_id: Filter by team ID (requires org_id)
            owner: Filter by owner (uses by-owner GSI)
            page_size: Maximum items per page
            last_key: Pagination token from previous response

        Returns:
            AgentRegistryListResponse: List of agents with pagination
        """
        # Decode pagination token if provided
        exclusive_start_key = None
        if last_key:
            try:
                exclusive_start_key = json.loads(base64.b64decode(last_key))
            except Exception:
                raise ValidationError("Invalid pagination token")

        try:
            if org_id and team_id:
                # Query by org_id and team_id (exact match on GSI)
                response = await self._query_by_org_team(org_id, team_id, page_size, exclusive_start_key)
            elif org_id:
                # Query by org_id only (GSI with begins_with on sort key)
                response = await self._query_by_org(org_id, page_size, exclusive_start_key)
            elif owner:
                # Query by owner (GSI)
                response = await self._query_by_owner(owner, page_size, exclusive_start_key)
            else:
                # Scan all (not recommended for large tables)
                response = await self._scan_all(page_size, exclusive_start_key)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                return AgentRegistryListResponse(items=[], count=0, last_key=None)
            logger.error(f"DynamoDB error listing agents: {e}")
            raise

        items = [self._item_to_response(item) for item in response.get("Items", [])]

        # Encode pagination token for next page
        next_key = None
        if response.get("LastEvaluatedKey"):
            next_key = base64.b64encode(json.dumps(response["LastEvaluatedKey"]).encode()).decode()

        # Note: DynamoDB doesn't return total count efficiently
        # count is the number of items in this page, not total
        return AgentRegistryListResponse(
            items=items,
            count=len(items),
            last_key=next_key,
        )

    async def update_agent(self, agent_id: str, request: AgentRegistryUpdateRequest) -> AgentRegistryResponse:
        """
        Update agent attributes.

        Args:
            agent_id: Agent UUID
            request: Update request with changed fields

        Returns:
            AgentRegistryResponse: Updated agent details

        Raises:
            NotFoundError: If agent not found
            ConflictError: If new role_arn already exists
        """
        # Verify agent exists
        existing = await self.get_agent(agent_id)

        # If role_arn is being updated, check for uniqueness
        if request.role_arn and request.role_arn != existing.role_arn:
            conflict = await self.get_agent_by_role(request.role_arn)
            if conflict and conflict.agent_id != agent_id:
                raise ConflictError(f"Agent with role_arn '{request.role_arn}' already exists (agent_id: {conflict.agent_id})")

        # Build update expression
        update_parts = []
        expression_values = {":updated_at": {"S": datetime.now(UTC).isoformat()}}
        expression_names = {}
        remove_parts = []

        if request.agent_name is not None:
            update_parts.append("#agent_name = :agent_name")
            expression_values[":agent_name"] = {"S": request.agent_name}
            expression_names["#agent_name"] = "agent_name"

        if request.role_arn is not None:
            update_parts.append("role_arn = :role_arn")
            expression_values[":role_arn"] = {"S": request.role_arn}

        if request.team_id is not None:
            update_parts.append("team_id = :team_id")
            expression_values[":team_id"] = {"S": request.team_id}

        if request.owner is not None:
            update_parts.append("#owner = :owner")
            expression_values[":owner"] = {"S": request.owner}
            expression_names["#owner"] = "owner"

        if request.scope is not None:
            update_parts.append("scope = :scope")
            expression_values[":scope"] = {"S": request.scope}

        if request.budget_config_id is not None:
            update_parts.append("budget_config_id = :budget_config_id")
            expression_values[":budget_config_id"] = {"S": request.budget_config_id}

        if request.allowed_models is not None:
            if request.allowed_models:
                update_parts.append("allowed_models = :allowed_models")
                expression_values[":allowed_models"] = {"SS": request.allowed_models}
            else:
                # Remove the attribute if empty list provided
                remove_parts.append("allowed_models")

        if request.status is not None:
            update_parts.append("#status = :status")
            expression_values[":status"] = {"S": request.status}
            expression_names["#status"] = "status"

        if request.description is not None:
            update_parts.append("description = :description")
            expression_values[":description"] = {"S": request.description}

        if request.image_uri is not None:
            update_parts.append("image_uri = :image_uri")
            expression_values[":image_uri"] = {"S": request.image_uri}

        if request.code_repo is not None:
            update_parts.append("code_repo = :code_repo")
            expression_values[":code_repo"] = {"S": request.code_repo}

        if request.workflow_name is not None:
            update_parts.append("workflow_name = :workflow_name")
            expression_values[":workflow_name"] = {"S": request.workflow_name}

        # Always update updated_at
        update_parts.append("updated_at = :updated_at")

        if not update_parts and not remove_parts:
            return existing

        # Build full update expression
        update_expression = "SET " + ", ".join(update_parts)
        if remove_parts:
            update_expression += " REMOVE " + ", ".join(remove_parts)

        try:
            kwargs = {
                "TableName": self.table_name,
                "Key": {"agent_id": {"S": agent_id}},
                "UpdateExpression": update_expression,
                "ExpressionAttributeValues": expression_values,
                "ReturnValues": "ALL_NEW",
            }
            if expression_names:
                kwargs["ExpressionAttributeNames"] = expression_names

            response = await asyncio.to_thread(self.dynamodb.update_item, **kwargs)
        except ClientError as e:
            logger.error(f"DynamoDB error updating agent: {e}")
            raise

        logger.info(f"Updated agent: {agent_id}")
        return self._item_to_response(response["Attributes"])

    async def delete_agent(self, agent_id: str) -> None:
        """
        Soft delete an agent by setting status to 'disabled'.

        Args:
            agent_id: Agent UUID

        Raises:
            NotFoundError: If agent not found
        """
        # Verify agent exists
        await self.get_agent(agent_id)

        try:
            await asyncio.to_thread(
                self.dynamodb.update_item,
                TableName=self.table_name,
                Key={"agent_id": {"S": agent_id}},
                UpdateExpression="SET #status = :status, updated_at = :updated_at",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": {"S": "disabled"},
                    ":updated_at": {"S": datetime.now(UTC).isoformat()},
                },
            )
        except ClientError as e:
            logger.error(f"DynamoDB error deleting agent: {e}")
            raise

        logger.info(f"Soft deleted agent: {agent_id}")

    async def _query_by_org_team(
        self,
        org_id: str,
        team_id: str,
        page_size: int,
        exclusive_start_key: dict | None,
    ) -> dict:
        """Query GSI by org_id and team_id."""
        kwargs = {
            "TableName": self.table_name,
            "IndexName": self.org_team_index,
            "KeyConditionExpression": "org_id = :org_id AND team_id = :team_id",
            "ExpressionAttributeValues": {
                ":org_id": {"S": org_id},
                ":team_id": {"S": team_id},
            },
            "Limit": page_size,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        return await asyncio.to_thread(self.dynamodb.query, **kwargs)

    async def _query_by_org(
        self,
        org_id: str,
        page_size: int,
        exclusive_start_key: dict | None,
    ) -> dict:
        """Query GSI by org_id only (all teams)."""
        kwargs = {
            "TableName": self.table_name,
            "IndexName": self.org_team_index,
            "KeyConditionExpression": "org_id = :org_id",
            "ExpressionAttributeValues": {
                ":org_id": {"S": org_id},
            },
            "Limit": page_size,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        return await asyncio.to_thread(self.dynamodb.query, **kwargs)

    async def _query_by_owner(
        self,
        owner: str,
        page_size: int,
        exclusive_start_key: dict | None,
    ) -> dict:
        """Query GSI by owner."""
        kwargs = {
            "TableName": self.table_name,
            "IndexName": self.owner_index,
            "KeyConditionExpression": "#owner = :owner",
            "ExpressionAttributeNames": {"#owner": "owner"},
            "ExpressionAttributeValues": {
                ":owner": {"S": owner},
            },
            "Limit": page_size,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        return await asyncio.to_thread(self.dynamodb.query, **kwargs)

    async def _scan_all(
        self,
        page_size: int,
        exclusive_start_key: dict | None,
    ) -> dict:
        """Scan all items (use sparingly)."""
        kwargs = {
            "TableName": self.table_name,
            "Limit": page_size,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        return await asyncio.to_thread(self.dynamodb.scan, **kwargs)

    def _item_to_response(self, item: dict) -> AgentRegistryResponse:
        """Convert DynamoDB item to response schema."""
        return AgentRegistryResponse(
            agent_id=item["agent_id"]["S"],
            agent_name=item.get("agent_name", {}).get("S", ""),
            role_arn=item.get("role_arn", {}).get("S", ""),
            org_id=item.get("org_id", {}).get("S", ""),
            team_id=item.get("team_id", {}).get("S") or None,
            owner=item.get("owner", {}).get("S", ""),
            scope=item.get("scope", {}).get("S", "shared"),
            budget_config_id=item.get("budget_config_id", {}).get("S") or None,
            allowed_models=item.get("allowed_models", {}).get("SS", []),
            status=item.get("status", {}).get("S", "active"),
            description=item.get("description", {}).get("S") or None,
            image_uri=item.get("image_uri", {}).get("S") or None,
            code_repo=item.get("code_repo", {}).get("S") or None,
            workflow_name=item.get("workflow_name", {}).get("S") or None,
            created_at=datetime.fromisoformat(item.get("created_at", {}).get("S", datetime.now(UTC).isoformat())),
            updated_at=datetime.fromisoformat(item.get("updated_at", {}).get("S", datetime.now(UTC).isoformat())),
        )

    async def get_agent_usage(
        self,
        agent_id: str,
        period: str = "monthly",
        start_date: str | None = None,
        end_date: str | None = None,
    ):
        """
        Get usage data for an agent.

        Issue #249: Per-Agent Budget Assignment and Usage Dashboard

        Args:
            agent_id: Agent UUID
            period: Aggregation period (daily/weekly/monthly)
            start_date: Optional start date filter (YYYY-MM-DD)
            end_date: Optional end date filter (YYYY-MM-DD)

        Returns:
            AgentUsageResponse with usage data and budget status
        """
        from decimal import Decimal

        from src.admin.agent_registry_schemas import (
            AgentBudgetStatus,
            AgentUsageResponse,
        )
        from src.admin.budget_helper import budget_helper_service

        # Get agent details
        agent = await self.get_agent(agent_id)

        # Initialize response with zero values
        total_requests = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost_usd = Decimal("0")
        by_model: list = []
        budget_status = None

        # If agent has a budget_config_id, get usage from budget_usage table
        if agent.budget_config_id:
            config, current_spend = await budget_helper_service.get_budget_and_usage_by_config_id(
                agent.budget_config_id,
                period_type=period,
            )

            if config:
                total_cost_usd = current_spend
                usage = await budget_helper_service.get_agent_usage(
                    agent.budget_config_id,
                    period_type=period,
                )
                if usage:
                    total_requests = usage.request_count
                    total_input_tokens = usage.total_tokens // 2  # Approximate split
                    total_output_tokens = usage.total_tokens - total_input_tokens

                # Build budget status
                remaining = config.budget_amount_usd - current_spend
                utilization = float(current_spend / config.budget_amount_usd * 100) if config.budget_amount_usd > 0 else 0.0

                budget_status = AgentBudgetStatus(
                    monthly_limit_usd=config.budget_amount_usd,
                    used_usd=current_spend,
                    remaining_usd=max(Decimal("0"), remaining),
                    utilization_pct=min(100.0, utilization),
                )

        return AgentUsageResponse(
            agent_id=agent.agent_id,
            agent_name=agent.agent_name,
            period=period,
            total_requests=total_requests,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_cost_usd=total_cost_usd,
            by_model=by_model,
            budget=budget_status,
        )
