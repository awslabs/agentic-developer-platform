"""Unit tests for Cognito service."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.admin.cognito_service import (
    CognitoService,
    GroupNotFoundError,
    UserAlreadyExistsError,
    UserNotFoundError,
)


@pytest.fixture
def mock_cognito_client():
    """Create a mock Cognito client."""
    with patch("boto3.client") as mock_client:
        mock_instance = MagicMock()
        mock_client.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def cognito_service(mock_cognito_client):
    """Create a CognitoService instance with mocked client."""
    service = CognitoService(user_pool_id="us-east-1_testpool", region="us-east-1")
    service._client = mock_cognito_client
    return service


class TestCreateUser:
    """Tests for create_user method."""

    def test_create_user_success(self, cognito_service, mock_cognito_client):
        """Test successful user creation."""
        mock_cognito_client.admin_create_user.return_value = {
            "User": {
                "Username": "test@example.com",
                "Attributes": [
                    {"Name": "email", "Value": "test@example.com"},
                    {"Name": "custom:org_id", "Value": "org-123"},
                ],
                "UserStatus": "FORCE_CHANGE_PASSWORD",
            }
        }

        result = cognito_service.create_user(
            email="test@example.com",
            org_id="org-123",
            dept_id="dept-456",
            team_id="team-789",
            name="Test User",
            role="user",
        )

        assert result["Username"] == "test@example.com"
        mock_cognito_client.admin_create_user.assert_called_once()

        # Verify correct attributes were passed
        call_args = mock_cognito_client.admin_create_user.call_args
        assert call_args.kwargs["UserPoolId"] == "us-east-1_testpool"
        assert call_args.kwargs["Username"] == "test@example.com"

    def test_create_user_already_exists(self, cognito_service, mock_cognito_client):
        """Test error when user already exists."""
        error_response = {
            "Error": {
                "Code": "UsernameExistsException",
                "Message": "User already exists",
            }
        }
        mock_cognito_client.admin_create_user.side_effect = ClientError(error_response, "AdminCreateUser")

        with pytest.raises(UserAlreadyExistsError) as exc_info:
            cognito_service.create_user(
                email="existing@example.com",
                org_id="org-123",
                dept_id="dept-456",
                team_id="team-789",
            )

        assert "existing@example.com" in str(exc_info.value)

    def test_create_user_suppressed_invitation(self, cognito_service, mock_cognito_client):
        """Test creating user with suppressed invitation email."""
        mock_cognito_client.admin_create_user.return_value = {"User": {"Username": "test@example.com"}}

        cognito_service.create_user(
            email="test@example.com",
            org_id="org-123",
            dept_id="dept-456",
            team_id="team-789",
            suppress_invitation=True,
        )

        call_args = mock_cognito_client.admin_create_user.call_args
        assert call_args.kwargs["MessageAction"] == "SUPPRESS"


class TestDeleteUser:
    """Tests for delete_user method."""

    def test_delete_user_success(self, cognito_service, mock_cognito_client):
        """Test successful user deletion."""
        mock_cognito_client.admin_delete_user.return_value = {}

        result = cognito_service.delete_user(username="test@example.com")

        assert result is True
        mock_cognito_client.admin_delete_user.assert_called_once_with(
            UserPoolId="us-east-1_testpool",
            Username="test@example.com",
        )

    def test_delete_user_not_found(self, cognito_service, mock_cognito_client):
        """Test error when user not found."""
        error_response = {
            "Error": {
                "Code": "UserNotFoundException",
                "Message": "User not found",
            }
        }
        mock_cognito_client.admin_delete_user.side_effect = ClientError(error_response, "AdminDeleteUser")

        with pytest.raises(UserNotFoundError) as exc_info:
            cognito_service.delete_user(username="nonexistent@example.com")

        assert "nonexistent@example.com" in str(exc_info.value)

    def test_delete_user_by_sub(self, cognito_service, mock_cognito_client):
        """Test deleting user by Cognito sub."""
        # Mock list_users to find user by sub
        mock_cognito_client.list_users.return_value = {"Users": [{"Username": "test@example.com"}]}
        mock_cognito_client.admin_delete_user.return_value = {}

        result = cognito_service.delete_user(cognito_sub="sub-123")

        assert result is True


class TestAddUserToGroup:
    """Tests for add_user_to_group method."""

    def test_add_user_to_group_success(self, cognito_service, mock_cognito_client):
        """Test successful addition of user to group."""
        mock_cognito_client.admin_add_user_to_group.return_value = {}

        result = cognito_service.add_user_to_group("test@example.com", "org-123")

        assert result is True
        mock_cognito_client.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="us-east-1_testpool",
            Username="test@example.com",
            GroupName="org-123",
        )

    def test_add_user_to_group_user_not_found(self, cognito_service, mock_cognito_client):
        """Test error when user not found."""
        error_response = {
            "Error": {
                "Code": "UserNotFoundException",
                "Message": "User not found",
            }
        }
        mock_cognito_client.admin_add_user_to_group.side_effect = ClientError(error_response, "AdminAddUserToGroup")

        with pytest.raises(UserNotFoundError):
            cognito_service.add_user_to_group("nonexistent@example.com", "org-123")

    def test_add_user_to_group_group_not_found(self, cognito_service, mock_cognito_client):
        """Test error when group not found."""
        error_response = {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Group not found",
            }
        }
        mock_cognito_client.admin_add_user_to_group.side_effect = ClientError(error_response, "AdminAddUserToGroup")

        with pytest.raises(GroupNotFoundError):
            cognito_service.add_user_to_group("test@example.com", "nonexistent-group")


class TestCreateOrgGroup:
    """Tests for create_org_group method."""

    def test_create_org_group_success(self, cognito_service, mock_cognito_client):
        """Test successful organization group creation."""
        mock_cognito_client.create_group.return_value = {
            "Group": {
                "GroupName": "org-123",
                "UserPoolId": "us-east-1_testpool",
            }
        }

        result = cognito_service.create_org_group("123")

        assert result["GroupName"] == "org-123"
        mock_cognito_client.create_group.assert_called_once()

        call_args = mock_cognito_client.create_group.call_args
        assert call_args.kwargs["GroupName"] == "org-123"

    def test_create_org_group_already_exists(self, cognito_service, mock_cognito_client):
        """Test handling of existing group."""
        error_response = {
            "Error": {
                "Code": "GroupExistsException",
                "Message": "Group already exists",
            }
        }
        mock_cognito_client.create_group.side_effect = ClientError(error_response, "CreateGroup")

        # When group exists, should try to get the existing group
        mock_cognito_client.get_group.return_value = {"Group": {"GroupName": "org-123"}}

        result = cognito_service.create_org_group("123")

        assert result["GroupName"] == "org-123"


class TestDeleteOrgGroup:
    """Tests for delete_org_group method."""

    def test_delete_org_group_success(self, cognito_service, mock_cognito_client):
        """Test successful organization group deletion."""
        mock_cognito_client.delete_group.return_value = {}

        result = cognito_service.delete_org_group("123")

        assert result is True
        mock_cognito_client.delete_group.assert_called_once_with(
            GroupName="org-123",
            UserPoolId="us-east-1_testpool",
        )

    def test_delete_org_group_not_found(self, cognito_service, mock_cognito_client):
        """Test error when group not found."""
        error_response = {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Group not found",
            }
        }
        mock_cognito_client.delete_group.side_effect = ClientError(error_response, "DeleteGroup")

        with pytest.raises(GroupNotFoundError):
            cognito_service.delete_org_group("nonexistent")


class TestGetUser:
    """Tests for get_user method."""

    def test_get_user_success(self, cognito_service, mock_cognito_client):
        """Test successful user retrieval."""
        mock_cognito_client.admin_get_user.return_value = {
            "Username": "test@example.com",
            "UserAttributes": [
                {"Name": "email", "Value": "test@example.com"},
            ],
        }

        result = cognito_service.get_user("test@example.com")

        assert result["Username"] == "test@example.com"

    def test_get_user_not_found(self, cognito_service, mock_cognito_client):
        """Test handling of non-existent user."""
        error_response = {
            "Error": {
                "Code": "UserNotFoundException",
                "Message": "User not found",
            }
        }
        mock_cognito_client.admin_get_user.side_effect = ClientError(error_response, "AdminGetUser")

        result = cognito_service.get_user("nonexistent@example.com")

        assert result is None


class TestListUsersInGroup:
    """Tests for list_users_in_group method."""

    def test_list_users_in_group_success(self, cognito_service, mock_cognito_client):
        """Test successful listing of users in group."""
        # Mock the paginator
        mock_paginator = MagicMock()
        mock_cognito_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Users": [
                    {"Username": "user1@example.com"},
                    {"Username": "user2@example.com"},
                ]
            }
        ]

        result = cognito_service.list_users_in_group("org-123")

        assert len(result) == 2
        assert result[0]["Username"] == "user1@example.com"

    def test_list_users_in_group_not_found(self, cognito_service, mock_cognito_client):
        """Test error when group not found."""
        mock_paginator = MagicMock()
        mock_cognito_client.get_paginator.return_value = mock_paginator

        error_response = {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Group not found",
            }
        }
        mock_paginator.paginate.side_effect = ClientError(error_response, "ListUsersInGroup")

        with pytest.raises(GroupNotFoundError):
            cognito_service.list_users_in_group("nonexistent")


class TestUpdateUserAttributes:
    """Tests for update_user_attributes method."""

    def test_update_user_attributes_success(self, cognito_service, mock_cognito_client):
        """Test successful attribute update."""
        mock_cognito_client.admin_update_user_attributes.return_value = {}

        result = cognito_service.update_user_attributes(
            "test@example.com",
            {"custom:role": "admin"},
        )

        assert result is True
        mock_cognito_client.admin_update_user_attributes.assert_called_once()

    def test_update_user_attributes_user_not_found(self, cognito_service, mock_cognito_client):
        """Test error when user not found."""
        error_response = {
            "Error": {
                "Code": "UserNotFoundException",
                "Message": "User not found",
            }
        }
        mock_cognito_client.admin_update_user_attributes.side_effect = ClientError(error_response, "AdminUpdateUserAttributes")

        with pytest.raises(UserNotFoundError):
            cognito_service.update_user_attributes("nonexistent@example.com", {"custom:role": "admin"})


class TestServiceInitialization:
    """Tests for service initialization."""

    def test_init_with_env_vars(self):
        """Test initialization using environment variables."""
        with patch.dict("os.environ", {"COGNITO_USER_POOL_ID": "env-pool-id", "AWS_REGION": "eu-west-1"}):
            service = CognitoService()
            assert service.user_pool_id == "env-pool-id"
            assert service.region == "eu-west-1"

    def test_init_with_explicit_values(self):
        """Test initialization with explicit values."""
        service = CognitoService(user_pool_id="explicit-pool", region="ap-southeast-1")
        assert service.user_pool_id == "explicit-pool"
        assert service.region == "ap-southeast-1"

    def test_lazy_client_initialization(self, mock_cognito_client):
        """Test that client is lazily initialized."""
        service = CognitoService(user_pool_id="test-pool")
        assert service._client is None

        # Accessing client property should initialize it
        _ = service.client
        # The client should be initialized (mock_cognito_client fixture patches boto3.client)


# =============================================================================
# Issue #226: Tests for Cognito as Source of Truth Methods
# =============================================================================


class TestListUsersByOrg:
    """Tests for list_users_by_org method (Issue #226)."""

    def test_list_users_by_org_success(self, cognito_service, mock_cognito_client):
        """Test successful listing of users by org."""
        mock_cognito_client.list_users.return_value = {
            "Users": [
                {
                    "Username": "user1@example.com",
                    "Attributes": [
                        {"Name": "email", "Value": "user1@example.com"},
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "dept-001"},
                    ],
                },
                {
                    "Username": "user2@example.com",
                    "Attributes": [
                        {"Name": "email", "Value": "user2@example.com"},
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "dept-002"},
                    ],
                },
                {
                    "Username": "user3@example.com",
                    "Attributes": [
                        {"Name": "email", "Value": "user3@example.com"},
                        {"Name": "custom:org_id", "Value": "org-456"},  # Different org
                        {"Name": "custom:department_id", "Value": "dept-003"},
                    ],
                },
            ]
        }

        users, total = cognito_service.list_users_by_org("org-123")

        assert total == 2
        assert len(users) == 2
        assert users[0]["Username"] == "user1@example.com"
        assert users[1]["Username"] == "user2@example.com"

    def test_list_users_by_org_with_pagination(self, cognito_service, mock_cognito_client):
        """Test pagination of users by org."""
        mock_cognito_client.list_users.return_value = {
            "Users": [
                {
                    "Username": f"user{i}@example.com",
                    "Attributes": [
                        {"Name": "email", "Value": f"user{i}@example.com"},
                        {"Name": "custom:org_id", "Value": "org-123"},
                    ],
                }
                for i in range(5)
            ]
        }

        users, total = cognito_service.list_users_by_org("org-123", page=1, page_size=2)

        assert total == 5
        assert len(users) == 2
        assert users[0]["Username"] == "user0@example.com"
        assert users[1]["Username"] == "user1@example.com"

    def test_list_users_by_org_empty(self, cognito_service, mock_cognito_client):
        """Test listing users when none match the org."""
        mock_cognito_client.list_users.return_value = {
            "Users": [
                {
                    "Username": "user@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "other-org"},
                    ],
                }
            ]
        }

        users, total = cognito_service.list_users_by_org("org-123")

        assert total == 0
        assert len(users) == 0

    def test_list_users_by_org_with_cognito_pagination(self, cognito_service, mock_cognito_client):
        """Test handling of Cognito's pagination token."""
        # First call returns pagination token
        mock_cognito_client.list_users.side_effect = [
            {
                "Users": [
                    {
                        "Username": "user1@example.com",
                        "Attributes": [{"Name": "custom:org_id", "Value": "org-123"}],
                    }
                ],
                "PaginationToken": "token123",
            },
            {
                "Users": [
                    {
                        "Username": "user2@example.com",
                        "Attributes": [{"Name": "custom:org_id", "Value": "org-123"}],
                    }
                ],
            },
        ]

        users, total = cognito_service.list_users_by_org("org-123")

        assert total == 2
        assert len(users) == 2
        assert mock_cognito_client.list_users.call_count == 2


