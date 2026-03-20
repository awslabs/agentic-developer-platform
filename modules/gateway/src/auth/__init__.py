"""
Bedrock Gateway Authentication Module

This module provides comprehensive authentication services for the Bedrock Gateway,
implementing AWS STS-based authentication, service account management, tenant resolution,
and secure token handling.

Main Components:
- AuthService: Core authentication service implementing IAuthService interface
- ServiceAccountService: CRUD operations for service accounts
- TenantResolver: Maps AWS STS identity to organization/department/team structures
- STSClient: AWS STS integration with proper error handling and retries
- TokenManager: Secure JWT token generation, validation, and storage
- Routes: FastAPI authentication endpoints
- Middleware: Request authentication and authorization middleware

User Stories Supported:
- US-1.4: Human User Authentication via AWS SSO
- US-1.5: Service Account Registration
- US-1.6: Automated Agent Authentication (M2M)
- US-9.2: Unknown Organization handling
- US-9.5: Unregistered Service Account handling
"""

from .auth_service import AuthService
from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    STSClientError,
    TokenValidationError,
)
from .middleware import auth_middleware
from .routes import router
from .schemas import (
    ServiceAccountCreate,
    ServiceAccountResponse,
    ServiceAccountUpdate,
    TenantInfo,
    TokenClaims,
)
from .service_account_service import ServiceAccountService
from .sts_client import STSClient
from .tenant_resolver import TenantResolver
from .token_manager import TokenManager

__all__ = [
    # Services
    "AuthService",
    "ServiceAccountService",
    "TenantResolver",
    "STSClient",
    "TokenManager",
    # FastAPI components
    "router",
    "auth_middleware",
    # Exceptions
    "AuthenticationError",
    "AuthorizationError",
    "TokenValidationError",
    "STSClientError",
    # Schemas
    "ServiceAccountCreate",
    "ServiceAccountUpdate",
    "ServiceAccountResponse",
    "TenantInfo",
    "TokenClaims",
]
