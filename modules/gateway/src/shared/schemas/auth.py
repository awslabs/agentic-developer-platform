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
    # Issue #3985 (A2): the caller's registered plane. Sourced from the
    # agent_registry entry for IAM callers; empty for human/JWT callers, which
    # are never internal-plane principals. Only scopes in INTERNAL_PLANE_SCOPES
    # may act on the internal plane.
    scope: str = ""
    # Issue #4131: the credential scopes this caller has actually been granted,
    # resolved server-side from the agent_registry entry. Empty for human/JWT
    # callers and for any agent that has not been granted one. This is the
    # authoritative source for credential-scope decisions — never a
    # caller-supplied header.
    credential_scopes: list[str] = []
