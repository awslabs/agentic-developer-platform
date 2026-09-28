"""SSO service — manages Cognito identity provider federation.

Supports SAML and OIDC identity providers per organization.
Each org gets a dedicated Cognito identity provider named 'org-{org_id}'.
"""

import logging
import uuid

import boto3
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


class SSOServiceError(Exception):
    """Raised when Cognito IdP operations fail."""


class SSOService:
    """Manage Cognito SAML/OIDC identity provider federation."""

    def __init__(self) -> None:
        self._client = boto3.client(
            "cognito-idp",
            region_name=settings.aws_region,
        )
        self._user_pool_id = settings.cognito_user_pool_id

    def _provider_name(self, org_id: uuid.UUID) -> str:
        """Generate a unique Cognito IdP name for an organization."""
        return f"org-{str(org_id)[:8]}"

    def create_or_update_saml_provider(
        self,
        org_id: uuid.UUID,
        metadata_url: str,
        display_name: str | None = None,
    ) -> str:
        """Create or update a SAML identity provider in the Cognito user pool.

        Args:
            org_id: Organization UUID.
            metadata_url: SAML IdP metadata URL.
            display_name: Optional human-friendly name.

        Returns:
            The Cognito provider name identifier.
        """
        provider_name = self._provider_name(org_id)
        idp_details = {"MetadataURL": metadata_url}
        attribute_mapping = {
            "email": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
            "custom:org_id": str(org_id),
        }

        try:
            # Try to update first (idempotent)
            self._client.update_identity_provider(
                UserPoolId=self._user_pool_id,
                ProviderName=provider_name,
                ProviderDetails=idp_details,
                AttributeMapping=attribute_mapping,
            )
            logger.info("Updated SAML IdP '%s' for org %s", provider_name, org_id)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                # Create new provider
                try:
                    self._client.create_identity_provider(
                        UserPoolId=self._user_pool_id,
                        ProviderName=provider_name,
                        ProviderType="SAML",
                        ProviderDetails=idp_details,
                        AttributeMapping=attribute_mapping,
                        IdpIdentifiers=[str(org_id)],
                    )
                    logger.info(
                        "Created SAML IdP '%s' for org %s", provider_name, org_id
                    )
                except ClientError as create_err:
                    logger.error("Failed to create SAML IdP: %s", create_err)
                    raise SSOServiceError(
                        f"Failed to create SAML provider: {create_err}"
                    ) from create_err
            else:
                logger.error("Failed to update SAML IdP: %s", e)
                raise SSOServiceError(f"Failed to update SAML provider: {e}") from e

        # Update the app client to include this provider
        self._add_provider_to_client(provider_name)
        return provider_name

    def create_or_update_oidc_provider(
        self,
        org_id: uuid.UUID,
        issuer_url: str,
        display_name: str | None = None,
    ) -> str:
        """Create or update an OIDC identity provider in the Cognito user pool.

        Args:
            org_id: Organization UUID.
            issuer_url: OIDC issuer URL.
            display_name: Optional human-friendly name.

        Returns:
            The Cognito provider name identifier.
        """
        provider_name = self._provider_name(org_id)
        idp_details = {
            "iss": issuer_url,
            "authorize_scopes": "openid email profile",
        }
        attribute_mapping = {
            "email": "email",
            "custom:org_id": str(org_id),
        }

        try:
            self._client.update_identity_provider(
                UserPoolId=self._user_pool_id,
                ProviderName=provider_name,
                ProviderDetails=idp_details,
                AttributeMapping=attribute_mapping,
            )
            logger.info("Updated OIDC IdP '%s' for org %s", provider_name, org_id)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                try:
                    self._client.create_identity_provider(
                        UserPoolId=self._user_pool_id,
                        ProviderName=provider_name,
                        ProviderType="OIDC",
                        ProviderDetails=idp_details,
                        AttributeMapping=attribute_mapping,
                        IdpIdentifiers=[str(org_id)],
                    )
                    logger.info(
                        "Created OIDC IdP '%s' for org %s", provider_name, org_id
                    )
                except ClientError as create_err:
                    logger.error("Failed to create OIDC IdP: %s", create_err)
                    raise SSOServiceError(
                        f"Failed to create OIDC provider: {create_err}"
                    ) from create_err
            else:
                logger.error("Failed to update OIDC IdP: %s", e)
                raise SSOServiceError(f"Failed to update OIDC provider: {e}") from e

        self._add_provider_to_client(provider_name)
        return provider_name

    def delete_provider(self, org_id: uuid.UUID) -> None:
        """Remove the identity provider for an organization."""
        provider_name = self._provider_name(org_id)
        try:
            self._client.delete_identity_provider(
                UserPoolId=self._user_pool_id,
                ProviderName=provider_name,
            )
            logger.info("Deleted IdP '%s' for org %s", provider_name, org_id)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.error("Failed to delete IdP: %s", e)
                raise SSOServiceError(f"Failed to delete provider: {e}") from e
            # Provider doesn't exist — nothing to delete
            logger.info("IdP '%s' not found (already deleted)", provider_name)

    def get_provider(self, org_id: uuid.UUID) -> dict | None:
        """Describe the identity provider for an organization.

        Returns None if no provider exists.
        """
        provider_name = self._provider_name(org_id)
        try:
            response = self._client.describe_identity_provider(
                UserPoolId=self._user_pool_id,
                ProviderName=provider_name,
            )
            return response.get("IdentityProvider", {})
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise SSOServiceError(f"Failed to describe provider: {e}") from e

    def _add_provider_to_client(self, provider_name: str) -> None:
        """Add the identity provider to the CLI app client's supported providers list."""
        if not settings.cognito_app_client_id:
            logger.warning(
                "No Cognito app client ID configured; skipping provider registration"
            )
            return

        try:
            response = self._client.describe_user_pool_client(
                UserPoolId=self._user_pool_id,
                ClientId=settings.cognito_app_client_id,
            )
            client_config = response["UserPoolClient"]
            providers = client_config.get("SupportedIdentityProviders", ["COGNITO"])

            if provider_name not in providers:
                providers.append(provider_name)
                self._client.update_user_pool_client(
                    UserPoolId=self._user_pool_id,
                    ClientId=settings.cognito_app_client_id,
                    SupportedIdentityProviders=providers,
                    AllowedOAuthFlows=client_config.get("AllowedOAuthFlows", []),
                    AllowedOAuthScopes=client_config.get("AllowedOAuthScopes", []),
                    AllowedOAuthFlowsUserPoolClient=client_config.get(
                        "AllowedOAuthFlowsUserPoolClient", True
                    ),
                    CallbackURLs=client_config.get("CallbackURLs", []),
                    LogoutURLs=client_config.get("LogoutURLs", []),
                )
                logger.info(
                    "Added '%s' to CLI app client supported providers", provider_name
                )
        except ClientError as e:
            logger.error("Failed to update app client providers: %s", e)
            raise SSOServiceError(f"Failed to update app client: {e}") from e