class TestListGroups:
    """Tests for list_groups method (Issue #226)."""

    def test_list_groups_success(self, cognito_service, mock_cognito_client):
        """Test successful listing of groups."""
        mock_cognito_client.list_groups.return_value = {
            "Groups": [
                {"GroupName": "team-platform", "Description": "Platform Team"},
                {"GroupName": "team-devops", "Description": "DevOps Team"},
                {"GroupName": "org-123", "Description": "Organization Group"},
            ]
        }

        groups, total = cognito_service.list_groups()

        assert total == 3
        assert len(groups) == 3
        assert groups[0]["GroupName"] == "team-platform"

    def test_list_groups_with_prefix(self, cognito_service, mock_cognito_client):
        """Test listing groups with prefix filter."""
        mock_cognito_client.list_groups.return_value = {
            "Groups": [
                {"GroupName": "team-platform", "Description": "Platform Team"},
                {"GroupName": "team-devops", "Description": "DevOps Team"},
                {"GroupName": "org-123", "Description": "Organization Group"},
            ]
        }

        groups, total = cognito_service.list_groups(prefix="team-")

        assert total == 2
        assert len(groups) == 2
        assert all(g["GroupName"].startswith("team-") for g in groups)

    def test_list_groups_with_pagination(self, cognito_service, mock_cognito_client):
        """Test pagination of groups."""
        mock_cognito_client.list_groups.return_value = {"Groups": [{"GroupName": f"team-{i}", "Description": f"Team {i}"} for i in range(5)]}

        groups, total = cognito_service.list_groups(page=2, page_size=2)

        assert total == 5
        assert len(groups) == 2
        assert groups[0]["GroupName"] == "team-2"
        assert groups[1]["GroupName"] == "team-3"

    def test_list_groups_empty(self, cognito_service, mock_cognito_client):
        """Test listing groups when none exist."""
        mock_cognito_client.list_groups.return_value = {"Groups": []}

        groups, total = cognito_service.list_groups()

        assert total == 0
        assert len(groups) == 0


