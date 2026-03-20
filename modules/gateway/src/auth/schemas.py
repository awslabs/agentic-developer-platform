"""
Pydantic schemas for the authentication module.

These schemas define the request/response models and internal data structures
used by the authentication services.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ServiceAccountCreate(BaseModel):
    """Schema for creating a new service account."""

    name: str = Field(..., description="Service account name", min_length=1, max_length=255)
    department_id: str = Field(..., description="Department ID", min_length=1)
    team_id: str = Field(..., description="Team ID", min_length=1)
    iam_role_arn: str = Field(..., description="IAM role ARN", min_length=1, max_length=512)

    class Config:
        json_schema_extra = {
            "example": {
                "name": "ml-training-service",
                "department_id": "dept-engineering",
                "team_id": "team-ml",
                "iam_role_arn": "arn:aws:iam::123456789012:role/ml-training-service-role",
            }
        }


class ServiceAccountUpdate(BaseModel):
    """Schema for updating an existing service account."""

    name: str | None = Field(None, description="Service account name", min_length=1, max_length=255)
    department_id: str | None = Field(None, description="Department ID", min_length=1)
    team_id: str | None = Field(None, description="Team ID", min_length=1)
    iam_role_arn: str | None = Field(None, description="IAM role ARN", min_length=1, max_length=512)

    class Config:
        json_schema_extra = {"example": {"name": "ml-training-service-v2", "team_id": "team-ml-platform"}}


class ServiceAccountResponse(BaseModel):
    """Schema for service account response."""

    id: str = Field(..., description="Service account ID")
    org_id: str = Field(..., description="Organization ID")
    name: str = Field(..., description="Service account name")
    department_id: str = Field(..., description="Department ID")
    team_id: str = Field(..., description="Team ID")
    iam_role_arn: str = Field(..., description="IAM role ARN")
    created_at: datetime = Field(..., description="Creation timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "sa-12345",
                "org_id": "org-acme",
                "name": "ml-training-service",
                "department_id": "dept-engineering",
                "team_id": "team-ml",
                "iam_role_arn": "arn:aws:iam::123456789012:role/ml-training-service-role",
                "created_at": "2024-02-12T18:30:00Z",
            }
        }


class TenantInfo(BaseModel):
    """Schema for tenant resolution information."""

    org_id: str = Field(..., description="Organization ID")
    org_name: str = Field(..., description="Organization name")
    department_id: str = Field(..., description="Department ID")
    team_id: str = Field(..., description="Team ID")
    account_type: str = Field(..., description="Account type (human or service)")
    entity_id: str = Field(..., description="User ID or Service Account ID")
    is_admin: bool = Field(default=False, description="Whether the entity has admin privileges")

    class Config:
        json_schema_extra = {
            "example": {
                "org_id": "org-acme",
                "org_name": "Acme Corp",
                "department_id": "dept-engineering",
                "team_id": "team-ml",
                "account_type": "human",
                "entity_id": "user-john-doe",
                "is_admin": False,
            }
        }


class TokenClaims(BaseModel):
    """Schema for JWT token claims."""

    sub: str = Field(..., description="Subject (entity ID)")
    org_id: str = Field(..., description="Organization ID")
    team_id: str = Field(..., description="Team ID")
    department_id: str = Field(..., description="Department ID")
    account_type: str = Field(..., description="Account type (human or service)")
    is_admin: bool = Field(default=False, description="Admin privileges")
    exp: int = Field(..., description="Expiration timestamp")
    iat: int = Field(..., description="Issued at timestamp")
    jti: str = Field(..., description="JWT ID (token hash)")

    class Config:
        json_schema_extra = {
            "example": {
                "sub": "user-john-doe",
                "org_id": "org-acme",
                "team_id": "team-ml",
                "department_id": "dept-engineering",
                "account_type": "human",
                "is_admin": False,
                "exp": 1708023000,
                "iat": 1708019400,
                "jti": "abc123...",
            }
        }


class AWSCallerIdentity(BaseModel):
    """Schema for AWS STS GetCallerIdentity response."""

    user_id: str | None = Field(None, description="AWS user ID")
    account: str = Field(..., description="AWS account ID")
    arn: str = Field(..., description="AWS resource ARN")

    class Config:
        json_schema_extra = {
            "example": {"user_id": "AIDACKCEVSQ6C2EXAMPLE", "account": "123456789012", "arn": "arn:aws:iam::123456789012:user/john.doe"}
        }


class ServiceAccountListResponse(BaseModel):
    """Schema for listing service accounts."""

    service_accounts: list[ServiceAccountResponse] = Field(..., description="List of service accounts")
    total_count: int = Field(..., description="Total number of service accounts")
    page: int = Field(default=1, description="Current page number")
    page_size: int = Field(default=50, description="Number of items per page")

    class Config:
        json_schema_extra = {
            "example": {
                "service_accounts": [
                    {
                        "id": "sa-12345",
                        "org_id": "org-acme",
                        "name": "ml-training-service",
                        "department_id": "dept-engineering",
                        "team_id": "team-ml",
                        "iam_role_arn": "arn:aws:iam::123456789012:role/ml-training-service-role",
                        "created_at": "2024-02-12T18:30:00Z",
                    }
                ],
                "total_count": 1,
                "page": 1,
                "page_size": 50,
            }
        }
