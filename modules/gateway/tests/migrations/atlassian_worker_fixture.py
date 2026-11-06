"""Test-only file-backed Identity/provider transport for separate worker processes."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from src.shared.identity.oauth_setup import OAuthAppSetup

CALLBACK = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/recovery-fixture"
SECRET_REF = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/recovery-app"


def initial_record():
    issuer = "https://auth.atlassian.com/recovery-fixture"
    protected = {"resource": "https://mcp.atlassian.com/v2/mcp", "authorization_servers": [issuer]}
    authorization = {
        "issuer": issuer,
        "authorization_endpoint": "https://auth.atlassian.com/authorize",
        "token_endpoint": "https://auth.atlassian.com/oauth/token",
        "registration_endpoint": "https://auth.atlassian.com/register",
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }
    digest = hashlib.sha256(json.dumps([protected, authorization], sort_keys=True).encode()).hexdigest()
    plan = {
        "resource": protected["resource"],
        "issuer": issuer,
        "authorization_endpoint": authorization["authorization_endpoint"],
        "token_endpoint": authorization["token_endpoint"],
        "client_id": "",
        "app_secret_ref": SECRET_REF,
        "registration_mode": "dynamic-client-registration",
        "return_url": "https://dashboard.example.test/api/auth/connections/atlassian-mcp/return",
        "contract_revision": "fixture-contract",
        "protected_resource_metadata": protected,
        "authorization_server_metadata": authorization,
        "metadata_sha256": digest,
    }
    return {
        "plan": plan,
        "client": {"state": "unregistered"},
        "workload": None,
        "provider": None,
        "tags": {},
        "provider_creates": 0,
        "registration_requests": [],
        "update_attempts": 0,
        "lose_update_reply_once": True,
    }


class FileBackedTransport:
    def __init__(self, ledger_path: Path):
        self.ledger_path = ledger_path
        plan = self.read()["plan"]
        self.settings = SimpleNamespace(
            oauth_workload_name="adp-tools-dev-oauth-user",
            oauth_return_url="https://dashboard.example.test/api/auth/connections/oauth/return",
            aws_region="us-east-1",
            atlassian_config_ref="fixture-config",
            atlassian_contract_revision=plan["contract_revision"],
            atlassian_metadata_sha256=plan["metadata_sha256"],
            atlassian_identity_compatibility_verified=True,
        )

    def read(self):
        return json.loads(self.ledger_path.read_text())

    def write(self, record):
        temporary = self.ledger_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record))
        temporary.replace(self.ledger_path)

    def setup(self):
        return OAuthAppSetup(self.settings, self, self)

    def describe_secret(self, **kwargs):
        assert kwargs["SecretId"] == SECRET_REF
        return {"ARN": SECRET_REF}

    def get_secret_value(self, **kwargs):
        record = self.read()
        secret_id = kwargs["SecretId"]
        assert secret_id in {"fixture-config", SECRET_REF}
        return {"SecretString": json.dumps(record["plan"] if secret_id == "fixture-config" else record["client"])}

    def put_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == SECRET_REF
        record = self.read()
        record["client"] = json.loads(kwargs["SecretString"])
        self.write(record)

    def get_workload_identity(self, **kwargs):
        assert kwargs["name"].endswith("-atlassian-mcp-oauth-user")
        return self.read()["workload"]

    def create_workload_identity(self, **kwargs):
        record = self.read()
        assert record["workload"] is None
        record["workload"] = {"allowedResourceOauth2ReturnUrls": kwargs["allowedResourceOauth2ReturnUrls"]}
        self.write(record)

    def get_oauth2_credential_provider(self, **kwargs):
        assert kwargs["name"].endswith("-atlassian-mcp-user")
        return self.read()["provider"]

    def list_tags_for_resource(self, **kwargs):
        assert kwargs["resourceArn"] == self.read()["provider"]["credentialProviderArn"]
        return {"tags": self.read()["tags"]}

    def create_oauth2_credential_provider(self, **kwargs):
        record = self.read()
        assert record["provider"] is None
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        assert config["clientSecretSource"] == "EXTERNAL"
        assert config["clientSecretConfig"] == {"secretId": SECRET_REF, "jsonKey": "bootstrap_secret"}
        assert len(record["client"]["bootstrap_secret"]) >= 32
        record["provider"] = {
            "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:oauth2credentialprovider/recovery",
            "credentialProviderVendor": "CustomOauth2",
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": config["oauthDiscovery"],
                    "clientId": config["clientId"],
                    "clientAuthenticationMethod": config["clientAuthenticationMethod"],
                }
            },
            "clientSecretSource": "EXTERNAL",
            "clientSecretArn": {"secretArn": SECRET_REF},
            "clientSecretJsonKey": "bootstrap_secret",
            "callbackUrl": CALLBACK,
            "status": "READY",
        }
        record["tags"] = kwargs["tags"]
        record["provider_creates"] += 1
        self.write(record)

    def update_oauth2_credential_provider(self, **kwargs):
        record = self.read()
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        assert kwargs["name"].endswith("-atlassian-mcp-user")
        assert config["clientSecretConfig"] == {"secretId": SECRET_REF, "jsonKey": "client_secret"}
        record["update_attempts"] += 1
        record["provider"]["oauth2ProviderConfigOutput"]["customOauth2ProviderConfig"]["clientId"] = config["clientId"]
        record["provider"]["clientSecretSource"] = "EXTERNAL"
        record["provider"]["clientSecretArn"] = {"secretArn": SECRET_REF}
        record["provider"]["clientSecretJsonKey"] = "client_secret"
        lost_reply = record["lose_update_reply_once"]
        record["lose_update_reply_once"] = False
        self.write(record)
        if lost_reply:
            raise TimeoutError("controlled Identity update reply loss")

    def post_registration(self, url, *, json, timeout, follow_redirects):
        record = self.read()
        assert url == record["plan"]["authorization_server_metadata"]["registration_endpoint"]
        assert json["redirect_uris"] == [CALLBACK] and json["token_endpoint_auth_method"] == "client_secret_basic"
        assert record["client"]["state"] == "registration_uncertain"
        assert timeout.connect == 2 and follow_redirects is False
        record["registration_requests"].append({"callback": CALLBACK, "method": "client_secret_basic"})
        self.write(record)
        response = {
            "client_id": "retained-fixture-client",
            "client_secret": "sentinel-not-a-user-token",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "client_secret_basic",
        }
        return SimpleNamespace(status_code=201, content=b"controlled-registration", json=lambda: response)
