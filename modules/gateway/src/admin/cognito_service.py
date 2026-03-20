"""
Cognito Integration Service for user management.

Provides methods to create, update, and delete users in AWS Cognito User Pool,
as well as manage user groups for organization-level access control.
"""

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.shared.exceptions import BedrockGatewayError

logger = logging.getLogger(__name__)


class CognitoServiceError(BedrockGatewayError):
    """Base exception for Cognito service errors."""

    def __init__(self, message: str = "Cognito service error", details: dict | None = None):
        super().__init__(
            error="cognito_service_error",
            message=message,
            status_code=500,
            details=details,
        )


class UserAlreadyExistsError(BedrockGatewayError):
    """Exception raised when trying to create a user that already exists."""

    def __init__(self, email: str):
        super().__init__(
            error="user_already_exists",
            message=f"User with email {email} already exists",
            status_code=409,
            details={"email": email},
        )


class UserNotFoundError(BedrockGatewayError):
    """Exception raised when a user is not found."""

    def __init__(self, username: str):
        super().__init__(
            error="user_not_found",
            message=f"User {username} not found",
            status_code=404,
            details={"username": username},
        )


class GroupNotFoundError(BedrockGatewayError):
    """Exception raised when a group is not found."""

    def __init__(self, group_name: str):
        super().__init__(
            error="group_not_found",
            message=f"Group {group_name} not found",
            status_code=404,
            details={"group_name": group_name},
        )