class TestGetUniqueDepartments:
    """Tests for get_unique_departments method (Issue #226)."""

    def test_get_unique_departments_success(self, cognito_service, mock_cognito_client):
        """Test successful retrieval of unique departments."""
        mock_cognito_client.list_users.return_value = {
            "Users": [
                {
                    "Username": "user1@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "engineering"},
                    ],
                },
                {
                    "Username": "user2@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "engineering"},  # Duplicate
                    ],
                },
                {
                    "Username": "user3@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "sales"},
                    ],
                },
                {
                    "Username": "user4@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "org-456"},  # Different org
                        {"Name": "custom:department_id", "Value": "marketing"},
                    ],
                },
            ]
        }

        departments = cognito_service.get_unique_departments("org-123")

        assert len(departments) == 2
        assert "engineering" in departments
        assert "sales" in departments
        assert "marketing" not in departments  # Different org

    def test_get_unique_departments_empty(self, cognito_service, mock_cognito_client):
        """Test getting departments when no users exist for org."""
        mock_cognito_client.list_users.return_value = {"Users": []}

        departments = cognito_service.get_unique_departments("org-123")

        assert len(departments) == 0

    def test_get_unique_departments_sorted(self, cognito_service, mock_cognito_client):
        """Test that departments are returned sorted."""
        mock_cognito_client.list_users.return_value = {
            "Users": [
                {
                    "Username": "user1@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "zebra"},
                    ],
                },
                {
                    "Username": "user2@example.com",
                    "Attributes": [
                        {"Name": "custom:org_id", "Value": "org-123"},
                        {"Name": "custom:department_id", "Value": "alpha"},
                    ],
                },
            ]
        }

        departments = cognito_service.get_unique_departments("org-123")

        assert departments == ["alpha", "zebra"]


class TestGetUserAttribute:
    """Tests for _get_user_attribute helper method."""

    def test_get_user_attribute_found(self, cognito_service):
        """Test extracting an existing attribute."""
        user = {
            "Username": "test@example.com",
            "Attributes": [
                {"Name": "email", "Value": "test@example.com"},
                {"Name": "custom:org_id", "Value": "org-123"},
            ],
        }

        result = cognito_service._get_user_attribute(user, "custom:org_id")
        assert result == "org-123"

    def test_get_user_attribute_not_found(self, cognito_service):
        """Test extracting a non-existent attribute."""
        user = {
            "Username": "test@example.com",
            "Attributes": [
                {"Name": "email", "Value": "test@example.com"},
            ],
        }

        result = cognito_service._get_user_attribute(user, "custom:org_id")
        assert result is None

    def test_get_user_attribute_empty_attributes(self, cognito_service):
        """Test extracting from user with no attributes."""
        user = {"Username": "test@example.com"}

        result = cognito_service._get_user_attribute(user, "custom:org_id")
        assert result is None
