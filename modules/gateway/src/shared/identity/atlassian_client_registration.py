"""Lease-fenced deployment client registration; Identity owns all user grants."""

import hashlib
import json
import secrets
import time
from urllib.parse import urlparse

import httpx
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.shared.identity.oauth_setup import atlassian_identity_metadata_matches, identity_config


def _post_registration(url, *, json, timeout, follow_redirects):
    deadline = time.monotonic() + 5
    with httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as client:
        with client.stream("POST", url, json=json) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline or len(body) + len(chunk) > 65536:
                    raise HTTPException(503, detail={"error": "atlassian_registration_uncertain"})
                body.extend(chunk)
            return httpx.Response(response.status_code, content=bytes(body))


def prepare_dynamic_client(setup, plan, *, before_effects, lease_guard, post=None, recovery_only=False):
    workload, _ = identity_config(setup.settings)
    workload_name = f"{workload.removesuffix('-oauth-user')}-atlassian-mcp-oauth-user"
    provider_name = f"{workload.removesuffix('-oauth-user')}-atlassian-mcp-user"
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    bootstrap_id = f"adp-bootstrap-{digest[:32]}"
    metadata = {
        "issuer": plan["issuer"],
        "authorizationEndpoint": plan["authorization_endpoint"],
        "tokenEndpoint": plan["token_endpoint"],
        "responseTypes": ["code"],
    }
    config = {"oauthDiscovery": {"authorizationServerMetadata": metadata}, "clientAuthenticationMethod": "CLIENT_SECRET_BASIC"}
    secret_ref = plan["app_secret_ref"]
    try:
        setup.secret_store.describe_secret(SecretId=secret_ref)
        stored = json.loads(setup.secret_store.get_secret_value(SecretId=secret_ref)["SecretString"])
    except (ClientError, KeyError, ValueError, TypeError):
        raise HTTPException(503, detail={"error": "atlassian_client_store_unavailable"}) from None
    if not isinstance(stored, dict) or stored.get("state") not in {"unregistered", "registration_uncertain", "registered"}:
        raise HTTPException(503, detail={"error": "atlassian_client_store_unavailable"})
    if stored["state"] == "registered" and (
        not isinstance(stored.get("client_id"), str)
        or not stored["client_id"]
        or not isinstance(stored.get("client_secret"), str)
        or not stored["client_secret"]
    ):
        raise HTTPException(503, detail={"error": "atlassian_client_store_unavailable"})
    if stored["state"] != "unregistered" and not isinstance(stored.get("callback_sha256"), str):
        raise HTTPException(503, detail={"error": "atlassian_client_store_unavailable"})
    if stored["state"] == "unregistered":
        if set(stored) not in ({"state"}, {"state", "bootstrap_secret"}) or (
            "bootstrap_secret" in stored and (not isinstance(stored["bootstrap_secret"], str) or not 32 <= len(stored["bootstrap_secret"]) <= 4096)
        ):
            raise HTTPException(503, detail={"error": "atlassian_client_store_unavailable"})
    # The durable intent is written before the registration POST. An untouched
    # store proves that POST never started, even if earlier Identity setup had
    # an uncertain result. Resume its named resources under the current lease.
    recovery_only = recovery_only and stored["state"] != "unregistered"
    if recovery_only and stored["state"] != "registered":
        raise HTTPException(503, detail={"error": "atlassian_registration_uncertain"})

    def effect():
        if before_effects is None or lease_guard is None:
            raise HTTPException(503, detail={"error": "atlassian_registration_requires_lease"})
        lease_guard()
        before_effects()

    existing_workload = setup._get(setup.control.get_workload_identity, name=workload_name)
    if existing_workload and existing_workload.get("allowedResourceOauth2ReturnUrls") != [plan["return_url"]]:
        raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
    if recovery_only and not existing_workload:
        raise HTTPException(503, detail={"error": "atlassian_setup_pending"})
    if not existing_workload and before_effects is not None:
        effect()
        setup.control.create_workload_identity(name=workload_name, allowedResourceOauth2ReturnUrls=[plan["return_url"]])
        existing_workload = setup._get(setup.control.get_workload_identity, name=workload_name)
    if not existing_workload:
        return {"status": "setup_required"}
    if existing_workload.get("allowedResourceOauth2ReturnUrls") != [plan["return_url"]]:
        raise HTTPException(503, detail={"error": "atlassian_setup_pending"})

    provider = setup._get(setup.control.get_oauth2_credential_provider, name=provider_name)
    if not provider and stored["state"] != "unregistered":
        raise HTTPException(503, detail={"error": "atlassian_setup_pending"})
    if not provider and before_effects is not None:
        # Keep bootstrap in the deployment-owned secret whose access is already
        # scoped in Terraform. Creating an Identity-managed secret would require
        # additional Secrets Manager permissions and an extra secret lifecycle.
        if "bootstrap_secret" not in stored:
            effect()
            stored = {**stored, "bootstrap_secret": secrets.token_urlsafe(32)}
            setup.secret_store.put_secret_value(SecretId=secret_ref, SecretString=json.dumps(stored))
        effect()
        setup.control.create_oauth2_credential_provider(
            name=provider_name,
            credentialProviderVendor="CustomOauth2",
            tags={"adp-config-digest": digest},
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    **config,
                    "clientId": bootstrap_id,
                    "clientSecretSource": "EXTERNAL",
                    "clientSecretConfig": {"secretId": secret_ref, "jsonKey": "bootstrap_secret"},
                }
            },
        )
        provider = setup._get(setup.control.get_oauth2_credential_provider, name=provider_name)
    if not provider:
        return {"status": "setup_required"}
    callback = provider.get("callbackUrl", "")
    parsed = urlparse(callback)
    provider_arn = provider.get("credentialProviderArn", "")
    if not isinstance(provider_arn, str) or not provider_arn.startswith(
        f"arn:aws:bedrock-agentcore:{setup.settings.aws_region}:{secret_ref.split(':')[4]}:"
    ):
        raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
    tags = setup.control.list_tags_for_resource(resourceArn=provider_arn).get("tags", {})
    actual = provider.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {})
    if (
        provider.get("credentialProviderVendor") != "CustomOauth2"
        or tags.get("adp-config-digest") != digest
        or not atlassian_identity_metadata_matches(actual.get("oauthDiscovery", {}).get("authorizationServerMetadata"), metadata)
        or actual.get("clientAuthenticationMethod") != "CLIENT_SECRET_BASIC"
        or parsed.scheme != "https"
        or parsed.netloc != f"bedrock-agentcore.{setup.settings.aws_region}.amazonaws.com"
        or not parsed.path.startswith("/identities/oauth2/callback/")
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
    callback_digest = hashlib.sha256(callback.encode()).hexdigest()
    if stored.get("callback_sha256") not in (None, callback_digest):
        raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
    client_id = actual.get("clientId")
    if client_id not in {bootstrap_id, stored.get("client_id")}:
        raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
    if stored["state"] == "registration_uncertain":
        raise HTTPException(503, detail={"error": "atlassian_registration_uncertain"})
    if stored["state"] == "unregistered":
        if before_effects is None:
            return {"status": "setup_required"}
        if client_id != bootstrap_id:
            raise HTTPException(409, detail={"error": "atlassian_setup_conflict"})
        effect()
        setup.secret_store.put_secret_value(
            SecretId=secret_ref, SecretString=json.dumps({"state": "registration_uncertain", "callback_sha256": callback_digest})
        )
        effect()
        registration_endpoint = plan["authorization_server_metadata"]["registration_endpoint"]
        response = (post or _post_registration)(
            registration_endpoint,
            json={
                "client_name": "ADP Atlassian MCP",
                "redirect_uris": [callback],
                "token_endpoint_auth_method": "client_secret_basic",
                "grant_types": ["authorization_code", "refresh_token"],
            },
            timeout=httpx.Timeout(5, connect=2),
            follow_redirects=False,
        )
        if response.status_code not in (200, 201) or len(response.content) > 65536:
            raise HTTPException(503, detail={"error": "atlassian_registration_uncertain"})
        try:
            registered = response.json()
        except ValueError:
            raise HTTPException(503, detail={"error": "atlassian_registration_uncertain"}) from None
        if (
            not isinstance(registered, dict)
            or not isinstance(registered.get("client_id"), str)
            or not 0 < len(registered["client_id"]) <= 1024
            or not isinstance(registered.get("client_secret"), str)
            or not 0 < len(registered["client_secret"]) <= 4096
            or registered.get("redirect_uris") != [callback]
            or registered.get("token_endpoint_auth_method") != "client_secret_basic"
        ):
            raise HTTPException(503, detail={"error": "atlassian_registration_uncertain"})
        stored = {
            "state": "registered",
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "callback_sha256": callback_digest,
        }
        effect()
        setup.secret_store.put_secret_value(SecretId=secret_ref, SecretString=json.dumps(stored))
    if client_id == bootstrap_id:
        if before_effects is None:
            return {"status": "setup_required"}
        effect()
        setup.control.update_oauth2_credential_provider(
            name=provider_name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    **config,
                    "clientId": stored["client_id"],
                    "clientSecretSource": "EXTERNAL",
                    "clientSecretConfig": {"secretId": secret_ref, "jsonKey": "client_secret"},
                }
            },
        )
        provider = setup._get(setup.control.get_oauth2_credential_provider, name=provider_name)
    final = provider.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {}) if provider else {}
    if (
        provider
        and provider.get("callbackUrl") == callback
        and provider.get("status") == "READY"
        and final.get("clientId") == stored["client_id"]
        and provider.get("clientSecretSource") == "EXTERNAL"
        and provider.get("clientSecretArn") == {"secretArn": secret_ref}
        and provider.get("clientSecretJsonKey") == "client_secret"
        and atlassian_identity_metadata_matches(final.get("oauthDiscovery", {}).get("authorizationServerMetadata"), metadata)
        and final.get("clientAuthenticationMethod") == "CLIENT_SECRET_BASIC"
    ):
        return {"status": "prepared", "callback_url": callback, "return_url": plan["return_url"]}
    return {"status": "setup_required"}
