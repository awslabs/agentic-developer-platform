"""Read production Identity, the registered client and MCP provider separately."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from tests.e2e.tools.atlassian_oauth_probe import CONSENT_SCOPES, RESOURCE
from tests.e2e.tools.atlassian_setup_live import CONNECTION, private_file


def _aws(config, role, service, operation, *, input_value=None, arguments=()):
    expected = config[role + "_role_arn"]
    aws_config = os.environ.get("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE")
    process_env = None
    if aws_config:
        private_file(aws_config, max_bytes=4096)
        credential_file = str(Path(aws_config).with_name("aws-credentials"))
        private_file(credential_file, max_bytes=0)
        if config[role + "_label"] != f"oidc-{role}":
            raise ValueError("Selected protected observer profile unavailable")
        prefix = ["aws", "--profile", f"oidc-{role}"]
        process_env = {
            key: os.environ[key]
            for key in (
                "PATH",
                "HOME",
                "LANG",
                "AWS_REGION",
                "ACTIONS_ID_TOKEN_REQUEST_URL",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
                "GITHUB_REPOSITORY",
                "GITHUB_REF",
                "GITHUB_EVENT_NAME",
                "GITHUB_WORKFLOW_REF",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
            )
            if key in os.environ
        }
        process_env.update(
            AWS_CONFIG_FILE=aws_config,
            AWS_SHARED_CREDENTIALS_FILE=credential_file,
            AWS_EC2_METADATA_DISABLED="true",
        )
    else:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise ValueError("Protected observer credential profile unavailable")
        prefix = [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            config[role + "_label"],
            "--exec",
            "aws",
        ]

    def invoke(parts, *, payload=None):
        try:
            response = subprocess.run(
                prefix
                + [
                    "--region",
                    config["region"],
                    *parts,
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                input=json.dumps(payload) if payload is not None else None,
                text=True,
                capture_output=True,
                timeout=90 if aws_config else 30,
                check=False,
                **({"env": process_env} if process_env is not None else {}),
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Selected AWS connection unavailable") from None
        if response.returncode != 0:
            if (
                "ResourceNotFoundException" in response.stderr
                and operation == "get-resource-oauth2-token"
            ):
                raise ValueError("Identity grant absent") from None
            raise ValueError("Selected AWS read unavailable")
        if len(response.stdout) > 131072:
            raise ValueError("Selected AWS read exceeded bound")
        try:
            return json.loads(response.stdout)
        except ValueError:
            raise ValueError("Selected AWS read returned invalid data") from None

    identity = invoke(["sts", "get-caller-identity"])
    assumed = identity.get("Arn", "") if isinstance(identity, dict) else ""
    account = config["account"]
    name = expected.rsplit("/", 1)[-1]
    if (
        type(identity) is not dict
        or identity.get("Account") != account
        or not assumed.startswith(f"arn:aws:sts::{account}:assumed-role/{name}/")
    ):
        raise ValueError(
            "Selected AWS account or exact role differs from protected configuration"
        )
    parts = [service, operation, *arguments]
    if input_value is not None:
        parts.extend(["--cli-input-json", "file:///dev/stdin"])
    result = invoke(parts, payload=input_value)
    if type(result) is not dict:
        raise ValueError("Selected AWS response is not an object")
    return result


class IndependentSetupObserver:
    """A second authority reads AgentCore and calls the actual MCP identity tools."""

    independent = True

    def __init__(self, config, *, gateway_revision=None):
        self.config = config
        self.gateway_revision = gateway_revision
        self.token = None
        self.client_sha256 = None

    def _gateway_status(self):
        token = private_file(
            self.config["observer_token_file"], max_bytes=16384
        ).strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("Independent human session unavailable")
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = client.get(
                    self.config["gateway_url"].rstrip("/") + CONNECTION + "/status",
                    headers={"Authorization": "Bearer " + token},
                )
            if response.status_code != 200 or len(response.content) > 65536:
                raise ValueError("Independent gateway status unavailable")
            result = response.json()
            if type(result) is not dict:
                raise ValueError("Independent gateway status invalid")
            return result
        except (httpx.HTTPError, ValueError):
            raise ValueError("Independent gateway status unavailable") from None

    def _provider(self):
        config = self.config
        plan = _aws(
            config,
            "core",
            "secretsmanager",
            "get-secret-value",
            arguments=("--secret-id", config["plan_secret_arn"]),
        )
        stored = _aws(
            config,
            "core",
            "secretsmanager",
            "get-secret-value",
            arguments=("--secret-id", config["client_secret_arn"]),
        )
        plan = json.loads(plan["SecretString"])
        stored = json.loads(stored["SecretString"])
        if (
            plan.get("registration_mode") != "dynamic-client-registration"
            or plan.get("client_id") != ""
            or plan.get("metadata_sha256") != config["metadata_sha256"]
            or plan.get("app_secret_ref") != config["client_secret_arn"]
            or plan.get("return_url")
            != config["gateway_url"].rstrip("/") + CONNECTION + "/return"
            or stored.get("state") != "registered"
            or not isinstance(stored.get("client_id"), str)
            or not stored["client_id"]
            or not isinstance(stored.get("client_secret"), str)
            or not stored["client_secret"]
        ):
            raise ValueError(
                "Registered client does not match the pinned production plan"
            )
        self.client_sha256 = hashlib.sha256(stored["client_id"].encode()).hexdigest()
        provider = _aws(
            config,
            "core",
            "bedrock-agentcore-control",
            "get-oauth2-credential-provider",
            arguments=("--name", config["provider_name"]),
        )
        workload = _aws(
            config,
            "core",
            "bedrock-agentcore-control",
            "get-workload-identity",
            arguments=("--name", config["workload_name"]),
        )
        actual = provider.get("oauth2ProviderConfigOutput", {}).get(
            "customOauth2ProviderConfig", {}
        )
        expected_metadata = {
            "issuer": plan.get("issuer"),
            "authorizationEndpoint": plan.get("authorization_endpoint"),
            "tokenEndpoint": plan.get("token_endpoint"),
            "responseTypes": ["code"],
        }
        callback = provider.get("callbackUrl", "")
        parsed_callback = urlsplit(callback)
        provider_arn = provider.get("credentialProviderArn", "")
        tags = _aws(
            config,
            "core",
            "bedrock-agentcore-control",
            "list-tags-for-resource",
            arguments=("--resource-arn", provider_arn),
        ).get("tags", {})
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        if (
            provider.get("status") != "READY"
            or actual.get("oauthDiscovery", {}).get("authorizationServerMetadata")
            not in (expected_metadata, {**expected_metadata, "tokenEndpointAuthMethods": ["client_secret_basic"]})
            or actual.get("clientAuthenticationMethod") != "CLIENT_SECRET_BASIC"
            or not provider_arn.startswith(
                f"arn:aws:bedrock-agentcore:{config['region']}:{config['account']}:"
            )
            or tags.get("adp-config-digest") != digest
            or parsed_callback.scheme != "https"
            or parsed_callback.netloc
            != f"bedrock-agentcore.{config['region']}.amazonaws.com"
            or not parsed_callback.path.startswith("/identities/oauth2/callback/")
            or parsed_callback.query
            or parsed_callback.fragment
            or actual.get("clientId") != stored["client_id"]
            or provider.get("clientSecretArn") != {"secretArn": config["client_secret_arn"]}
            or provider.get("clientSecretJsonKey") != "client_secret"
            or provider.get("clientSecretSource") != "EXTERNAL"
            or hashlib.sha256(callback.encode()).hexdigest()
            != stored.get("callback_sha256")
            or workload.get("allowedResourceOauth2ReturnUrls") != [plan["return_url"]]
        ):
            raise ValueError("Production Identity provider or DCR readback differs")
        return plan, provider

    def _access_token(self, owner):
        config = self.config
        workload = _aws(
            config,
            "adapter",
            "bedrock-agentcore",
            "get-workload-access-token-for-user-id",
            arguments=("--workload-name", config["workload_name"], "--user-id", owner),
        )
        if not isinstance(workload.get("workloadAccessToken"), str):
            raise ValueError("Owner Identity workload access unavailable")
        response = _aws(
            config,
            "adapter",
            "bedrock-agentcore",
            "get-resource-oauth2-token",
            input_value={
                "workloadIdentityToken": workload["workloadAccessToken"],
                "resourceCredentialProviderName": config["provider_name"],
                "scopes": list(CONSENT_SCOPES),
                "resources": [RESOURCE],
                "oauth2Flow": "USER_FEDERATION",
            },
        )
        token = response.get("accessToken")
        if (
            response.get("sessionStatus") in {"IN_PROGRESS", "FAILED"}
            or not isinstance(token, str)
            or not token
            or len(token) > 16384
            or any(character.isspace() for character in token)
        ):
            raise ValueError("Owner Identity grant is not active")
        return token

    def read_actions(self, *, run_id, owner_id, fixture_kind):
        config = self.config
        if (run_id, owner_id, fixture_kind) != tuple(
            config[key] for key in ("run_id", "owner_id", "fixture_kind")
        ):
            raise ValueError("A different owner or run requested provider readback")
        plan, provider = self._provider()
        status = self._gateway_status()
        binding = status.get("connection") or {}
        attempt = json.loads(private_file(config["attempt_file"], max_bytes=8192))
        if (
            status.get("status")
            not in (
                {"ready", "policy_blocked"} if config["policy_required"] else {"ready"}
            )
            or type(attempt) is not dict
            or attempt.get("phase") != "selected"
            or attempt.get("run_id") != run_id
            or attempt.get("owner_id") != owner_id
            or attempt.get("connection_id") != binding.get("id")
            or attempt.get("revision") != binding.get("revision")
            or not binding.get("revision")
            or binding.get("selected_site_id") != config["site_id"]
            or config["site_id"] not in binding.get("sites", [])
        ):
            raise ValueError(
                "Gateway consent, site selection or policy not independently visible"
            )
        from tool_adapters.atlassian_observe import collect_observation

        settings = SimpleNamespace(
            subject_schema_sha256=config["subject_schema_sha256"],
            sites_schema_sha256=config["sites_schema_sha256"],
        )
        self.token = self._access_token(owner_id)
        subject, sites, scopes = collect_observation(self.token, settings)
        if (
            subject != config["expected_subject"]
            or not any(site.site_id == config["site_id"] for site in sites)
            or not set(CONSENT_SCOPES) - {"offline_access"} <= set(scopes)
        ):
            raise ValueError(
                "Independent provider subject or site does not match the owner"
            )
        safe = {
            "run": run_id,
            "owner": owner_id,
            "site": config["site_id"],
            "subject": subject,
            "metadata": plan["metadata_sha256"],
            "provider": provider["credentialProviderArn"],
            "revision": binding["revision"],
        }
        digest = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
        events = []
        for action, source in (
            ("dcr_registration", "identity"),
            ("identity_provider_setup", "identity"),
            ("connection_begin", "gateway"),
            ("consent", "identity"),
            ("site_selection", "provider"),
        ):
            events.append(
                {
                    "action": action,
                    "actor": "human"
                    if action in {"consent", "site_selection"}
                    else "machine",
                    "status": "observed",
                    "source": source,
                    "evidence_sha256": digest,
                    "run_id": run_id,
                    "owner_id": owner_id,
                }
            )
        from tests.e2e.tools.atlassian_setup_client import observe_client_configuration

        events.append(observe_client_configuration(config, self.gateway_revision))
        if config["policy_required"]:
            event = self._policy_action(binding)
            if event is not None:
                events.append(event)
        return events

    def _policy_action(self, connection):
        from tests.e2e.tools.atlassian_policy_observation import (
            PolicyObservationLedger,
            binding_for,
            observe_read_policy,
        )
        from tests.e2e.tools.atlassian_readback_grant import IdentityReadbackGrant

        config = self.config
        policy = config.get("policy_observation")
        if policy is None:
            return None
        if (
            type(policy) is not dict
            or set(policy)
            != {"tenant_id", "grant_file", "probes", "expected_results", "checks"}
            or type(policy["checks"]) is not dict
            or set(policy["checks"]) != {"read", "search"}
            or not self.client_sha256
        ):
            raise ValueError("Protected read/search policy fixtures are required")
        grant = IdentityReadbackGrant(
            json.loads(private_file(policy["grant_file"])), config["owner_id"]
        )
        if (
            grant.config["provider_name"] != config["provider_name"]
            or grant.config["workload_name"] != config["workload_name"]
            or grant.config["provider_account_id"] != config["expected_subject"]
            or any(grant.config[key] != config[key] for key in ("account", "region", "adapter_label", "adapter_role_arn"))
        ):
            raise ValueError(
                "Policy observation must use the independently observed client and owner"
            )
        token = private_file(config["observer_token_file"], max_bytes=16384).strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("Independent human session unavailable")
        with httpx.Client(
            timeout=25, follow_redirects=False, trust_env=False
        ) as client:
            response = client.post(
                config["gateway_url"].rstrip("/") + CONNECTION + "/policy/observe",
                headers={"Authorization": "Bearer " + token},
                json={
                    "connection_revision": connection["revision"],
                    "site_id": config["site_id"],
                    "probes": policy["probes"],
                    "expected_results": policy["expected_results"],
                },
            )
        if response.status_code != 200 or len(response.content) > 16384:
            raise ValueError("Current tenant policy observation unavailable")
        result = response.json()
        now = int(time.time() * 1000)
        if (
            type(result) is not dict
            or result.get("owner_id") != config["owner_id"]
            or result.get("tenant_id") != policy["tenant_id"]
            or result.get("connection_revision") != connection["revision"]
            or result.get("site_id") != config["site_id"]
            or result.get("permission") != "read-search"
            or type(result.get("observed_at_ms")) is not int
            or not 0 <= now - result["observed_at_ms"] <= 30_000
            or result.get("status") not in {"allowed", "blocked", "unknown"}
        ):
            raise ValueError(
                "Tenant policy observation is stale or belongs to another binding"
            )
        evidence = []
        for permission, check in policy["checks"].items():
            if type(check) is not dict or set(check) != {
                "path",
                "expected_result_sha256",
                "ledger_file",
            }:
                raise ValueError("An independent read expectation is required")
            binding = binding_for(
                grant,
                run_id=config["run_id"],
                tenant_id=policy["tenant_id"],
                site_id=config["site_id"],
                client_sha256=self.client_sha256,
                configuration_sha256=result["configuration_sha256"],
                permission=permission,
                path=check["path"],
                expected_result_sha256=check["expected_result_sha256"],
            )
            ledger = PolicyObservationLedger(check["ledger_file"], binding)
            evidence.append(
                observe_read_policy(
                    grant,
                    ledger,
                    path=check["path"],
                    expected_result_sha256=check["expected_result_sha256"],
                )
            )
        allowed = result["status"] == "allowed" and all(
            item["status"] == "allowed" for item in evidence
        )
        digest = hashlib.sha256(
            json.dumps(
                {"gateway": result, "provider": evidence}, sort_keys=True
            ).encode()
        ).hexdigest()
        return {
            "action": "policy_observation",
            "actor": "machine",
            "status": "observed" if allowed else "blocked",
            "source": "provider",
            "evidence_sha256": digest,
            "run_id": config["run_id"],
            "owner_id": config["owner_id"],
        }

    def cleaned(self, *, run_id, owner_id, fixture_kind):
        if (run_id, owner_id, fixture_kind) != tuple(
            self.config[key] for key in ("run_id", "owner_id", "fixture_kind")
        ):
            raise ValueError("A different run cannot attest cleanup")
        if self._gateway_status().get("connection") or self.token is None:
            return False
        try:
            self._access_token(owner_id)
        except ValueError as error:
            if str(error) != "Identity grant absent":
                return False
        else:
            return False
        from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError

        try:
            MCPTransport(deadline=time.monotonic() + 8).send(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "adp-atlassian-observer", "version": "1"},
                },
                self.token,
            )
        except ProviderResponseError as error:
            return error.code == "provider_unauthorized"
        except (OSError, ValueError):
            return False
        return False
