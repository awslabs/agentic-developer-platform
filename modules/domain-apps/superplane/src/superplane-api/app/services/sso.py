"""SSO service — manages Cognito identity provider federation.

Supports SAML and OIDC identity providers per organization.
Each org gets a dedicated Cognito identity provider named 'org-{org_id}'.
"""

import json
import logging
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from app.config import settings
from app.schemas.org import sso_https_origin

logger = logging.getLogger(__name__)

_CLIENT_MUTABLE_FIELDS = (
    "ClientName", "ExplicitAuthFlows", "RefreshTokenValidity", "AccessTokenValidity",
    "IdTokenValidity", "TokenValidityUnits", "ReadAttributes", "WriteAttributes",
    "AllowedOAuthFlows", "AllowedOAuthScopes", "AllowedOAuthFlowsUserPoolClient",
    "CallbackURLs", "LogoutURLs", "DefaultRedirectURI", "AnalyticsConfiguration",
    "PreventUserExistenceErrors", "EnableTokenRevocation", "EnablePropagateAdditionalUserContextData",
    "AuthSessionValidity", "RefreshTokenRotation",
)
_PROTECTED_ATTRIBUTES = frozenset({"org_id", "role", "team_id", "department_id"})


class SSOServiceError(Exception):
    """Raised when Cognito IdP operations fail."""


class SSOOwnershipConflict(Exception):
    """Provider ownership has not been independently established."""


class SSOOriginPolicyUnavailable(Exception):
    """Operator-owned SSO origin policy is missing or invalid."""


class SSOOriginNotApproved(Exception):
    """The verified organization's policy does not approve this origin."""


def require_approved_sso_origin(org_id: uuid.UUID, metadata_url: str) -> None:
    origin = sso_https_origin(metadata_url)
    path = settings.superplane_sso_approved_origins_file
    if not path:
        raise SSOOriginPolicyUnavailable()
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        policy = json.loads(raw)
        if policy["version"] != 1 or not isinstance(policy["tenants"], dict):
            raise ValueError()
        allowed = policy["tenants"].get(str(org_id))
        if not isinstance(allowed, list) or not allowed or any(
            not isinstance(item, str) or item != sso_https_origin(item)
            for item in allowed
        ):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise SSOOriginPolicyUnavailable() from None
    if origin not in allowed:
        raise SSOOriginNotApproved()


