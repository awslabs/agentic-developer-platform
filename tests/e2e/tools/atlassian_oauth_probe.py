"""Standalone Atlassian MCP v2 probe using AgentCore Identity only.

Caller-supplied SDK clients must have independent core and adapter authorities.
No user token or code exchange is retained here.
"""

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit


RESOURCE = "https://mcp.atlassian.com/v2/mcp"
READ_SCOPES = (
    "search:jira:agent-interface",
    "read:jira:agent-interface",
    "search:confluence:agent-interface",
    "read:confluence:agent-interface",
)
IDENTITY_SCOPES = ("read:me", "read:account", "offline_access")
CONSENT_SCOPES = READ_SCOPES + IDENTITY_SCOPES
ALLOWED_SCOPES = frozenset(CONSENT_SCOPES + ("write:jira:agent-interface",))


class ProbeBlocked(Exception):
    """A required Identity or provider behavior has not been established."""


@dataclass(frozen=True)
class IdentityConfig:
    workload_name: str
    provider_name: str
    return_url: str
    scopes: tuple[str, ...] = CONSENT_SCOPES
    resource: str = RESOURCE
    issuer: str | None = None
    metadata_sha256: str | None = None

    def __post_init__(self):
        parsed = urlsplit(self.return_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("An HTTPS ADP return URL without credentials or fragment is required")
        if not self.workload_name or not self.provider_name:
            raise ValueError("Isolated Identity workload and provider names are required")
        if self.resource != RESOURCE:
            raise ValueError("Legacy OAuth resources cannot be used for MCP v2")
        if len(self.scopes) != len(set(self.scopes)) or not set(CONSENT_SCOPES) <= set(self.scopes) or not set(self.scopes) <= ALLOWED_SCOPES:
            raise ValueError("MCP v2 product, subject/site and managed-refresh scopes are required without legacy/admin scopes")
        if self.issuer is not None:
            issuer = urlsplit(self.issuer)
            if issuer.scheme != "https" or issuer.netloc != "auth.atlassian.com" or not issuer.path.strip("/") or issuer.query or issuer.fragment:
                raise ValueError("The MCP issuer must be the exact Atlassian authorization-server issuer")

    @classmethod
    def from_metadata(cls, workload_name, provider_name, return_url, protected_resource, authorization_server, scopes=CONSENT_SCOPES):
        issuer_list = protected_resource.get("authorization_servers")
        issuer = authorization_server.get("issuer")
        if protected_resource.get("resource") != RESOURCE or not isinstance(issuer_list, list) or len(issuer_list) != 1 or issuer_list[0] != issuer:
            raise ValueError("MCP v2 resource and exact issuer metadata must agree")
        if not set(scopes) <= set(protected_resource.get("scopes_supported", [])):
            raise ValueError("Requested scopes are not advertised by the pinned MCP v2 resource")
        if "S256" not in authorization_server.get("code_challenge_methods_supported", []):
            raise ValueError("MCP v2 issuer does not advertise S256 PKCE")
        if not {"authorization_code", "refresh_token"} <= set(authorization_server.get("grant_types_supported", [])):
            raise ValueError("MCP v2 issuer does not advertise required user/refresh flows")
        digest = hashlib.sha256(json.dumps([protected_resource, authorization_server], sort_keys=True).encode()).hexdigest()
        return cls(workload_name, provider_name, return_url, tuple(scopes), RESOURCE, issuer, digest)


class IdentityLifecycle:
    """Only calls Identity's user-federation SDK, using distinct caller roles."""

    def __init__(self, config: IdentityConfig, core_identity, adapter_identity):
        if core_identity is adapter_identity:
            raise ValueError("Core setup and adapter retrieval require distinct SDK authorities")
        self.config = config
        self.core_identity = core_identity
        self.adapter_identity = adapter_identity

    def start(self, user_id: str, state: str) -> dict:
        if not user_id or not state:
            raise ValueError("An owner and bound browser state are required")
        if not self.config.metadata_sha256 or not self.config.issuer:
            raise ProbeBlocked("V2 resource, issuer and scopes must be pinned before Identity consent")
        workload = self.core_identity.get_workload_access_token_for_user_id(
            workloadName=self.config.workload_name, userId=user_id
        )
        response = self.core_identity.get_resource_oauth2_token(
            workloadIdentityToken=workload["workloadAccessToken"],
            resourceCredentialProviderName=self.config.provider_name,
            scopes=list(self.config.scopes),
            resources=[self.config.resource],
            oauth2Flow="USER_FEDERATION",
            forceAuthentication=True,
            resourceOauth2ReturnUrl=self.config.return_url,
            customState=state,
        )
        if response.get("sessionStatus") != "IN_PROGRESS" or not response.get("sessionUri") or not response.get("authorizationUrl"):
            raise ProbeBlocked("Identity did not produce a consent session")
        return {"session_uri": response["sessionUri"], "authorization_url": response["authorizationUrl"]}

    def complete(self, user_id: str, session_uri: str) -> None:
        if not user_id or not session_uri:
            raise ValueError("An owner and Identity session are required")
        self.core_identity.complete_resource_token_auth(
            userIdentifier={"userId": user_id}, sessionUri=session_uri
        )

    def with_access_token(self, user_id: str, dispatch):
        """Use a transient token for one adapter observation; never return it."""
        if not user_id:
            raise ValueError("An owner is required")
        if not self.config.metadata_sha256 or not self.config.issuer:
            raise ProbeBlocked("V2 resource, issuer and scopes must be pinned before token retrieval")
        workload = self.adapter_identity.get_workload_access_token_for_user_id(
            workloadName=self.config.workload_name, userId=user_id
        )
        response = self.adapter_identity.get_resource_oauth2_token(
            workloadIdentityToken=workload["workloadAccessToken"],
            resourceCredentialProviderName=self.config.provider_name,
            scopes=list(self.config.scopes),
            resources=[self.config.resource],
            oauth2Flow="USER_FEDERATION",
        )
        token = response.get("accessToken")
        if response.get("sessionStatus") in {"IN_PROGRESS", "FAILED"} or not isinstance(token, str) or not token or len(token) > 16384 or any(char.isspace() for char in token):
            raise ProbeBlocked("Identity has not returned an authorized token")
        return dispatch(token)

    def revoke(self, user_id: str) -> None:
        raise ProbeBlocked("No supported per-user Identity revocation API or provider effect has been proven")


def proof_auth_lifecycle(ledger, owner, foreign_owner, consent_fixture, expiry_fixture, provider_oracle, expected_subject, expected_site, source_revision=None):
    """Run generated owner/expiry safety controls; never attest real provider consent."""
    if not owner or not foreign_owner or owner == foreign_owner or not expected_subject or not expected_site:
        raise ValueError("Distinct owners and independently expected provider identity/site are required")
    if source_revision is not None and (not isinstance(source_revision, str) or len(source_revision) != 40 or any(char not in "0123456789abcdef" for char in source_revision)):
        raise ValueError("The optional source revision must be an exact Git commit")
    attempt_started = False
    try:
        browser = ledger.begin(owner)
        attempt_started = True
        if consent_fixture.authorize(owner, browser["authorization_url"], browser["state"]) is not True:
            raise ProbeBlocked("Generated consenting fixture did not authorize this attempt")
        ledger.complete(owner, browser["state"])
        restarted = type(ledger)(ledger.path, ledger.lifecycle)
        first = restarted.observe(owner, provider_oracle.observe)
        if first != {"subject": expected_subject, "site": expected_site}:
            raise ProbeBlocked("Independent provider subject or site does not match the fixture")
        before_denial = provider_oracle.call_count()
        try:
            restarted.observe(foreign_owner, provider_oracle.observe)
        except ProbeBlocked:
            pass
        else:
            raise ProbeBlocked("Foreign owner was allowed to use the Identity grant")
        if provider_oracle.call_count() != before_denial:
            raise ProbeBlocked("An unauthorized provider effect was observed")
        if expiry_fixture.expire(owner) is not True:
            raise ProbeBlocked("The fixture did not prove the original access expired")
        second = restarted.observe(owner, provider_oracle.observe)
        if second != first or provider_oracle.call_count() != before_denial + 1:
            raise ProbeBlocked("Same-owner provider access after expiry was not observed")
        if consent_fixture.call_count() != 1:
            raise ProbeBlocked("Expiry unexpectedly triggered new user consent")
        return {
            **({"source_revision": source_revision, "design_revision": "3454ce6b73d7a263e4e90979d6eaa49f5896d098"} if source_revision else {}),
            "scenario_id": "SCN-01",
            "controlled_status": "pass",
            "evidence_kind": "generated-fixture",
            "provider_status": "unproven",
            "managed_refresh": "simulated-only",
            "remote_revocation": "unproven",
            "cleanup": "pending-remote-verification",
            "role_boundary": "sdk-clients-distinct; live-role-arns-unverified",
        }
    except Exception:
        raise ProbeBlocked("Generated Identity lifecycle controls failed") from None
    finally:
        if attempt_started and ledger.status(owner) in {"consent_pending", "completing", "completed"}:
            ledger.deny(owner)


def proof_capability_contract(ledger, owner, selected_site, granted_scopes, accessible_sites,
                              documented_methods, documented_tools_sha256, expected_catalogue_sha256,
                              pinned_schema_hashes, transport_factory, source_revision=None, *, tenant=None):
    """Compare independent public docs and exact full-catalogue fixture hashes."""
    from tests.e2e.tools.atlassian_catalogue import discover
    from tests.e2e.tools.atlassian_contract import CONTRACT_PATH, load_contract

    contract = load_contract()
    if source_revision is not None and (not isinstance(source_revision, str) or len(source_revision) != 40 or any(char not in "0123456789abcdef" for char in source_revision)):
        raise ProbeBlocked("The source revision must be an exact Git commit")
    expected_methods = {entry["method"]: entry["scope"] for entry in contract["operations"]}
    if documented_tools_sha256 != contract["documented_tools_sha256"] or not isinstance(documented_methods, dict):
        raise ProbeBlocked("Independent documented-tools snapshot is not pinned")
    if any(documented_methods.get(method) != scope for method, scope in expected_methods.items()):
        raise ProbeBlocked("A required method or scope is not independently documented")
    if not isinstance(expected_catalogue_sha256, str) or len(expected_catalogue_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_catalogue_sha256):
        raise ProbeBlocked("The full provider catalogue needs an exact independent digest")
    report = discover(ledger, owner, selected_site, granted_scopes, accessible_sites,
                      pinned_schema_hashes=pinned_schema_hashes, transport_factory=transport_factory, tenant=tenant)
    if report["catalogue_sha256"] != expected_catalogue_sha256:
        raise ProbeBlocked("The complete provider catalogue changed from its pinned snapshot")
    if report["status"] != "candidate_match":
        raise ProbeBlocked("Input schemas remain unpinned; do not accept provisional names as executable")
    return {
        **({"source_revision": source_revision, "design_revision": "3454ce6b73d7a263e4e90979d6eaa49f5896d098"} if source_revision else {}),
        "scenario_id": "SCN-02",
        "controlled_status": "pass",
        "evidence_kind": "generated-fixture",
        "live_status": "unproven",
        "contract_revision": contract["revision"],
        "contract_sha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
        "documentation_sha256": documented_tools_sha256,
        "catalogue_sha256": report["catalogue_sha256"],
        "schema_sha256": report["schema_sha256"],
        "site_sha256": report["site_sha256"],
        "tool_count": report["tool_count"],
    }


def proof_handoff_review(lifecycle_report, capability_report, expected_source_revision, expected_contract_sha256, matrix_path=None):
    """Review source-bound generated evidence, never provider activation."""
    from tests.e2e.tools.atlassian_contract import CONTRACT_PATH, METHODS, load_contract
    from tests.e2e.tools.atlassian_handoff import DESIGN_REVISION, MATRIX_PATH, review_handoff

    matrix_path = MATRIX_PATH if matrix_path is None else matrix_path
    if not isinstance(expected_source_revision, str) or len(expected_source_revision) != 40 or any(char not in "0123456789abcdef" for char in expected_source_revision):
        raise ProbeBlocked("An immutable expected source revision is required")
    actual_contract_digest = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()
    if expected_contract_sha256 != actual_contract_digest or capability_report.get("contract_sha256") != actual_contract_digest:
        raise ProbeBlocked("Capability evidence does not match the pinned operation contract")
    for scenario, report in (("SCN-01", lifecycle_report), ("SCN-02", capability_report)):
        if (report.get("scenario_id") != scenario or report.get("controlled_status") != "pass"
                or report.get("evidence_kind") != "generated-fixture"
                or report.get("source_revision") != expected_source_revision
                or report.get("design_revision") != DESIGN_REVISION):
            raise ProbeBlocked("A generated proof is missing or from a different source/design revision")
    if (lifecycle_report.get("provider_status") != "unproven"
            or lifecycle_report.get("remote_revocation") != "unproven"
            or lifecycle_report.get("cleanup") != "pending-remote-verification"
            or capability_report.get("live_status") != "unproven"
            or capability_report.get("documentation_sha256") != load_contract()["documented_tools_sha256"]):
        raise ProbeBlocked("Controlled observations cannot establish live provider readiness")
    schemas = capability_report.get("schema_sha256", {})
    if set(schemas) != set(METHODS.values()) or any(not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest) for digest in schemas.values()):
        raise ProbeBlocked("The handoff requires every named input schema digest")
    result = review_handoff({
        "source_revision": expected_source_revision,
        "design_revision": DESIGN_REVISION,
        "phase": "controlled-development-handoff",
        "live_status": "unproven",
        "scenarios": {"SCN-01": "pass", "SCN-02": "pass", "SCN-03": "pass"},
    }, matrix_path)
    return {
        "scenario_id": "SCN-03",
        "controlled_status": "pass" if result["controlled_status"] == "candidate" else "blocked",
        "handoff_readiness": result["controlled_status"],
        "live_status": result["live_status"],
        "source_revision": expected_source_revision,
        "design_revision": DESIGN_REVISION,
        "contract_sha256": actual_contract_digest,
        "activation_blockers": result["activation_blockers"],
    }