class CognitoService:
    """
    Service for managing users and groups in AWS Cognito User Pool.

    This service provides:
    - User creation with custom attributes (org_id, department_id, team_id, role)
    - User deletion
    - User group management for organization-level isolation
    - User retrieval and listing
    """

    def __init__(self, user_pool_id: str | None = None, region: str | None = None):
        """
        Initialize the Cognito service.

        Args:
            user_pool_id: Cognito User Pool ID. If not provided, uses COGNITO_USER_POOL_ID env var.
            region: AWS region. If not provided, uses AWS_REGION env var or defaults to us-east-1.
        """
        self.user_pool_id = user_pool_id or os.environ.get("BG_COGNITO_USER_POOL_ID") or os.environ.get("COGNITO_USER_POOL_ID", "")
        self.region = region or os.environ.get("BG_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")

        if not self.user_pool_id:
            logger.warning("COGNITO_USER_POOL_ID not set - Cognito operations will fail")

        self._client = None

    @property
    def client(self) -> Any:
        """Lazy initialization of boto3 Cognito client."""
        if self._client is None:
            self._client = boto3.client("cognito-idp", region_name=self.region)
        return self._client

    def create_user(
        self,
        email: str,
        org_id: str,
        dept_id: str,
        team_id: str,
        name: str | None = None,
        role: str = "user",
        github_username: str | None = None,
        suppress_invitation: bool = False,
    ) -> dict[str, Any]:
        """
        Create a new user in Cognito with custom attributes.

        Args:
            email: User email address (used as username)
            org_id: Organization ID
            dept_id: Department ID
            team_id: Team ID
            name: User's full name
            role: User role (admin, user)
            github_username: User's GitHub username
            suppress_invitation: If True, don't send invitation email

        Returns:
            dict: Created user data from Cognito

        Raises:
            UserAlreadyExistsError: If user with email already exists
            CognitoServiceError: If user creation fails
        """
        try:
            user_attributes = [
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:org_id", "Value": org_id},
                {"Name": "custom:department_id", "Value": dept_id},
                {"Name": "custom:team_id", "Value": team_id},
                {"Name": "custom:role", "Value": role},
            ]

            if name:
                user_attributes.append({"Name": "name", "Value": name})

            if github_username:
                user_attributes.append({"Name": "custom:github_username", "Value": github_username})

            message_action = "SUPPRESS" if suppress_invitation else "RESEND"

            response = self.client.admin_create_user(
                UserPoolId=self.user_pool_id,
                Username=email,
                UserAttributes=user_attributes,
                MessageAction=message_action,
                DesiredDeliveryMediums=["EMAIL"],
            )

            logger.info(f"Created Cognito user: {email} in org {org_id}")
            return response.get("User", {})

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UsernameExistsException":
                logger.warning(f"User already exists: {email}")
                raise UserAlreadyExistsError(email)
            logger.error(f"Failed to create Cognito user {email}: {e}")
            raise CognitoServiceError(f"Failed to create user: {str(e)}")

    def delete_user(self, cognito_sub: str | None = None, username: str | None = None) -> bool:
        """
        Delete a user from Cognito.

        Args:
            cognito_sub: Cognito user sub (ID) - not directly usable for deletion
            username: Cognito username (email)

        Returns:
            bool: True if deleted successfully

        Raises:
            UserNotFoundError: If user not found
            CognitoServiceError: If deletion fails
        """
        if not username and cognito_sub:
            # If we have sub but not username, we need to look up the user first
            user = self.get_user_by_sub(cognito_sub)
            if user:
                username = user.get("Username")

        if not username:
            raise CognitoServiceError("Either username or valid cognito_sub required")

        try:
            self.client.admin_delete_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
            logger.info(f"Deleted Cognito user: {username}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                logger.warning(f"User not found for deletion: {username}")
                raise UserNotFoundError(username)
            logger.error(f"Failed to delete Cognito user {username}: {e}")
            raise CognitoServiceError(f"Failed to delete user: {str(e)}")

    def get_user(self, username: str) -> dict[str, Any] | None:
        """
        Get a user from Cognito by username.

        Args:
            username: Cognito username (email)

        Returns:
            dict: User data or None if not found
        """
        try:
            response = self.client.admin_get_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
            return response

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                return None
            logger.error(f"Failed to get Cognito user {username}: {e}")
            raise CognitoServiceError(f"Failed to get user: {str(e)}")

    def get_user_by_sub(self, cognito_sub: str) -> dict[str, Any] | None:
        """
        Get a user from Cognito by their sub (ID).

        Args:
            cognito_sub: Cognito user sub

        Returns:
            dict: User data or None if not found
        """
        try:
            # Use list_users with filter to find by sub
            response = self.client.list_users(
                UserPoolId=self.user_pool_id,
                Filter=f'sub = "{cognito_sub}"',
                Limit=1,
            )
            users = response.get("Users", [])
            return users[0] if users else None

        except ClientError as e:
            logger.error(f"Failed to get Cognito user by sub {cognito_sub}: {e}")
            raise CognitoServiceError(f"Failed to get user: {str(e)}")

    def update_user_attributes(self, username: str, attributes: dict[str, str]) -> bool:
        """
        Update user attributes in Cognito.

        Args:
            username: Cognito username (email)
            attributes: Dictionary of attribute name to value

        Returns:
            bool: True if updated successfully

        Raises:
            UserNotFoundError: If user not found
            CognitoServiceError: If update fails
        """
        try:
            user_attributes = [{"Name": k, "Value": v} for k, v in attributes.items()]

            self.client.admin_update_user_attributes(
                UserPoolId=self.user_pool_id,
                Username=username,
                UserAttributes=user_attributes,
            )
            logger.info(f"Updated Cognito user attributes: {username}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            logger.error(f"Failed to update Cognito user {username}: {e}")
            raise CognitoServiceError(f"Failed to update user: {str(e)}")

    def add_user_to_group(self, username: str, group_name: str) -> bool:
        """
        Add a user to a Cognito group.

        Args:
            username: Cognito username (email)
            group_name: Name of the group

        Returns:
            bool: True if added successfully

        Raises:
            UserNotFoundError: If user not found
            GroupNotFoundError: If group not found
            CognitoServiceError: If operation fails
        """
        try:
            self.client.admin_add_user_to_group(
                UserPoolId=self.user_pool_id,
                Username=username,
                GroupName=group_name,
            )
            logger.info(f"Added user {username} to group {group_name}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            if error_code == "ResourceNotFoundException":
                raise GroupNotFoundError(group_name)
            logger.error(f"Failed to add user {username} to group {group_name}: {e}")
            raise CognitoServiceError(f"Failed to add user to group: {str(e)}")

    def remove_user_from_group(self, username: str, group_name: str) -> bool:
        """
        Remove a user from a Cognito group.

        Args:
            username: Cognito username (email)
            group_name: Name of the group

        Returns:
            bool: True if removed successfully

        Raises:
            UserNotFoundError: If user not found
            GroupNotFoundError: If group not found
            CognitoServiceError: If operation fails
        """
        try:
            self.client.admin_remove_user_from_group(
                UserPoolId=self.user_pool_id,
                Username=username,
                GroupName=group_name,
            )
            logger.info(f"Removed user {username} from group {group_name}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            if error_code == "ResourceNotFoundException":
                raise GroupNotFoundError(group_name)
            logger.error(f"Failed to remove user {username} from group {group_name}: {e}")
            raise CognitoServiceError(f"Failed to remove user from group: {str(e)}")

    def create_org_group(self, org_id: str, description: str | None = None) -> dict[str, Any]:
        """
        Create a Cognito group for an organization.

        Group name pattern: org-{org_id}

        Args:
            org_id: Organization ID
            description: Optional group description

        Returns:
            dict: Created group data

        Raises:
            CognitoServiceError: If group creation fails (including if already exists)
        """
        group_name = f"org-{org_id}"
        try:
            response = self.client.create_group(
                GroupName=group_name,
                UserPoolId=self.user_pool_id,
                Description=description or f"Organization group for {org_id}",
            )
            logger.info(f"Created Cognito group: {group_name}")
            return response.get("Group", {})

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "GroupExistsException":
                logger.warning(f"Group already exists: {group_name}")
                # Return existing group info
                return self.get_group(group_name) or {}
            logger.error(f"Failed to create Cognito group {group_name}: {e}")
            raise CognitoServiceError(f"Failed to create group: {str(e)}")

    def delete_org_group(self, org_id: str) -> bool:
        """
        Delete a Cognito group for an organization.

        Args:
            org_id: Organization ID

        Returns:
            bool: True if deleted successfully

        Raises:
            GroupNotFoundError: If group not found
            CognitoServiceError: If deletion fails
        """
        group_name = f"org-{org_id}"
        try:
            self.client.delete_group(
                GroupName=group_name,
                UserPoolId=self.user_pool_id,
            )
            logger.info(f"Deleted Cognito group: {group_name}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                raise GroupNotFoundError(group_name)
            logger.error(f"Failed to delete Cognito group {group_name}: {e}")
            raise CognitoServiceError(f"Failed to delete group: {str(e)}")

    def get_group(self, group_name: str) -> dict[str, Any] | None:
        """
        Get a Cognito group by name.

        Args:
            group_name: Name of the group

        Returns:
            dict: Group data or None if not found
        """
        try:
            response = self.client.get_group(
                GroupName=group_name,
                UserPoolId=self.user_pool_id,
            )
            return response.get("Group")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                return None
            logger.error(f"Failed to get Cognito group {group_name}: {e}")
            raise CognitoServiceError(f"Failed to get group: {str(e)}")

    def list_users_in_group(self, group_name: str, limit: int = 60) -> list[dict[str, Any]]:
        """
        List users in a Cognito group.

        Args:
            group_name: Name of the group
            limit: Maximum number of users to return

        Returns:
            list: List of user data dictionaries

        Raises:
            GroupNotFoundError: If group not found
            CognitoServiceError: If listing fails
        """
        try:
            users = []
            paginator = self.client.get_paginator("list_users_in_group")

            for page in paginator.paginate(
                UserPoolId=self.user_pool_id,
                GroupName=group_name,
                PaginationConfig={"MaxItems": limit},
            ):
                users.extend(page.get("Users", []))

            return users

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                raise GroupNotFoundError(group_name)
            logger.error(f"Failed to list users in Cognito group {group_name}: {e}")
            raise CognitoServiceError(f"Failed to list users in group: {str(e)}")

    def list_groups_for_user(self, username: str) -> list[dict[str, Any]]:
        """
        List all groups a user belongs to.

        Args:
            username: Cognito username (email)

        Returns:
            list: List of group data dictionaries

        Raises:
            UserNotFoundError: If user not found
            CognitoServiceError: If listing fails
        """
        try:
            response = self.client.admin_list_groups_for_user(
                Username=username,
                UserPoolId=self.user_pool_id,
            )
            return response.get("Groups", [])

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            logger.error(f"Failed to list groups for user {username}: {e}")
            raise CognitoServiceError(f"Failed to list groups for user: {str(e)}")

    def set_user_password(self, username: str, password: str, permanent: bool = True) -> bool:
        """
        Set a user's password.

        Args:
            username: Cognito username (email)
            password: New password
            permanent: If True, password is permanent; if False, user must change on next login

        Returns:
            bool: True if password set successfully

        Raises:
            UserNotFoundError: If user not found
            CognitoServiceError: If operation fails
        """
        try:
            self.client.admin_set_user_password(
                UserPoolId=self.user_pool_id,
                Username=username,
                Password=password,
                Permanent=permanent,
            )
            logger.info(f"Set password for Cognito user: {username}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            logger.error(f"Failed to set password for user {username}: {e}")
            raise CognitoServiceError(f"Failed to set password: {str(e)}")

    def disable_user(self, username: str) -> bool:
        """
        Disable a user in Cognito.

        Args:
            username: Cognito username (email)

        Returns:
            bool: True if disabled successfully

        Raises:
            UserNotFoundError: If user not found
            CognitoServiceError: If operation fails
        """
        try:
            self.client.admin_disable_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
            logger.info(f"Disabled Cognito user: {username}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            logger.error(f"Failed to disable user {username}: {e}")
            raise CognitoServiceError(f"Failed to disable user: {str(e)}")

    def enable_user(self, username: str) -> bool:
        """
        Enable a user in Cognito.

        Args:
            username: Cognito username (email)

        Returns:
            bool: True if enabled successfully

        Raises:
            UserNotFoundError: If user not found
            CognitoServiceError: If operation fails
        """
        try:
            self.client.admin_enable_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
            logger.info(f"Enabled Cognito user: {username}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                raise UserNotFoundError(username)
            logger.error(f"Failed to enable user {username}: {e}")
            raise CognitoServiceError(f"Failed to enable user: {str(e)}")

    # =============================================================================
    # Issue #226: Cognito as Source of Truth Methods
    # =============================================================================

    def list_users_by_org(
        self,
        org_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        List users filtered by custom:org_id attribute.

        Issue #226: Cognito as source of truth for users.

        Args:
            org_id: Organization ID to filter by
            page: Page number (1-indexed)
            page_size: Number of items per page

        Returns:
            Tuple of (list of user data dicts, total count estimate)

        Raises:
            CognitoServiceError: If listing fails
        """
        try:
            users: list[dict[str, Any]] = []
            pagination_token = None

            # Cognito doesn't support filtering by custom attributes directly
            # We need to list all users and filter client-side
            # For large user pools, consider using a search index
            while True:
                params: dict[str, Any] = {
                    "UserPoolId": self.user_pool_id,
                    "Limit": 60,  # Max allowed per API call
                }
                if pagination_token:
                    params["PaginationToken"] = pagination_token

                response = self.client.list_users(**params)

                for user in response.get("Users", []):
                    # Check if user belongs to the specified org
                    user_org_id = self._get_user_attribute(user, "custom:org_id")
                    if user_org_id == org_id:
                        users.append(user)

                pagination_token = response.get("PaginationToken")
                if not pagination_token:
                    break

            # Apply pagination
            total = len(users)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_users = users[start_idx:end_idx]

            return paginated_users, total

        except ClientError as e:
            logger.error(f"Failed to list users for org {org_id}: {e}")
            raise CognitoServiceError(f"Failed to list users: {str(e)}")

    def list_groups(
        self,
        prefix: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        List Cognito groups, optionally filtered by prefix.

        Issue #226: Cognito groups represent teams.

        Args:
            prefix: Optional prefix to filter groups (e.g., "team-" or org-specific prefix)
            page: Page number (1-indexed)
            page_size: Number of items per page

        Returns:
            Tuple of (list of group data dicts, total count)

        Raises:
            CognitoServiceError: If listing fails
        """
        try:
            groups: list[dict[str, Any]] = []
            next_token = None

            while True:
                params: dict[str, Any] = {
                    "UserPoolId": self.user_pool_id,
                    "Limit": 60,  # Max allowed per API call
                }
                if next_token:
                    params["NextToken"] = next_token

                response = self.client.list_groups(**params)

                for group in response.get("Groups", []):
                    group_name = group.get("GroupName", "")
                    # Apply prefix filter if specified
                    if prefix is None or group_name.startswith(prefix):
                        groups.append(group)

                next_token = response.get("NextToken")
                if not next_token:
                    break

            # Apply pagination
            total = len(groups)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_groups = groups[start_idx:end_idx]

            return paginated_groups, total

        except ClientError as e:
            logger.error(f"Failed to list groups: {e}")
            raise CognitoServiceError(f"Failed to list groups: {str(e)}")

    def get_unique_departments(
        self,
        org_id: str,
    ) -> list[str]:
        """
        Get unique department IDs from users in an organization.

        Issue #226: Departments are derived from custom:department_id attribute
        on users in Cognito.

        Args:
            org_id: Organization ID to filter users by

        Returns:
            List of unique department IDs

        Raises:
            CognitoServiceError: If listing fails
        """
        try:
            departments: set[str] = set()
            pagination_token = None

            while True:
                params: dict[str, Any] = {
                    "UserPoolId": self.user_pool_id,
                    "Limit": 60,
                }
                if pagination_token:
                    params["PaginationToken"] = pagination_token

                response = self.client.list_users(**params)

                for user in response.get("Users", []):
                    user_org_id = self._get_user_attribute(user, "custom:org_id")
                    if user_org_id == org_id:
                        dept_id = self._get_user_attribute(user, "custom:department_id")
                        if dept_id:
                            departments.add(dept_id)

                pagination_token = response.get("PaginationToken")
                if not pagination_token:
                    break

            return sorted(list(departments))

        except ClientError as e:
            logger.error(f"Failed to get departments for org {org_id}: {e}")
            raise CognitoServiceError(f"Failed to get departments: {str(e)}")

    def get_unique_teams(
        self,
        org_id: str,
    ) -> list[str]:
        """
        Get unique team IDs from users in an organization.

        Teams are derived from custom:team_id attribute on users in Cognito.

        Args:
            org_id: Organization ID to filter users by

        Returns:
            List of unique team IDs

        Raises:
            CognitoServiceError: If listing fails
        """
        try:
            teams: set[str] = set()
            pagination_token = None

            while True:
                params: dict[str, Any] = {
                    "UserPoolId": self.user_pool_id,
                    "Limit": 60,
                }
                if pagination_token:
                    params["PaginationToken"] = pagination_token

                response = self.client.list_users(**params)

                for user in response.get("Users", []):
                    user_org_id = self._get_user_attribute(user, "custom:org_id")
                    if user_org_id == org_id:
                        team_id = self._get_user_attribute(user, "custom:team_id")
                        if team_id:
                            teams.add(team_id)

                pagination_token = response.get("PaginationToken")
                if not pagination_token:
                    break

            return sorted(list(teams))

        except ClientError as e:
            logger.error(f"Failed to get teams for org {org_id}: {e}")
            raise CognitoServiceError(f"Failed to get teams: {str(e)}")

    def _get_user_attribute(self, user: dict[str, Any], attr_name: str) -> str | None:
        """
        Extract an attribute value from a Cognito user dict.

        Args:
            user: Cognito user data dict
            attr_name: Attribute name to extract

        Returns:
            Attribute value or None if not found
        """
        attributes = user.get("Attributes", [])
        for attr in attributes:
            if attr.get("Name") == attr_name:
                return attr.get("Value")
        return None
