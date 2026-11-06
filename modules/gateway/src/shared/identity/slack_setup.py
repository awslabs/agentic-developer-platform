"""One-time Slack manifest automation, using an owner-supplied config token.

Only an ADP-created app may be resumed. Persist intent before the non-idempotent
create; an uncertain result requires reconciliation, never a second create.
The configuration token is used for this request only and is never persisted.
"""

import json
import re

import httpx
from fastapi import HTTPException

SLACK_USER_METADATA = {
    "issuer": "https://slack.com",
    "authorizationEndpoint": "https://slack.com/oauth/v2_user/authorize",
    "tokenEndpoint": "https://slack.com/api/oauth.v2.user.access",
    "responseTypes": ["code"],
    "tokenEndpointAuthMethods": ["client_secret_basic"],
}
AUTOMATION = "adp-slack-manifest-v1"
REJECTED_WITHOUT_CREATION = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_expired",
        "token_revoked",
        "missing_scope",
        "invalid_manifest",
        "invalid_arguments",
        "not_in_team",
        "access_denied",
        "account_inactive",
    }
)


class SlackSetupRejected(HTTPException):
    """A documented refusal that establishes the create had no effect."""


def slack_manifest():
    # App permissions are an allowlist; ADP requests chat:write only in the
    # separate explicit user consent flow for approved test-channel posting.
    return {
        "display_information": {"name": "ADP", "description": "Connect your Slack account to ADP"},
        "oauth_config": {"scopes": {"user": ["users:read", "chat:write"]}},
        "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False},
    }


def _failure(code, message, status=503):
    return HTTPException(status, detail={"error": code, "message": message})


