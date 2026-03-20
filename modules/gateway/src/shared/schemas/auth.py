from datetime import datetime

from pydantic import BaseModel


class AuthExchangeRequest(BaseModel):
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str


class AuthExchangeResponse(BaseModel):
    token: str
    expires_at: datetime
    user_id: str
    org_id: str
    team_id: str
    department_id: str
    account_type: str  # "human" or "service"


class TokenContext(BaseModel):
    """Attached to every authenticated request after token validation."""

    user_id: str
    org_id: str
    team_id: str
    department_id: str
    account_type: str  # "human" or "service"
    is_admin: bool = False
    expires_at: datetime
    auth_source: str = "jwt"  # "jwt" (Cognito) or "iam" (API Gateway)
