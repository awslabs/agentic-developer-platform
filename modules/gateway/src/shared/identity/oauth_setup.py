"""Deployment-owned OAuth app registration and metadata; never returns credentials."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.shared.config import get_settings
from src.shared.identity.oauth_providers import PROVIDER_SCOPES
from src.shared.identity.slack_setup import AUTOMATION, SLACK_USER_METADATA, automate_slack, slack_manifest

PROVIDER_APPS = {
    "github": ("GithubOauth2", "https://github.com/settings/developers"),
    "atlassian": ("AtlassianOauth2", "https://developer.atlassian.com/console/myapps/"),
    "slack": ("CustomOauth2", "https://api.slack.com/apps"),
}

ATLASSIAN_MCP_RETURN_PATH = "/api/auth/connections/atlassian-mcp/return"
ATLASSIAN_MCP_CONFIG_KEYS = frozenset(
    {
        "resource",
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "client_id",
        "app_secret_ref",
        "registration_mode",
        "return_url",
        "contract_revision",
        "protected_resource_metadata",
        "authorization_server_metadata",
        "metadata_sha256",
    }
)


def atlassian_identity_metadata_matches(actual, expected):
    # Older providers may still return the deprecated field. Preserve those
    # registrations when it agrees with the explicitly verified BASIC method.
    return actual == expected or actual == {**expected, "tokenEndpointAuthMethods": ["client_secret_basic"]}


def atlassian_mcp_config(settings, secret_store):
    """Load the release-owned, nonsecret provider plan; never accept browser inputs."""
    if not settings.atlassian_config_ref or not settings.atlassian_contract_revision or not settings.atlassian_metadata_sha256:
        raise HTTPException(503, detail={"error": "atlassian_setup_unavailable"})
    try:
        stored = secret_store.get_secret_value(SecretId=settings.atlassian_config_ref)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            raise HTTPException(503, detail={"error": "atlassian_setup_unavailable"}) from None
        raise
    try:
        plan = json.loads(stored["SecretString"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(503, detail={"error": "invalid_atlassian_config"}) from None
    if not isinstance(plan, dict) or set(plan) != ATLASSIAN_MCP_CONFIG_KEYS:
        raise HTTPException(503, detail={"error": "invalid_atlassian_config"})
    documents = (plan["protected_resource_metadata"], plan["authorization_server_metadata"])
    if any(
        not isinstance(plan[key], str) or not plan[key]
        for key in ATLASSIAN_MCP_CONFIG_KEYS - {"protected_resource_metadata", "authorization_server_metadata", "client_id"}
    ) or any(not isinstance(document, dict) for document in documents):
        raise HTTPException(503, detail={"error": "invalid_atlassian_config"})
    protected, authorization = documents
    metadata_digest = hashlib.sha256(json.dumps(list(documents), sort_keys=True).encode()).hexdigest()
    issuer = urlparse(plan["issuer"])
    return_url = urlparse(plan["return_url"])
    dynamic = plan["registration_mode"] == "dynamic-client-registration"
    registration = authorization.get("registration_endpoint")
    endpoints = (plan["authorization_endpoint"], plan["token_endpoint"])
    if (
        plan["registration_mode"] not in {"retained-deployment-client", "dynamic-client-registration"}
        or (dynamic and plan["client_id"] != "")
        or (not dynamic and (not isinstance(plan["client_id"], str) or not plan["client_id"]))
        or (
            dynamic
            and (
                not isinstance(registration, str)
                or urlparse(registration).scheme != "https"
                or urlparse(registration).netloc != "auth.atlassian.com"
                or not urlparse(registration).path
                or urlparse(registration).query
                or urlparse(registration).fragment
            )
        )
        or plan["resource"] != "https://mcp.atlassian.com/v2/mcp"
        or protected.get("resource") != plan["resource"]
        or protected.get("authorization_servers") != [plan["issuer"]]
        or authorization.get("issuer") != plan["issuer"]
        or issuer.scheme != "https"
        or issuer.netloc != "auth.atlassian.com"
        or not issuer.path.strip("/")
        or issuer.query
        or issuer.fragment
        or authorization.get("authorization_endpoint") != plan["authorization_endpoint"]
        or authorization.get("token_endpoint") != plan["token_endpoint"]
        or "S256" not in authorization.get("code_challenge_methods_supported", [])
        or not {"authorization_code", "refresh_token"} <= set(authorization.get("grant_types_supported", []))
        or "client_secret_basic" not in authorization.get("token_endpoint_auth_methods_supported", [])
        or any(
            urlparse(endpoint).scheme != "https"
            or urlparse(endpoint).netloc != "auth.atlassian.com"
            or not urlparse(endpoint).path
            or urlparse(endpoint).query
            or urlparse(endpoint).fragment
            for endpoint in endpoints
        )
        or plan["metadata_sha256"] != metadata_digest
        or settings.atlassian_metadata_sha256 != metadata_digest
        or return_url.scheme != "https"
        or not return_url.hostname
        or return_url.path != ATLASSIAN_MCP_RETURN_PATH
        or return_url.username
        or return_url.password
        or return_url.query
        or return_url.fragment
        or plan["contract_revision"] != settings.atlassian_contract_revision
        or not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+", plan["app_secret_ref"])
        or plan["app_secret_ref"].split(":")[3] != settings.aws_region
    ):
        raise HTTPException(503, detail={"error": "invalid_atlassian_config"})
    return plan


def identity_config(settings):
    parsed = urlparse(settings.oauth_return_url)
    if (
        re.fullmatch(r"adp-tools-[A-Za-z0-9_-]+-oauth-user", settings.oauth_workload_name) is None
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("/auth/connections/oauth/return", "/api/auth/connections/oauth/return")
    ):
        raise HTTPException(503, detail={"error": "oauth_unavailable", "message": "OAuth setup is incomplete. Contact a platform administrator."})
    return settings.oauth_workload_name, settings.oauth_return_url


class OAuthAppSetup:
    def __init__(self, settings, control, secret_store=None):
        self.settings = settings
        self.control = control
        self.secret_store = secret_store

    def provider_name(self, provider):
        if provider not in PROVIDER_APPS:
            raise HTTPException(404, detail={"error": "unknown_provider"})
        workload, _ = identity_config(self.settings)
        return f"{workload.removesuffix('-oauth-user')}-{provider}-user"

    def secret_name(self, provider):
        self.provider_name(provider)
        workload_name, _ = identity_config(self.settings)
        environment = workload_name.removeprefix("adp-tools-").removesuffix("-oauth-user")
        return f"adp/{environment}/connections/oauth/{provider}"

    def automate_slack(self, token, *, before_effects):
        return automate_slack(self, token, before_effects=before_effects)

    def atlassian_mcp_status(self, *, policy_result=None):
        """Expose deployment readiness only with trusted, explicit policy evidence."""
        if policy_result not in (None, "allowed", "blocked"):
            raise HTTPException(503, detail={"error": "atlassian_policy_unavailable"})
        try:
            result = self.prepare_atlassian_mcp(before_effects=None)
        except HTTPException as exc:
            if exc.status_code == 409:
                result = {"status": "unavailable"}
            elif exc.status_code == 503:
                result = {"status": "unavailable" if exc.detail == {"error": "atlassian_registration_uncertain"} else "setup_required"}
            else:
                raise
        if result["status"] == "unavailable":
            return {
                "status": "unavailable",
                "owner": "platform_admin",
                "action": "Ask a platform administrator to reconcile the deployment configuration.",
            }
        if policy_result == "blocked":
            return {
                "status": "policy_blocked",
                "owner": "atlassian_organization_admin",
                "action": "Ask the Atlassian organization administrator to allow this deployment client.",
            }
        if result["status"] == "prepared":
            return {
                "status": "consent_required",
                "owner": "user",
                "action": "Connect with Atlassian to verify your account. Tool access remains subject to policy and provider verification.",
            }
        return {
            "status": "setup_required",
            "owner": "platform_admin",
            "action": "Ask a platform administrator to complete and verify the deployment setup.",
        }

    def prepare_atlassian_mcp(self, *, before_effects, registration_lease=None, registration_post=None, recovery_only=False):
        """Prepare an isolated Identity provider from the retained deployment plan."""
        plan = atlassian_mcp_config(self.settings, self.secret_store)
        if plan["registration_mode"] == "dynamic-client-registration":
            from src.shared.identity.atlassian_client_registration import prepare_dynamic_client

            return prepare_dynamic_client(
                self, plan, before_effects=before_effects, lease_guard=registration_lease, post=registration_post, recovery_only=recovery_only
            )
        workload, _ = identity_config(self.settings)
        workload_name = f"{workload.removesuffix('-oauth-user')}-atlassian-mcp-oauth-user"
        provider_name = f"{workload.removesuffix('-oauth-user')}-atlassian-mcp-user"
        metadata = {
            "issuer": plan["issuer"],
            "authorizationEndpoint": plan["authorization_endpoint"],
            "tokenEndpoint": plan["token_endpoint"],
            "responseTypes": ["code"],
        }
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        try:
            self.secret_store.describe_secret(SecretId=plan["app_secret_ref"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                raise HTTPException(503, detail={"error": "atlassian_client_missing"}) from None
            raise
        existing_workload = self._get(self.control.get_workload_identity, name=workload_name)
        existing_provider = self._get(self.control.get_oauth2_credential_provider, name=provider_name)
        if existing_workload and existing_workload.get("allowedResourceOauth2ReturnUrls") != [plan["return_url"]]:
            raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
        if not existing_provider:
            if before_effects is None:
                return {"status": "setup_required"}
            if not existing_workload:
                before_effects()
                self.control.create_workload_identity(name=workload_name, allowedResourceOauth2ReturnUrls=[plan["return_url"]])
                existing_workload = self._get(self.control.get_workload_identity, name=workload_name)
                if not existing_workload or existing_workload.get("allowedResourceOauth2ReturnUrls") != [plan["return_url"]]:
                    raise HTTPException(503, detail={"error": "atlassian_setup_pending"})
            before_effects()
            self.control.create_oauth2_credential_provider(
                name=provider_name,
                credentialProviderVendor="CustomOauth2",
                tags={"adp-config-digest": digest},
                oauth2ProviderConfigInput={
                    "customOauth2ProviderConfig": {
                        "oauthDiscovery": {"authorizationServerMetadata": metadata},
                        "clientId": plan["client_id"],
                        "clientSecretSource": "EXTERNAL",
                        "clientSecretConfig": {"secretId": plan["app_secret_ref"], "jsonKey": "client_secret"},
                        "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
                    }
                },
            )
            existing_provider = self._get(self.control.get_oauth2_credential_provider, name=provider_name)
        if existing_provider:
            actual = existing_provider.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {})
            callback = urlparse(existing_provider.get("callbackUrl", ""))
            provider_arn = existing_provider.get("credentialProviderArn", "")
            if not isinstance(provider_arn, str) or not provider_arn.startswith(f"arn:aws:bedrock-agentcore:{self.settings.aws_region}:"):
                raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
            tags = self.control.list_tags_for_resource(resourceArn=provider_arn).get("tags", {})
            if (
                existing_provider.get("credentialProviderVendor") != "CustomOauth2"
                or tags.get("adp-config-digest") != digest
                or not atlassian_identity_metadata_matches(actual.get("oauthDiscovery", {}).get("authorizationServerMetadata"), metadata)
                or actual.get("clientId") != plan["client_id"]
                or actual.get("clientAuthenticationMethod") != "CLIENT_SECRET_BASIC"
                or existing_provider.get("clientSecretSource") != "EXTERNAL"
                or existing_provider.get("clientSecretArn") != {"secretArn": plan["app_secret_ref"]}
                or existing_provider.get("clientSecretJsonKey") != "client_secret"
                or callback.scheme != "https"
                or callback.netloc != f"bedrock-agentcore.{self.settings.aws_region}.amazonaws.com"
                or not callback.path.startswith("/identities/oauth2/callback/")
                or callback.query
                or callback.fragment
            ):
                raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
            if existing_workload and existing_provider.get("status") == "READY":
                return {"status": "prepared", "callback_url": existing_provider["callbackUrl"], "return_url": plan["return_url"]}
            raise HTTPException(503, detail={"error": "atlassian_setup_pending"})
        raise HTTPException(503, detail={"error": "atlassian_setup_pending"})

    @staticmethod
    def _get(method, **kwargs):
        try:
            return method(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"ResourceNotFoundException", "ResourceNotFound"}:
                return None
            raise

    def status(self, provider, *, admin=False):
        name = self.provider_name(provider)
        workload_name, return_url = identity_config(self.settings)
        workload = self._get(self.control.get_workload_identity, name=workload_name)
        app = self._get(self.control.get_oauth2_credential_provider, name=name)
        state = "setup_required"
        if workload and return_url not in workload.get("allowedResourceOauth2ReturnUrls", []):
            state = "deployment_incomplete"
        if app is not None:
            if app.get("credentialProviderVendor") != PROVIDER_APPS[provider][0] and not (
                provider == "slack" and app.get("credentialProviderVendor") == "SlackOauth2"
            ):
                state = "unavailable"
            elif (
                provider == "slack"
                and app.get("credentialProviderVendor") == "CustomOauth2"
                and (
                    app.get("oauth2ProviderConfigOutput", {})
                    .get("customOauth2ProviderConfig", {})
                    .get("oauthDiscovery", {})
                    .get("authorizationServerMetadata")
                    != SLACK_USER_METADATA
                )
            ):
                state = "unavailable"
            elif not workload or return_url not in workload.get("allowedResourceOauth2ReturnUrls", []):
                state = "deployment_incomplete"
            elif app.get("status") == "READY":
                state = "registered"
            else:
                state = "unavailable"
        saved = {}
        if provider == "slack" and self.secret_store:
            stored = self._get(self.secret_store.get_secret_value, SecretId=self.secret_name(provider))
            saved = json.loads(stored["SecretString"]) if stored else {}
            if not isinstance(saved, dict):
                raise ValueError("invalid Slack setup record")
            # Registration can succeed before Slack accepts the callback.
            # Keep Connect unavailable while automated setup still needs resume.
            if not admin and state == "registered" and saved.get("automation") == AUTOMATION and saved.get("stage") != "ready":
                state = "setup_required"
        result = {"provider": provider, "service": PROVIDER_SCOPES[provider][0], "status": state}
        if admin:
            result.update({"developer_url": PROVIDER_APPS[provider][1], "scopes": PROVIDER_SCOPES[provider][1]})
            if provider == "slack":
                result["manifest"] = slack_manifest()
                if saved.get("automation") == AUTOMATION:
                    result["automation_state"] = saved.get("stage", "unavailable")
                    if isinstance(saved.get("app_id"), str) and re.fullmatch(r"A[A-Z0-9]{8,63}", saved["app_id"]):
                        result["developer_url"] = f"https://api.slack.com/apps/{saved['app_id']}"
            if app:
                callback = app.get("callbackUrl", "")
                parsed = urlparse(callback)
                if (
                    parsed.scheme == "https"
                    and parsed.netloc == f"bedrock-agentcore.{self.settings.aws_region}.amazonaws.com"
                    and parsed.path.startswith("/identities/oauth2/callback/")
                    and not parsed.query
                    and not parsed.fragment
                ):
                    result["callback_url"] = callback
        return result

    def register(self, provider, client_id, client_secret, *, before_effects, replace=False, app_id=None):
        """Create a provider, or explicitly replace the existing GitHub client."""
        name = self.provider_name(provider)
        workload_name, return_url = identity_config(self.settings)
        existing = self._get(self.control.get_oauth2_credential_provider, name=name)
        if replace and (
            provider != "github"
            or not isinstance(app_id, str)
            or re.fullmatch(r"[1-9][0-9]{0,18}", app_id) is None
            or getattr(self.settings, "github_app_id", "") != app_id
        ):
            raise HTTPException(400, detail={"error": "invalid_github_app_client"})
        if existing is not None and not replace:
            raise HTTPException(409, detail={"error": "oauth_app_exists", "message": "An app is already registered. Refresh to view its setup."})
        if existing is not None and existing.get("credentialProviderVendor") != PROVIDER_APPS[provider][0]:
            raise HTTPException(409, detail={"error": "oauth_app_conflict"})
        workload = self._get(self.control.get_workload_identity, name=workload_name)
        if workload and return_url not in workload.get("allowedResourceOauth2ReturnUrls", []):
            raise HTTPException(409, detail={"error": "oauth_return_mismatch", "message": "The deployment return URL needs an operator update."})
        secret_name = self.secret_name(provider)
        # A prior interrupted attempt may have stored the secret. Resume only
        # with identical credentials; never overwrite a pre-existing secret.
        stored = self._get(self.secret_store.get_secret_value, SecretId=secret_name)
        if existing is not None and replace and stored is None:
            raise HTTPException(409, detail={"error": "oauth_secret_missing"})
        saved = None
        matches = False
        if stored:
            try:
                saved = json.loads(stored["SecretString"])
                matches = secrets.compare_digest(saved["client_id"].encode(), client_id.encode()) and secrets.compare_digest(
                    saved["client_secret"].encode(), client_secret.encode()
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                if replace:
                    raise HTTPException(409, detail={"error": "invalid_oauth_secret"}) from None
            if not matches and not replace:
                raise HTTPException(
                    409, detail={"error": "oauth_secret_exists", "message": "Stored app credentials differ. Ask an operator to reconcile this setup."}
                )
        if replace and matches and saved.get("app_id") == app_id and existing is not None:
            current = existing.get("oauth2ProviderConfigOutput", {}).get("githubOauth2ProviderConfig", {}).get("clientId")
            if current == client_id:
                return self.status(provider, admin=True)
        before_effects()
        if not workload:
            try:
                self.control.create_workload_identity(name=workload_name, allowedResourceOauth2ReturnUrls=[return_url])
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConflictException":
                    raise
                workload = self.control.get_workload_identity(name=workload_name)
                if return_url not in workload.get("allowedResourceOauth2ReturnUrls", []):
                    raise HTTPException(409, detail={"error": "oauth_return_mismatch"}) from None
        secret_value = {"client_id": client_id, "client_secret": client_secret}
        if replace:
            secret_value["app_id"] = app_id
        if not stored:
            self.secret_store.create_secret(Name=secret_name, SecretString=json.dumps(secret_value))
        elif replace:
            self.secret_store.put_secret_value(SecretId=secret_name, SecretString=json.dumps(secret_value))
        config = {
            "clientId": client_id,
            "clientSecretSource": "EXTERNAL",
            "clientSecretConfig": {"secretId": secret_name, "jsonKey": "client_secret"},
        }
        if provider == "slack":
            config.update(
                {"oauthDiscovery": {"authorizationServerMetadata": SLACK_USER_METADATA}, "clientAuthenticationMethod": "CLIENT_SECRET_BASIC"}
            )
        operation = self.control.update_oauth2_credential_provider if existing is not None else self.control.create_oauth2_credential_provider
        operation(
            name=name,
            credentialProviderVendor=PROVIDER_APPS[provider][0],
            oauth2ProviderConfigInput={f"{'custom' if provider == 'slack' else provider}Oauth2ProviderConfig": config},
        )
        return self.status(provider, admin=True)


def get_oauth_setup():
    settings = get_settings()
    config = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})
    return OAuthAppSetup(
        settings,
        boto3.client("bedrock-agentcore-control", region_name=settings.aws_region, config=config),
        boto3.client("secretsmanager", region_name=settings.aws_region, config=config),
    )