class SlackManifestClient:
    def __init__(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise _failure("slack_config_token_invalid", "Enter a valid Slack app configuration access token.", 400)
        self.token = token

    def call(self, operation, **body):
        if operation not in {"create", "export", "update"}:
            raise ValueError("unsupported manifest operation")
        try:
            with httpx.Client(timeout=httpx.Timeout(12, connect=3), trust_env=False, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    f"https://slack.com/api/apps.manifest.{operation}",
                    headers={"Authorization": "Bearer " + self.token, "Accept-Encoding": "identity"},
                    json=body,
                ) as response:
                    if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("Slack request unavailable")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > 65536:
                            raise ValueError("Slack response too large")
                        raw.extend(chunk)
            result = json.loads(raw)
            if isinstance(result, dict) and result.get("ok") is False and result.get("error") in REJECTED_WITHOUT_CREATION:
                raise SlackSetupRejected(
                    400,
                    detail={
                        "error": "slack_setup_rejected",
                        "reason": result["error"],
                        "message": "Slack refused app setup. Check your app configuration access token and workspace permissions, then try again.",
                    },
                )
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Slack refused app configuration")
            return result
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise _failure(
                "slack_setup_unavailable",
                "Slack app setup did not finish. Refresh its status before retrying; check the configuration token and workspace app policy.",
            ) from None


def automate_slack(setup, token, *, before_effects, client=None):
    client = client or SlackManifestClient(token)
    name = setup.provider_name("slack")
    secret_name = setup.secret_name("slack")
    from src.shared.identity.oauth_setup import identity_config

    workload_name, return_url = identity_config(setup.settings)
    workload = setup._get(setup.control.get_workload_identity, name=workload_name)
    if workload and return_url not in workload.get("allowedResourceOauth2ReturnUrls", []):
        raise _failure("oauth_return_mismatch", "The deployment return URL needs an operator update before creating an app.", 409)
    provider = setup._get(setup.control.get_oauth2_credential_provider, name=name)
    stored = setup._get(setup.secret_store.get_secret_value, SecretId=secret_name)
    saved = json.loads(stored["SecretString"]) if stored else None
    if saved is not None and (not isinstance(saved, dict) or saved.get("automation") != AUTOMATION):
        raise _failure("slack_app_exists", "An app is already configured. Use its existing app settings.", 409)
    if provider and (saved is None or saved.get("stage") == "create_rejected"):
        raise _failure("slack_app_exists", "An app is already registered. Use its existing app settings.", 409)
    if saved is not None and saved.get("stage") == "creating":
        raise _failure(
            "slack_setup_reconciliation_required",
            "A prior Slack app creation has an uncertain result. Check your Slack apps and ask an operator to reconcile it; "
            "ADP will not create a duplicate.",
            409,
        )
    if saved is None or saved.get("stage") == "create_rejected":
        before_effects()
        reservation = json.dumps({"automation": AUTOMATION, "stage": "creating"})
        if saved is None:
            setup.secret_store.create_secret(Name=secret_name, SecretString=reservation)
        else:
            setup.secret_store.put_secret_value(SecretId=secret_name, SecretString=reservation)
        try:
            result = client.call("create", manifest=json.dumps(slack_manifest()))
        except SlackSetupRejected:
            setup.secret_store.put_secret_value(SecretId=secret_name, SecretString=json.dumps({"automation": AUTOMATION, "stage": "create_rejected"}))
            raise
        credentials = result.get("credentials", {})
        if (
            not isinstance(result.get("app_id"), str)
            or not re.fullmatch(r"A[A-Z0-9]{8,63}", result["app_id"])
            or not isinstance(credentials, dict)
            or any(
                not isinstance(credentials.get(key), str) or not re.fullmatch(r"[\x21-\x7e]{1,4096}", credentials[key])
                for key in ("client_id", "client_secret")
            )
        ):
            raise _failure(
                "slack_setup_reconciliation_required", "Slack's app creation result could not be verified. Check your Slack apps before continuing."
            )
        saved = {
            "automation": AUTOMATION,
            "stage": "credentials_saved",
            "app_id": result["app_id"],
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
        }
        setup.secret_store.put_secret_value(SecretId=secret_name, SecretString=json.dumps(saved))
    if not re.fullmatch(r"A[A-Z0-9]{8,63}", saved.get("app_id", "")) or saved.get("stage") not in {"credentials_saved", "ready"}:
        raise _failure("slack_setup_reconciliation_required", "Stored Slack setup needs operator reconciliation.", 409)
    if provider:
        config = provider.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {})
        if (
            provider.get("credentialProviderVendor") != "CustomOauth2"
            or config.get("clientId") != saved.get("client_id")
            or config.get("oauthDiscovery", {}).get("authorizationServerMetadata") != SLACK_USER_METADATA
        ):
            raise _failure("slack_app_changed", "The registered Slack app changed. Ask an operator to reconcile it.", 409)
        status = setup.status("slack", admin=True)
    else:
        status = setup.register("slack", saved["client_id"], saved["client_secret"], before_effects=before_effects)
    callback = status.get("callback_url")
    if not callback or status["status"] != "registered":
        raise _failure("slack_callback_pending", "App registration is still pending. Refresh before resuming setup.")
    # Export first: update replaces the entire manifest. Preserve any settings
    # the owner has added since creation, including other redirect URLs.
    exported = client.call("export", app_id=saved["app_id"])
    manifest = exported.get("manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("oauth_config", {}), dict):
        raise _failure("slack_manifest_unavailable", "The current Slack app configuration could not be read.")
    oauth = manifest.setdefault("oauth_config", {})
    redirects = oauth.get("redirect_urls", [])
    if not isinstance(redirects, list) or any(not isinstance(url, str) for url in redirects):
        raise _failure("slack_manifest_unavailable", "The current Slack app configuration could not be read.")
    if callback not in redirects:
        oauth["redirect_urls"] = [*redirects, callback]
        before_effects()
        updated = client.call("update", app_id=saved["app_id"], manifest=json.dumps(manifest))
        if updated.get("app_id") != saved["app_id"]:
            raise _failure("slack_callback_pending", "Verify the callback in Slack before connecting an account.")
    confirmed = client.call("export", app_id=saved["app_id"]).get("manifest", {})
    if callback not in confirmed.get("oauth_config", {}).get("redirect_urls", []):
        raise _failure("slack_callback_pending", "The callback could not be verified in Slack. Resume setup to check again.")
    before_effects()
    setup.secret_store.put_secret_value(SecretId=secret_name, SecretString=json.dumps({**saved, "stage": "ready"}))
    return {**status, "automation_state": "ready", "developer_url": f"https://api.slack.com/apps/{saved['app_id']}", "callback_configured": True}
