"""Prepare only isolated AgentCore Identity provider resources for the probe.

A retained deployment app is a private configuration prerequisite, not a user
OAuth credential. CIMD and automated registration remain unverified modes.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from secrets import token_hex
from urllib.parse import urlsplit

from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked
from tests.e2e.tools.atlassian_probe_ledger import ProbeLedger


OWNERSHIP_TAG = "adp-atlassian-proof-attempt"


@dataclass(frozen=True)
class ProviderPlan:
    identity: object
    protected_resource: dict
    authorization_server: dict
    client_id: str
    secret_id: str
    region: str

    def __post_init__(self):
        if not self.identity.metadata_sha256 or self.identity.issuer != self.authorization_server.get("issuer"):
            raise ProbeBlocked("A verified v2 resource and issuer must precede provider setup")
        digest = hashlib.sha256(json.dumps([self.protected_resource, self.authorization_server], sort_keys=True).encode()).hexdigest()
        if digest != self.identity.metadata_sha256:
            raise ProbeBlocked("Provider setup metadata differs from the consent configuration")
        if not all(name.startswith("atlassian-proof-") for name in (self.identity.workload_name, self.identity.provider_name)):
            raise ProbeBlocked("Setup names must be isolated from all existing connection providers")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", self.region):
            raise ProbeBlocked("A selected AWS region is required")
        if not isinstance(self.client_id, str) or not self.client_id or len(self.client_id) > 512:
            raise ProbeBlocked("An approved deployment OAuth client ID is required")
        if not isinstance(self.secret_id, str) or not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:[a-zA-Z0-9/_+=.@-]+", self.secret_id):
            raise ProbeBlocked("A protected deployment app-secret reference is required")
        if self.secret_id.split(":")[3] != self.region:
            raise ProbeBlocked("The app secret must belong to the selected region")
        methods = self.authorization_server.get("token_endpoint_auth_methods_supported", [])
        if "client_secret_basic" not in methods:
            raise ProbeBlocked("The issuer does not advertise the selected client authentication method")
        for field in ("authorization_endpoint", "token_endpoint"):
            endpoint = urlsplit(self.authorization_server.get(field, ""))
            if endpoint.scheme != "https" or endpoint.netloc != "auth.atlassian.com" or not endpoint.path or endpoint.fragment:
                raise ProbeBlocked("The selected provider endpoint is not an approved HTTPS issuer endpoint")

    @property
    def metadata(self):
        # Retain the durable setup-intent digest for already owned probes.
        # New AWS requests omit the deprecated field below.
        return {
            "issuer": self.identity.issuer,
            "authorizationEndpoint": self.authorization_server["authorization_endpoint"],
            "tokenEndpoint": self.authorization_server["token_endpoint"],
            "responseTypes": ["code"],
            "tokenEndpointAuthMethods": ["client_secret_basic"],
        }

    @property
    def digest(self):
        return hashlib.sha256(json.dumps({
            "identity_metadata_sha256": self.identity.metadata_sha256,
            "client_id": self.client_id,
            "secret_id": self.secret_id,
            "region": self.region,
            "provider_metadata": self.metadata,
        }, sort_keys=True).encode()).hexdigest()


class ProviderSetup:
    """Use core control SDK only; read back or hold uncertainty after lost replies."""

    def __init__(self, plan: ProviderPlan, control, lifecycle, ledger_path):
        if lifecycle.config != plan.identity:
            raise ProbeBlocked("Setup and consent must use the same pinned Identity configuration")
        self.plan = plan
        self.control = control
        self.ledger = ProbeLedger(ledger_path, lifecycle)

    @staticmethod
    def _get(operation, name):
        try:
            return operation(name=name)
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise ProbeBlocked("Identity control-plane read failed") from None

    def _owned_arn(self, resource, field, recorded, record):
        arn = resource.get(field)
        account = self.plan.secret_id.split(":")[4]
        prefix = f"arn:aws:bedrock-agentcore:{self.plan.region}:{account}:"
        if not isinstance(arn, str) or not arn.startswith(prefix) or (record.get(recorded) and record[recorded] != arn):
            raise ProbeBlocked("The Identity resource does not match this setup attempt")
        try:
            tags = self.control.list_tags_for_resource(resourceArn=arn)
        except Exception:
            raise ProbeBlocked("Identity ownership tags could not be read") from None
        if not isinstance(tags, dict) or not isinstance(tags.get("tags"), dict) or tags["tags"].get(OWNERSHIP_TAG) != record["ownership_nonce"]:
            raise ProbeBlocked("Identity resource ownership has not been established")
        return arn

    def _verify_workload(self, workload):
        if workload.get("name") != self.plan.identity.workload_name or self.plan.identity.return_url not in workload.get("allowedResourceOauth2ReturnUrls", []):
            raise ProbeBlocked("Workload identity is not bound to the expected return URL")

    def _verify_provider(self, provider):
        identity = self.plan.identity
        expected = {"clientId": self.plan.client_id,
                    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC"}
        metadata = {key: value for key, value in self.plan.metadata.items() if key != "tokenEndpointAuthMethods"}
        actual = provider.get("oauth2ProviderConfigOutput", {}).get("customOauth2ProviderConfig", {})
        callback = urlsplit(provider.get("callbackUrl", ""))
        if (provider.get("name") != identity.provider_name or provider.get("credentialProviderVendor") != "CustomOauth2"
                or provider.get("status") != "READY" or any(actual.get(key) != value for key, value in expected.items())
                or actual.get("oauthDiscovery") not in ({"authorizationServerMetadata": metadata}, {"authorizationServerMetadata": self.plan.metadata})
                or provider.get("clientSecretSource") != "EXTERNAL" or provider.get("clientSecretArn") != {"secretArn": self.plan.secret_id}
                or provider.get("clientSecretJsonKey") != "client_secret"
                or callback.scheme != "https" or callback.netloc != f"bedrock-agentcore.{self.plan.region}.amazonaws.com"
                or not callback.path.startswith("/identities/oauth2/callback/") or callback.query or callback.fragment):
            raise ProbeBlocked("Provider configuration, callback or app secret differs from the isolated plan")

    def prepare(self):
        identity = self.plan.identity
        with self.ledger._locked():
            record = self.ledger._read()
            if record and record.get("setup_digest") != self.plan.digest:
                raise ProbeBlocked("A different deployment app/configuration owns this setup")
            if record and (not isinstance(record.get("ownership_nonce"), str) or
                           not re.fullmatch(r"[0-9a-f]{48}", record["ownership_nonce"])):
                raise ProbeBlocked("An older setup without a private ownership marker cannot be adopted")
            if record and record.get("status") in {"cleaning", "cleaned"}:
                raise ProbeBlocked("A cleanup attempt cannot restart provider setup")
            workload = self._get(self.control.get_workload_identity, identity.workload_name)
            provider = self._get(self.control.get_oauth2_credential_provider, identity.provider_name)
            if record is None:
                if workload or provider:
                    raise ProbeBlocked("Existing unowned Identity resources must not be adopted or overwritten")
                record = {"config_digest": self.ledger.config_digest, "setup_digest": self.plan.digest,
                          "status": "preparing", "phase": "workload", "uncertain": False,
                          "ownership_nonce": token_hex(24)}
                self.ledger._save(record)
            if workload:
                self._verify_workload(workload)
            elif record.get("phase") != "workload":
                raise ProbeBlocked("A previously prepared workload disappeared; do not recreate it")
            elif record.get("uncertain"):
                raise ProbeBlocked("Workload create reply was lost; wait for authoritative Identity readback")
            else:
                record.update(phase="workload", uncertain=True)
                self.ledger._save(record)
                try:
                    self.control.create_workload_identity(name=identity.workload_name,
                                                          allowedResourceOauth2ReturnUrls=[identity.return_url],
                                                          tags={OWNERSHIP_TAG: record["ownership_nonce"]})
                except Exception:
                    raise ProbeBlocked("Workload create unconfirmed; reconcile before retry") from None
                workload = self._get(self.control.get_workload_identity, identity.workload_name)
                if not workload:
                    raise ProbeBlocked("Workload creation has not been read back")
                self._verify_workload(workload)
            record["workload_arn"] = self._owned_arn(workload, "workloadIdentityArn", "workload_arn", record)
            self.ledger._save(record)
            if record["phase"] != "provider":
                record.update(phase="provider", uncertain=False)
                self.ledger._save(record)
            if provider:
                self._verify_provider(provider)
            elif record.get("uncertain"):
                raise ProbeBlocked("Provider create reply was lost; wait for authoritative Identity readback")
            elif record.get("status") == "ready":
                raise ProbeBlocked("A previously ready provider disappeared; do not recreate it")
            else:
                record.update(uncertain=True)
                self.ledger._save(record)
                try:
                    self.control.create_oauth2_credential_provider(
                        name=identity.provider_name,
                        credentialProviderVendor="CustomOauth2",
                        tags={OWNERSHIP_TAG: record["ownership_nonce"]},
                        oauth2ProviderConfigInput={"customOauth2ProviderConfig": {
                            "oauthDiscovery": {"authorizationServerMetadata": {
                                key: value for key, value in self.plan.metadata.items() if key != "tokenEndpointAuthMethods"
                            }},
                            "clientId": self.plan.client_id,
                            "clientSecretSource": "EXTERNAL",
                            "clientSecretConfig": {"secretId": self.plan.secret_id, "jsonKey": "client_secret"},
                            "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
                        }},
                    )
                except Exception:
                    raise ProbeBlocked("Provider create unconfirmed; reconcile before retry") from None
                provider = self._get(self.control.get_oauth2_credential_provider, identity.provider_name)
                if not provider:
                    raise ProbeBlocked("Provider creation has not been read back")
                self._verify_provider(provider)
            record["provider_arn"] = self._owned_arn(provider, "credentialProviderArn", "provider_arn", record)
            record.update(status="ready", phase="ready", uncertain=False)
            self.ledger._save(record)
            return {"status": "ready", "mode": "retained-deployment-client", "provider": identity.provider_name,
                    "callback_url": provider["callbackUrl"]}

    def cleanup(self):
        """Delete only tagged attempt resources; never infer user-grant revocation."""
        identity = self.plan.identity
        resources = (
            ("provider", identity.provider_name, self.control.get_oauth2_credential_provider,
             self.control.delete_oauth2_credential_provider, "credentialProviderArn"),
            ("workload", identity.workload_name, self.control.get_workload_identity,
             self.control.delete_workload_identity, "workloadIdentityArn"),
        )
        with self.ledger._locked():
            record = self.ledger._read()
            if (not isinstance(record, dict) or record.get("setup_digest") != self.plan.digest
                    or not isinstance(record.get("ownership_nonce"), str)
                    or not re.fullmatch(r"[0-9a-f]{48}", record["ownership_nonce"])
                    or record.get("status") not in {"ready", "preparing", "cleaning", "cleaned"}):
                raise ProbeBlocked("No tagged setup attempt authorizes resource cleanup")
            if record["status"] == "cleaned":
                if any(self._get(reader, name) is not None for _, name, reader, *_ in resources):
                    raise ProbeBlocked("A cleaned resource name has reappeared")
                return {"status": "cleaned", "controlled_cleanup": "verified", "provider_grant_revocation": "unproven"}
            if record["status"] != "cleaning":
                record.update(status="cleaning", cleanup_phase="provider_pending")
                self.ledger._save(record)
            for kind, name, reader, delete, arn_field in resources:
                pending, deleting = f"{kind}_pending", f"{kind}_deleting"
                if record["cleanup_phase"] not in {pending, deleting}:
                    if kind == "provider" and self._get(reader, name) is not None:
                        raise ProbeBlocked("A previously removed provider has reappeared")
                    continue
                current = self._get(reader, name)
                recorded_arn = f"{kind}_arn"
                if current is None:
                    if record["cleanup_phase"] == pending and (record.get(recorded_arn) or record.get("uncertain")):
                        raise ProbeBlocked("Identity creation or unexpected removal remains unresolved")
                else:
                    if kind == "provider":
                        if current.get("name") != name:
                            raise ProbeBlocked("Identity provider name differs from this setup attempt")
                    else:
                        self._verify_workload(current)
                    record[recorded_arn] = self._owned_arn(current, arn_field, recorded_arn, record)
                    if kind == "provider":
                        status = current.get("status")
                        if status == "DELETING":
                            record["cleanup_phase"] = deleting
                            self.ledger._save(record)
                            raise ProbeBlocked("Identity provider deletion is still in progress; wait for readback")
                        if status in {"CREATING", "UPDATING"}:
                            raise ProbeBlocked("Identity provider setup is still in progress; wait for readback")
                        if status not in {"READY", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}:
                            raise ProbeBlocked("Identity provider status does not authorize cleanup")
                    record["cleanup_phase"] = deleting
                    self.ledger._save(record)
                    try:
                        delete(name=name)
                    except Exception:
                        raise ProbeBlocked("Identity delete reply is ambiguous; reconcile before retry") from None
                    if self._get(reader, name) is not None:
                        raise ProbeBlocked("Identity deletion is not yet confirmed by readback")
                record["cleanup_phase"] = "workload_pending" if kind == "provider" else "verifying"
                record["uncertain"] = False
                self.ledger._save(record)
            if any(self._get(reader, name) is not None for _, name, reader, *_ in resources):
                raise ProbeBlocked("Independent Identity removal readback is incomplete")
            record.update(status="cleaned", cleanup_phase="verified")
            self.ledger._save(record)
            return {"status": "cleaned", "controlled_cleanup": "verified", "provider_grant_revocation": "unproven"}