class SSOService:
    """Manage Cognito SAML/OIDC identity provider federation."""

    def __init__(self) -> None:
        self._client = boto3.client(
            "cognito-idp",
            region_name=settings.aws_region,
        )
        self._user_pool_id = settings.cognito_user_pool_id

    def _provider_name(self, org_id: uuid.UUID) -> str:
        return f"org-{org_id}"

    def _describe(self, provider_name: str) -> dict | None:
        try:
            response = self._client.describe_identity_provider(
                UserPoolId=self._user_pool_id, ProviderName=provider_name
            )
            return response.get("IdentityProvider")
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise SSOServiceError("Failed to describe SSO provider") from error

    def preflight_new_provider(self, org_id: uuid.UUID) -> str:
        provider_name = self._provider_name(org_id)
        if self._describe(provider_name) is not None:
            raise SSOOwnershipConflict()
        return provider_name

    def _verified_provider(
        self, org_id: uuid.UUID, provider_name: str, provider_type: str, *, newly_created: bool = False
    ) -> dict:
        provider = self._describe(provider_name)
        if not isinstance(provider, dict) or provider.get("ProviderName") != provider_name or (
            provider.get("ProviderType") != provider_type
        ):
            raise SSOOwnershipConflict()
        identifiers = provider.get("IdpIdentifiers")
        if identifiers and identifiers != [str(org_id)]:
            raise SSOOwnershipConflict()
        if newly_created and identifiers != [str(org_id)]:
            raise SSOOwnershipConflict()
        return provider

    def _upsert_provider(
        self,
        org_id: uuid.UUID,
        provider_type: str,
        details: dict,
        mapping: dict,
        *,
        bound_provider_name: str | None,
        enabled: bool,
    ) -> str:
        provider_name = bound_provider_name or self._provider_name(org_id)
        existing = self._describe(provider_name)
        if existing is not None:
            if not bound_provider_name:
                raise SSOOwnershipConflict()
            self._verified_provider(org_id, provider_name, provider_type)
        elif bound_provider_name and bound_provider_name != self._provider_name(org_id):
            raise SSOOwnershipConflict()
        try:
            if existing is not None:
                self._client.update_identity_provider(
                    UserPoolId=self._user_pool_id,
                    ProviderName=provider_name,
                    ProviderDetails=details,
                    AttributeMapping=mapping,
                )
            else:
                self._client.create_identity_provider(
                    UserPoolId=self._user_pool_id,
                    ProviderName=provider_name,
                    ProviderType=provider_type,
                    ProviderDetails=details,
                    AttributeMapping=mapping,
                    IdpIdentifiers=[str(org_id)],
                )
        except ClientError as error:
            raise SSOServiceError("Failed to configure SSO provider") from error
        self._verified_provider(
            org_id, provider_name, provider_type, newly_created=existing is None
        )
        self._reconcile_client_provider(provider_name, enabled=enabled)
        return provider_name

    def create_or_update_saml_provider(
        self,
        org_id: uuid.UUID,
        metadata_url: str,
        display_name: str | None = None,
        *,
        bound_provider_name: str | None = None,
        enabled: bool = True,
    ) -> str:
        require_approved_sso_origin(org_id, metadata_url)
        return self._upsert_provider(
            org_id,
            "SAML",
            {"MetadataURL": metadata_url},
            {"email": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"},
            bound_provider_name=bound_provider_name,
            enabled=enabled,
        )

    def create_or_update_oidc_provider(
        self,
        org_id: uuid.UUID,
        issuer_url: str,
        display_name: str | None = None,
        *,
        bound_provider_name: str | None = None,
        enabled: bool = True,
    ) -> str:
        require_approved_sso_origin(org_id, issuer_url)
        return self._upsert_provider(
            org_id,
            "OIDC",
            {"iss": issuer_url, "authorize_scopes": "openid email profile"},
            {"email": "email"},
            bound_provider_name=bound_provider_name,
            enabled=enabled,
        )

    def get_provider(
        self, org_id: uuid.UUID, provider_name: str, provider_type: str, *, missing_ok: bool = False
    ) -> dict | None:
        if missing_ok and self._describe(provider_name) is None:
            return None
        return self._verified_provider(org_id, provider_name, provider_type)

    def delete_provider(self, org_id: uuid.UUID, provider_name: str, provider_type: str) -> None:
        if self._describe(provider_name) is None:
            return
        self._verified_provider(org_id, provider_name, provider_type)
        try:
            self._client.delete_identity_provider(
                UserPoolId=self._user_pool_id, ProviderName=provider_name
            )
        except ClientError as error:
            raise SSOServiceError("Failed to delete SSO provider") from error

    def _client_config(self) -> dict:
        if not settings.cognito_app_client_id:
            raise SSOServiceError("SSO app client is unavailable")
        try:
            response = self._client.describe_user_pool_client(
                UserPoolId=self._user_pool_id, ClientId=settings.cognito_app_client_id
            )
            config = response["UserPoolClient"]
        except (ClientError, KeyError, TypeError) as error:
            raise SSOServiceError("SSO app client is unavailable") from error
        if not isinstance(config, dict) or not isinstance(config.get("SupportedIdentityProviders"), list):
            raise SSOServiceError("SSO app client configuration is incomplete")
        return config

    def _reconcile_client_provider(self, provider_name: str, *, enabled: bool) -> None:
        config = self._client_config()
        for required in (
            "WriteAttributes", "ReadAttributes", "ExplicitAuthFlows", "EnableTokenRevocation",
            "RefreshTokenRotation", "TokenValidityUnits",
        ):
            if required not in config:
                raise SSOServiceError("SSO app client configuration is incomplete")
        writes = config["WriteAttributes"]
        if not isinstance(writes, list) or not writes or not all(isinstance(item, str) for item in writes):
            raise SSOServiceError("SSO app client write attributes are unavailable")
        safe_writes = [
            item for item in writes
            if item != "*" and item.removeprefix("custom:").lower() not in _PROTECTED_ATTRIBUTES
        ]
        if not safe_writes:
            raise SSOServiceError("SSO app client write attributes are unavailable")
        providers = config["SupportedIdentityProviders"]
        selected = list(dict.fromkeys([*providers, provider_name])) if enabled else [
            provider for provider in providers if provider != provider_name
        ]
        expected = {key: config[key] for key in _CLIENT_MUTABLE_FIELDS if key in config}
        expected.update(SupportedIdentityProviders=selected, WriteAttributes=safe_writes)
        if providers != selected or writes != safe_writes:
            try:
                self._client.update_user_pool_client(
                    UserPoolId=self._user_pool_id, ClientId=settings.cognito_app_client_id, **expected
                )
            except ClientError as error:
                raise SSOServiceError("Failed to update SSO app client") from error
        observed = self._client_config()
        if any(observed.get(key) != value for key, value in expected.items()):
            raise SSOServiceError("SSO app client update could not be verified")

    def remove_provider_from_client(self, provider_name: str) -> None:
        self._reconcile_client_provider(provider_name, enabled=False)
