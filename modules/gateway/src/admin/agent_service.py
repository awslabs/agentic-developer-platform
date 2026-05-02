"""
Agent (Cognito App Client) Management Service.

Issue #119: Unified Cognito JWT Auth
Issue #377: Migrated from agent-clients DynamoDB table to identity table.

- Manages Cognito App Clients for agent/service authentication
- Stores agent metadata in the identity DynamoDB table
- Supports creating, listing, updating, and deleting agents
"""

import logging
import os
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from src.shared.config import get_settings
from src.shared.exceptions import NotFoundError, ValidationError

from .agent_schemas import (
    AgentCreateRequest,
    AgentCredentialsResponse,
    AgentListResponse,
    AgentResponse,
    AgentUpdateRequest,
)

logger = logging.getLogger(__name__)


class AgentService:
    """
    Service for managing agents (Cognito App Clients).

    Creates Cognito App Clients with client_credentials grant and stores
    metadata in the identity DynamoDB table.
    """

    def __init__(
        self,
        cognito_client=None,
        dynamodb_resource=None,
        user_pool_id: str | None = None,
        table_name: str | None = None,
    ):
        """
        Initialize the agent service.

        Args:
            cognito_client: Boto3 Cognito Identity Provider client
            dynamodb_resource: Boto3 DynamoDB resource
            user_pool_id: Cognito User Pool ID
            table_name: DynamoDB table name for agent metadata
        """
        settings = get_settings()
        self.region = settings.aws_region

        # Cognito client
        self.cognito = cognito_client or boto3.client(
            "cognito-idp",
            region_name=self.region,
        )

        # DynamoDB
        self.dynamodb = dynamodb_resource or boto3.resource(
            "dynamodb",
            region_name=self.region,
        )

        # Configuration
        self.user_pool_id = user_pool_id or settings.cognito_user_pool_id
        self.table_name = table_name or os.environ.get(
            "IDENTITY_TABLE",
            f"{os.environ.get('BG_NAME_PREFIX', 'bedrockgw')}-identity",
        )

        # Build token endpoint
        cognito_domain = settings.cognito_domain or ""
        self.token_endpoint = f"https://{cognito_domain}.auth.{self.region}.amazoncognito.com/oauth2/token"

        if not self.user_pool_id:
            logger.warning("Cognito User Pool ID not configured")

    async def create_agent(self, request: AgentCreateRequest) -> AgentResponse:
        """
        Create a new agent (Cognito App Client).

        Args:
            request: Agent creation request

        Returns:
            AgentResponse: Created agent details

        Raises:
            ValidationError: If request is invalid
            ConflictError: If agent name already exists
        """
        logger.info(f"Creating agent: {request.name} for org: {request.org_id}")

        if not self.user_pool_id:
            raise ValidationError("Cognito User Pool ID not configured")

        # Generate a unique client name
        client_name = f"agent-{request.org_id}-{request.name}".lower()

        try:
            # Create Cognito App Client with client_credentials grant
            response = self.cognito.create_user_pool_client(
                UserPoolId=self.user_pool_id,
                ClientName=client_name,
                GenerateSecret=True,
                AllowedOAuthFlows=["client_credentials"],
                AllowedOAuthFlowsUserPoolClient=True,
                AllowedOAuthScopes=request.scopes,
                SupportedIdentityProviders=["COGNITO"],
                AccessTokenValidity=60,  # 60 minutes
                TokenValidityUnits={
                    "AccessToken": "minutes",
                },
                EnableTokenRevocation=True,
            )

            client_id = response["UserPoolClient"]["ClientId"]
            created_at = datetime.now(UTC)

            # Store agent metadata in DynamoDB
            table = self.dynamodb.Table(self.table_name)
            table.put_item(
                Item={
                    "client_id": client_id,
                    "name": request.name,
                    "client_name": client_name,
                    "org_id": request.org_id,
                    "team_id": request.team_id or "",
                    "department_id": request.department_id or "",
                    "description": request.description or "",
                    "scopes": request.scopes,
                    "status": "active",
                    "created_at": created_at.isoformat(),
                    "updated_at": created_at.isoformat(),
                }
            )

            logger.info(f"Created agent: {client_id}")

            return AgentResponse(
                client_id=client_id,
                name=request.name,
                org_id=request.org_id,
                team_id=request.team_id,
                department_id=request.department_id,
                description=request.description,
                scopes=request.scopes,
                created_at=created_at,
                status="active",
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "InvalidParameterException":
                raise ValidationError(f"Invalid parameter: {e}")
            elif error_code == "LimitExceededException":
                raise ValidationError("Too many app clients in user pool")
            else:
                logger.error(f"Cognito error creating agent: {e}")
                raise
        except Exception as e:
            logger.error(f"Error creating agent: {e}")
            raise

    async def get_agent(self, client_id: str, org_id: str) -> AgentResponse:
        """
        Get agent details by client ID.

        Args:
            client_id: Cognito App Client ID
            org_id: Organization ID (for authorization)

        Returns:
            AgentResponse: Agent details

        Raises:
            NotFoundError: If agent not found
        """
        # Get from DynamoDB
        table = self.dynamodb.Table(self.table_name)
        response = table.get_item(Key={"client_id": client_id})

        item = response.get("Item")
        if not item:
            raise NotFoundError(f"Agent not found: {client_id}")

        # Check org_id matches
        if item.get("org_id") != org_id:
            raise NotFoundError(f"Agent not found: {client_id}")

        return AgentResponse(
            client_id=item["client_id"],
            name=item["name"],
            org_id=item["org_id"],
            team_id=item.get("team_id") or None,
            department_id=item.get("department_id") or None,
            description=item.get("description") or None,
            scopes=item.get("scopes", []),
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None,
            status=item.get("status", "active"),
        )

    async def list_agents(
        self,
        org_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> AgentListResponse:
        """
        List agents for an organization.

        Args:
            org_id: Organization ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            AgentListResponse: List of agents
        """
        # Query DynamoDB GSI by org_id
        table = self.dynamodb.Table(self.table_name)

        try:
            response = table.query(
                IndexName="org_id-index",
                KeyConditionExpression="org_id = :org_id",
                ExpressionAttributeValues={":org_id": org_id},
            )

            items = response.get("Items", [])

            # Manual pagination (DynamoDB scan/query returns all matching items)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            page_items = items[start_idx:end_idx]

            agents = [
                AgentResponse(
                    client_id=item["client_id"],
                    name=item["name"],
                    org_id=item["org_id"],
                    team_id=item.get("team_id") or None,
                    department_id=item.get("department_id") or None,
                    description=item.get("description") or None,
                    scopes=item.get("scopes", []),
                    created_at=datetime.fromisoformat(item["created_at"]),
                    updated_at=datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None,
                    status=item.get("status", "active"),
                )
                for item in page_items
            ]

            return AgentListResponse(
                items=agents,
                total=len(items),
                page=page,
                page_size=page_size,
                has_more=end_idx < len(items),
            )

        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                # Table doesn't exist yet
                return AgentListResponse(items=[], total=0, page=page, page_size=page_size)
            raise

    async def get_agent_credentials(
        self,
        client_id: str,
        org_id: str,
    ) -> AgentCredentialsResponse:
        """
        Get agent credentials (client_id and client_secret).

        This is a one-time operation - the secret should be stored securely
        and cannot be retrieved again after this call.

        Args:
            client_id: Cognito App Client ID
            org_id: Organization ID (for authorization)

        Returns:
            AgentCredentialsResponse: Agent credentials

        Raises:
            NotFoundError: If agent not found
        """
        # First verify the agent exists and belongs to the org
        agent = await self.get_agent(client_id, org_id)

        # Get client secret from Cognito
        try:
            response = self.cognito.describe_user_pool_client(
                UserPoolId=self.user_pool_id,
                ClientId=client_id,
            )

            client = response.get("UserPoolClient", {})
            client_secret = client.get("ClientSecret")

            if not client_secret:
                raise NotFoundError("Agent client secret not available")

            # Build example curl command
            example_curl = f"""curl -X POST {self.token_endpoint} \\
  -H "Content-Type: application/x-www-form-urlencoded" \\
  -d "grant_type=client_credentials" \\
  -d "client_id={client_id}" \\
  -d "client_secret=<YOUR_CLIENT_SECRET>" \\
  -d "scope={" ".join(agent.scopes)}" """

            return AgentCredentialsResponse(
                client_id=client_id,
                client_secret=client_secret,
                token_endpoint=self.token_endpoint,
                scopes=agent.scopes,
                example_curl=example_curl,
            )

        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                raise NotFoundError(f"Agent not found: {client_id}")
            raise

    async def update_agent(
        self,
        client_id: str,
        org_id: str,
        request: AgentUpdateRequest,
    ) -> AgentResponse:
        """
        Update agent metadata.

        Args:
            client_id: Cognito App Client ID
            org_id: Organization ID (for authorization)
            request: Update request

        Returns:
            AgentResponse: Updated agent details

        Raises:
            NotFoundError: If agent not found
        """
        # Verify agent exists and belongs to org
        agent = await self.get_agent(client_id, org_id)

        # Build update expression
        update_parts = []
        expression_values = {":updated_at": datetime.now(UTC).isoformat()}
        expression_names = {}

        if request.name is not None:
            update_parts.append("#name = :name")
            expression_values[":name"] = request.name
            expression_names["#name"] = "name"

        if request.team_id is not None:
            update_parts.append("team_id = :team_id")
            expression_values[":team_id"] = request.team_id

        if request.department_id is not None:
            update_parts.append("department_id = :department_id")
            expression_values[":department_id"] = request.department_id

        if request.description is not None:
            update_parts.append("description = :description")
            expression_values[":description"] = request.description

        if request.status is not None:
            update_parts.append("#status = :status")
            expression_values[":status"] = request.status
            expression_names["#status"] = "status"

        update_parts.append("updated_at = :updated_at")

        if not update_parts:
            return agent

        # Update DynamoDB
        table = self.dynamodb.Table(self.table_name)
        update_expression = "SET " + ", ".join(update_parts)

        kwargs = {
            "Key": {"client_id": client_id},
            "UpdateExpression": update_expression,
            "ExpressionAttributeValues": expression_values,
            "ReturnValues": "ALL_NEW",
        }
        if expression_names:
            kwargs["ExpressionAttributeNames"] = expression_names

        response = table.update_item(**kwargs)
        item = response["Attributes"]

        logger.info(f"Updated agent: {client_id}")

        return AgentResponse(
            client_id=item["client_id"],
            name=item["name"],
            org_id=item["org_id"],
            team_id=item.get("team_id") or None,
            department_id=item.get("department_id") or None,
            description=item.get("description") or None,
            scopes=item.get("scopes", []),
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None,
            status=item.get("status", "active"),
        )

    async def delete_agent(self, client_id: str, org_id: str) -> None:
        """
        Delete an agent.

        Removes the Cognito App Client and DynamoDB metadata.

        Args:
            client_id: Cognito App Client ID
            org_id: Organization ID (for authorization)

        Raises:
            NotFoundError: If agent not found
        """
        # Verify agent exists and belongs to org
        await self.get_agent(client_id, org_id)

        # Delete Cognito App Client
        try:
            self.cognito.delete_user_pool_client(
                UserPoolId=self.user_pool_id,
                ClientId=client_id,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise

        # Delete from DynamoDB
        table = self.dynamodb.Table(self.table_name)
        table.delete_item(Key={"client_id": client_id})

        logger.info(f"Deleted agent: {client_id}")
