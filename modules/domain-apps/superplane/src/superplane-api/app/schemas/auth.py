"""Pydantic schemas for auth endpoints."""

import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """POST /auth/login — exchange API key for JWT."""

    api_key: str = Field(..., description="API key (sp_...)")


class LoginResponse(BaseModel):
    """JWT token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")


class CreateApiKeyRequest(BaseModel):
    """POST /auth/token — create a new API key."""

    name: str = Field(
        ..., min_length=1, max_length=255, description="Friendly name for the key"
    )


class CreateApiKeyResponse(BaseModel):
    """Returns the raw API key (shown only once)."""

    id: uuid.UUID
    name: str
    key: str = Field(description="Full API key — store securely, shown only once")
    key_prefix: str


class SignupRequest(BaseModel):
    """POST /auth/signup — create a new user + org."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(
        ..., min_length=8, max_length=128, description="User password (min 8 chars)"
    )
    org_name: str = Field(
        ..., min_length=1, max_length=255, description="Organization name"
    )


class SignupResponse(BaseModel):
    """Signup result — JWT + org details."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")
    org_id: uuid.UUID
    org_name: str
    default_workspace_id: uuid.UUID


class TokenPayload(BaseModel):
    """Decoded JWT token payload."""

    sub: str  # org_id
    org_id: uuid.UUID
    exp: int
    user_id: uuid.UUID | None = None
    role: str | None = None
