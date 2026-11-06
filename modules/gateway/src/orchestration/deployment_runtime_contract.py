"""Bounded runtime evidence: workflow completion never supplies these facts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .delivery_contract import WorkflowRuntimeComponent

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=512)]
CHAT_MANIFEST_KEYS = frozenset(
    {
        "scaledjob/chat-agent-worker",
        "job/adp-chat-supervisor-once",
        "configmap/chat-crew-pod-template",
        "validatingadmissionpolicy/adp-chat-sandbox-template",
        "validatingadmissionpolicybinding/adp-chat-sandbox-template",
        "validatingadmissionpolicy/adp-chat-supervisor-pods",
        "validatingadmissionpolicybinding/adp-chat-supervisor-pods",
        "validatingadmissionpolicy/adp-crew-pod-template",
        "validatingadmissionpolicybinding/adp-crew-pod-template",
        "networkpolicy/chat-sandbox-deny",
    }
)
Component = Literal[
    "gateway-backend",
    "gateway-frontend",
    "gateway-migrations",
    "qualification-api",
    "qualification-client",
    "tools-facade",
    "tool-adapters",
    "insights",
    "tools-interceptor",
    "tools-platform",
    "knowledge-door",
    "agent-worker",
    "chat-worker",
    "dev-box",
]


class StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DevBoxRuntimeEvidence(StrictEvidence):
    schema_version: Literal[1]
    component: Literal["dev-box"]
    release_manifest_sha256: Hash
    source_revision: Sha
    definition_revision: Sha
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    module_sha256: Hash
    image_id: str = Field(pattern=r"^ami-[0-9a-f]{8,17}$")
    launch_template_id: str = Field(pattern=r"^lt-[0-9a-f]{8,17}$")
    launch_template_version: int = Field(gt=0)
    guest_security_group_id: str = Field(pattern=r"^sg-[0-9a-f]{8,17}$")
    gateway_security_group_id: str = Field(pattern=r"^sg-[0-9a-f]{8,17}$")
    private_subnet_id: str = Field(pattern=r"^subnet-[0-9a-f]{8,17}$")
    guest_instance_profile_arn: Name
    guest_role_arn: Name
    controller_role_arn: Name
    run_table_arn: str | None = Field(default=None, pattern=r"^arn:aws:dynamodb:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:table/[A-Za-z0-9_.-]+$")
    # Customer KMS key encrypting the run registry (#8139): names the one extra KMS statement the
    # controller's owner-bound-host-runs policy may carry. Optional so receipts written before
    # #8145 (AWS-owned key, or an older runtime.py) stay valid.
    run_table_kms_key_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws:kms:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:key/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    gateway_execute_arn: str | None = Field(default=None, pattern=r"^arn:aws:execute-api:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:[a-z0-9]+/[A-Za-z0-9_-]+$")
    dashboard_enabled: bool
    controller_status: Literal["absent", "present"]
    controller_digest: Digest | None = None
    controller_ready: bool
    verified_at: datetime

    @model_validator(mode="after")
    def readiness(self):
        if self.gateway_execute_arn and not self.run_table_arn:
            raise ValueError("Dev Box gateway access requires a run registry")
        if self.run_table_kms_key_arn and not self.run_table_arn:
            raise ValueError("Dev Box run registry key requires a run registry")
        for service, arn in (
            ("dynamodb", self.run_table_arn),
            ("kms", self.run_table_kms_key_arn),
            ("execute-api", self.gateway_execute_arn),
        ):
            if arn and not arn.startswith(f"arn:aws:{service}:{self.region}:{self.account_id}:"):
                raise ValueError("Dev Box host authority must use the selected account and region")
        if self.verified_at.tzinfo is None or self.controller_ready != (self.controller_status == "present"):
            raise ValueError("Dev Box controller readiness must be an observed, timestamped state")
        if self.controller_ready != (self.controller_digest is not None):
            raise ValueError("Dev Box controller requires an immutable observed image digest")
        return self


class ProductRecoveryEvidence(StrictEvidence):
    baseline_source_revision: Sha
    baseline_image_digest: Digest
    admissions_disabled_before_rollback: Literal[True]
    migration_mode: Literal["retained"]
    data_readback: Literal["pending"]


class ReleaseArtifact(StrictEvidence):
    """Built artifact hashes published by the pinned workflow, before acceptance."""

    schema_version: Literal[1]
    repository_id: int = Field(gt=0)
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    source_revision: Sha
    workflow_revision: Sha
    workflow_path: Name
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    resource_id: Name
    component: Component
    image_digest: Digest | None = None
    product_profile_enabled: bool | None = None
    product_onboarding_enabled: bool | None = None
    product_profile_schema_version: str | None = Field(default=None, pattern=r"^1\.0$")
    migration_head: Name | None = None
    cli_manifest_sha256: Hash | None = None
    qualification_job: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    product_recovery: ProductRecoveryEvidence | None = None
    supervisor_image_digest: Digest | None = None
    manifests_sha256: Hash | None = None
    dev_box_receipt: DevBoxRuntimeEvidence | None = None
    lambda_version: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    code_sha256: str | None = Field(default=None, pattern=r"^[A-Za-z0-9+/]{43}=$")
    gateway_id: Name | None = None
    gateway_url: Name | None = None
    target_ids: list[Name] = Field(default_factory=list, max_length=32)
    target_sync_revision: Hash | None = None
    tools_list_sha256: Hash | None = None
    # tools-platform releases (modules/tools/platform/scripts/deployment_evidence.py)
    # carry the gateway's reviewed configuration alongside the identity fields:
    # the interceptor version and code hash the release gate verified, the roles
    # and keys the gateway was wired to, and the name→id target map that
    # `target_ids` is derived from. They are bounded and cross-checked below so a
    # richer receipt cannot smuggle a different gateway or interceptor past review.
    gateway_arn: Name | None = None
    targets: dict[Name, Name] = Field(default_factory=dict, max_length=32)
    interceptor_lambda_arn: str | None = Field(
        default=None, pattern=r"^arn:aws(?:-[a-z]+)*:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?$"
    )
    interceptor_version: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    interceptor_code_sha256: str | None = Field(default=None, pattern=r"^[A-Za-z0-9+/]{43}=$")
    kms_key_arn: str | None = Field(default=None, pattern=r"^arn:aws(?:-[a-z]+)*:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-f-]{36}$")
    assertion_signing_key_arn: str | None = Field(default=None, pattern=r"^arn:aws(?:-[a-z]+)*:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-f-]{36}$")
    target_role_arn: str | None = Field(default=None, pattern=r"^arn:aws(?:-[a-z]+)*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
    caller_role_arn: str | None = Field(default=None, pattern=r"^arn:aws(?:-[a-z]+)*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
    execution_table_name: Name | None = None
    environment: Literal["dev", "staging", "prod"] | None = None
    definition_revision: Sha | None = None
    adp_correlation: str | None = Field(default=None, max_length=255)
    door_identity_sha256: Hash | None = None
    assets: dict[Name, Hash] = Field(default_factory=dict, max_length=256)
    # Both documents travel inside the existing authenticated release payload.
    # Runtime state must never supply its own expected manifest or contract.
    adapter_manifest: dict[str, JsonValue] | None = None
    adapter_contract: dict[str, JsonValue] | None = None
    compatibility: dict[str, JsonValue] | None = None
    produced_at: datetime

    @model_validator(mode="after")
    def complete(self):
        if self.produced_at.tzinfo is None:
            raise ValueError("release evidence must carry an aware timestamp")
        if self.product_recovery and (
            self.component != "gateway-backend" or self.product_profile_enabled is not False or self.product_onboarding_enabled is not False
        ):
            raise ValueError("Product rollback must disable new admissions")
        if self.component == "dev-box":
            receipt = self.dev_box_receipt
            if (
                receipt is None
                or receipt.source_revision != self.source_revision
                or receipt.definition_revision != self.workflow_revision
                or receipt.account_id != self.account_id
                or receipt.region != self.region
                or receipt.launch_template_id != self.resource_id
                or self.image_digest != receipt.controller_digest
                or self.assets != {"dev-box-module.tar.gz": receipt.module_sha256}
            ):
                raise ValueError("Dev Box release and provider receipt identities differ")
        elif self.dev_box_receipt is not None:
            raise ValueError("Dev Box receipt cannot satisfy another component")
        elif self.component == "qualification-api":
            if self.image_digest is None or self.assets or len(self.resource_id.split("/")) != 3:
                raise ValueError("qualification API requires a pinned image and approved workload target")
        elif self.component == "qualification-client":
            if (
                self.image_digest is None
                or self.assets
                or len(self.resource_id.split("/")) != 2
                or not self.qualification_job
                or not self.cli_manifest_sha256
            ):
                raise ValueError("qualification client requires a pinned image and job execution manifest")
        elif self.component == "gateway-frontend":
            if self.image_digest is not None or "index.html" not in self.assets:
                raise ValueError("frontend release requires actual build hashes")
            for path in self.assets:
                if path.startswith("/") or ".." in path.split("/") or "\\" in path or any(ord(c) < 32 for c in path):
                    raise ValueError("unsafe build artifact path")
        elif self.component in {"agent-worker", "chat-worker"}:
            manifest_hash = hashlib.sha256(json.dumps(self.assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if (
                self.image_digest is None
                or self.supervisor_image_digest is None
                or self.image_digest == self.supervisor_image_digest
                or set(self.assets) != CHAT_MANIFEST_KEYS
                or self.manifests_sha256 != manifest_hash
            ):
                raise ValueError("chat release requires distinct pinned images and all hashed manifests")
        elif self.component == "tools-interceptor":
            if self.image_digest is not None or self.assets or not self.lambda_version or not self.code_sha256:
                raise ValueError("interceptor release requires a numbered Lambda version and code hash")
        elif self.component == "tool-adapters":
            if self.image_digest is None or set(self.assets) != {"adapter-manifest.json", "adapter-contract.json"}:
                raise ValueError("adapter release requires a pinned image and both reviewed contract hashes")
            for name, document in (("adapter-manifest.json", self.adapter_manifest), ("adapter-contract.json", self.adapter_contract)):
                if document is None:
                    raise ValueError("adapter release requires both expected documents")
                payload = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                if len(payload) > 24 * 1024 or hashlib.sha256(payload).hexdigest() != self.assets[name]:
                    raise ValueError("adapter expected document is oversized or differs from its reviewed hash")
            if self.compatibility is not None:
                self._validate_adapter_compatibility()
        elif self.component == "tools-platform":
            if (
                self.image_digest is not None
                or self.assets
                or not self.gateway_id
                or not self.gateway_url
                or not self.target_ids
                or not self.target_sync_revision
                or not self.tools_list_sha256
            ):
                raise ValueError("gateway release requires target sync and discovery evidence")
            if self.gateway_arn is not None and (self.gateway_arn != self.resource_id or not self.gateway_arn.endswith("/" + self.gateway_id)):
                raise ValueError("gateway release names a different gateway than its target")
            if self.targets and sorted(self.targets.values()) != sorted(self.target_ids):
                raise ValueError("gateway release target map and target ids differ")
            if self.definition_revision is not None and self.definition_revision != self.workflow_revision:
                raise ValueError("gateway release definition revision differs from the workflow revision")
            if (self.interceptor_version is None) != (self.interceptor_code_sha256 is None):
                raise ValueError("gateway release interceptor version and code hash must be recorded together")
        elif self.component == "knowledge-door":
            if self.image_digest is None or self.assets or not self.door_identity_sha256:
                raise ValueError("Door release requires its image and identity-key digest")
        elif self.component == "insights":
            if (
                not self.assets
                or len(self.assets) > 16
                or any(not path.startswith("platform/") or not path.endswith(".tf") or ".." in path.split("/") for path in self.assets)
            ):
                raise ValueError("insights release requires bounded infrastructure hashes")
        elif self.image_digest is None or self.assets:
            raise ValueError("backend and migration releases require a container digest")
        if self.component != "qualification-client" and self.qualification_job is not None:
            raise ValueError("qualification Job identity belongs to client releases only")
        if self.component != "tool-adapters" and (
            self.adapter_manifest is not None or self.adapter_contract is not None or self.compatibility is not None
        ):
            raise ValueError("adapter expected documents belong to tool-adapters releases only")
        if self.component != "tool-adapters" and self.compatibility is not None:
            raise ValueError("adapter compatibility belongs to tool-adapters releases only")
        if self.component != "tools-platform" and (
            self.gateway_arn
            or self.targets
            or self.interceptor_lambda_arn
            or self.interceptor_version
            or self.interceptor_code_sha256
            or self.kms_key_arn
            or self.assertion_signing_key_arn
            or self.target_role_arn
            or self.caller_role_arn
            or self.execution_table_name
        ):
            raise ValueError("gateway configuration fields belong to tools-platform releases only")
        return self

    def _validate_adapter_compatibility(self):
        try:
            configurations = [
                item["data"]["config.json"]
                for item in self.adapter_manifest["items"]
                if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "tool-adapters-contract"
            ]
            if len(configurations) != 1:
                raise ValueError("expected one adapter configuration")
            raw = configurations[0]
            configuration = json.loads(raw)
            release = configuration["release"]
            mode = configuration["mode"]
            observation = configuration.get("observation")
            if (
                release["source_revision"] != self.source_revision
                or release["image_digest"] != self.image_digest
                or release["contract_version"] != self.adapter_contract["contract_version"]
                or mode not in ("foundation", "observation")
                or (mode == "observation") != (observation is not None)
            ):
                raise ValueError("adapter configuration differs from release")
            expected = {
                "adapter": {
                    "release": release,
                    "mode": mode,
                    "contract": self.adapter_contract,
                    "configuration_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "manifest_sha256": self.assets["adapter-manifest.json"],
                }
            }
            if observation is not None:
                expected["identity"] = {
                    "provider": observation["identity_provider"],
                    "workload": observation["identity_workload"],
                    "config_revision": observation["config_revision"],
                    "subject_schema_sha256": observation["subject_schema_sha256"],
                    "sites_schema_sha256": observation["sites_schema_sha256"],
                }
            encoded = json.dumps(self.compatibility, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if len(encoded) > 24 * 1024 or encoded != json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False).encode():
                raise ValueError("adapter compatibility differs from reviewed documents")
        except (KeyError, TypeError, ValueError):
            raise ValueError("adapter compatibility must match the reviewed configuration and contract") from None


class RuntimeComponent(StrictEvidence):
    component: Component
    actual_revision: Sha
    artifact_hash: Hash
    image_digest: Digest | None = None
    manifests_sha256: Hash | None = None
    adapter_contract_sha256: Hash | None = None
    adapter_probe_path: Literal["api_server_proxy"] | None = None
    host_image_id: str | None = Field(default=None, pattern=r"^ami-[0-9a-f]{8,17}$")
    host_template_id: str | None = Field(default=None, pattern=r"^lt-[0-9a-f]{8,17}$")
    lambda_version: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    code_sha256: str | None = Field(default=None, pattern=r"^[A-Za-z0-9+/]{43}=$")
    gateway_id: Name | None = None
    gateway_url: Name | None = None
    target_ids: list[Name] = Field(default_factory=list, max_length=32)
    target_sync_revision: Hash | None = None
    tools_list_sha256: Hash | None = None
    # How the served listing was established: read live as the connection role, or
    # taken from the façade's release smoke because a fail-closed interceptor refuses
    # every unasserted caller (the refusal itself is then the live proof).
    tools_list_source: Literal["live_listing", "release_smoke"] | None = None
    interceptor_lambda_arn: str | None = Field(default=None, min_length=1, max_length=255)
    interceptor_fail_closed: bool | None = None
    door_identity_sha256: Hash | None = None
    # How the Door's identity enforcement was probed: over the cluster network as an
    # admitted peer (the verifier runs inside the target cluster) or through the API
    # server's scoped service proxy (#7870).
    door_probe_path: Literal["cluster_network", "api_server_proxy"] | None = None
    healthy: Literal[True]
    evidence_ref: Name
    observed_at: datetime
    # Actual SQL revision, compared with the heads in the verified running image.
    migration_head: Name | None = None
    cli_manifest_sha256: Hash | None = None
    product_profile_enabled: bool | None = None
    product_onboarding_enabled: bool | None = None
    tick_digest: Digest | None = None
    pod_uids: list[Name] = Field(default_factory=list, max_length=100)
    asset_count: int = Field(default=0, ge=0, le=256)

    @model_validator(mode="after")
    def complete(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("runtime observation timestamp must be aware")
        if self.component == "dev-box":
            if (
                self.image_digest is None
                or self.host_image_id is None
                or self.host_template_id is None
                or self.pod_uids
                or self.migration_head is not None
            ):
                raise ValueError("Dev Box runtime requires a verified host and ready controller")
        elif self.host_image_id is not None or self.host_template_id is not None:
            raise ValueError("EC2 host identity cannot satisfy another component")
        elif self.component == "qualification-api":
            if self.image_digest is None or len(self.pod_uids) != 1 or self.migration_head is not None or self.tick_digest is not None:
                raise ValueError("qualification API requires one observed serving image")
        elif self.component == "qualification-client":
            if (
                self.image_digest is None
                or len(self.pod_uids) != 1
                or self.migration_head is not None
                or self.tick_digest is not None
                or not self.cli_manifest_sha256
            ):
                raise ValueError("qualification client requires one verified completed Job")
        elif self.component == "gateway-frontend":
            if self.asset_count == 0 or self.image_digest is not None:
                raise ValueError("frontend runtime must verify published build bytes")
        elif self.component in {"agent-worker", "chat-worker"}:
            if self.image_digest is None or self.manifests_sha256 is None or self.asset_count != len(CHAT_MANIFEST_KEYS):
                raise ValueError("chat runtime requires pinned image and verified manifest evidence")
        elif self.component == "tool-adapters":
            if (
                self.image_digest is None
                or self.manifests_sha256 is None
                or self.adapter_contract_sha256 is None
                or self.adapter_probe_path != "api_server_proxy"
                or len(self.pod_uids) != 2
                or len(set(self.pod_uids)) != 2
                or self.migration_head is not None
                or self.asset_count != 0
            ):
                raise ValueError("adapter runtime requires two serving pods and independently probed contracts")
        elif self.component == "tools-interceptor":
            if self.image_digest is not None or not self.lambda_version or not self.code_sha256 or self.pod_uids:
                raise ValueError("interceptor runtime requires the serving Lambda alias and version hash")
        elif self.component == "tools-platform":
            if (
                self.image_digest is not None
                or not self.gateway_id
                or not self.gateway_url
                or not self.target_ids
                or not self.target_sync_revision
                or not self.tools_list_sha256
                or self.pod_uids
            ):
                raise ValueError("gateway runtime requires target sync and discovery evidence")
        elif self.component == "knowledge-door":
            if self.image_digest is None or not self.pod_uids or not self.door_identity_sha256 or self.door_probe_path is None:
                raise ValueError("Door runtime requires serving pods, identity-contract evidence and a named probe path")
        elif self.component == "insights":
            if self.image_digest is None or not self.pod_uids or self.migration_head is not None or self.asset_count == 0:
                raise ValueError("insights runtime requires serving pods and infrastructure evidence")
        elif self.image_digest is None or not self.pod_uids or (self.component != "tools-facade" and not self.migration_head):
            raise ValueError("runtime requires serving pods and actual schema evidence")
        if (
            self.component == "gateway-backend"
            and any(item is not None for item in (self.cli_manifest_sha256, self.product_profile_enabled, self.product_onboarding_enabled))
            and (self.cli_manifest_sha256 is None or self.product_profile_enabled is None or self.product_onboarding_enabled is None)
        ):
            raise ValueError("Product runtime evidence must include both admission flags and the served CLI manifest")
        if (self.component not in {"gateway-backend", "qualification-client"} and self.cli_manifest_sha256 is not None) or (
            self.component != "gateway-backend" and any(item is not None for item in (self.product_profile_enabled, self.product_onboarding_enabled))
        ):
            raise ValueError("Product runtime evidence belongs to the Gateway backend")
        if self.component == "gateway-backend" and self.tick_digest != self.image_digest:
            raise ValueError("scheduled tick must match the gateway release digest")
        if self.component != "knowledge-door" and self.door_probe_path is not None:
            raise ValueError("a Door probe path belongs to knowledge-door runtime only")
        if self.component != "tool-adapters" and (self.adapter_contract_sha256 is not None or self.adapter_probe_path is not None):
            raise ValueError("adapter contract evidence belongs to tool-adapters runtime only")
        return self


class DeploymentReceipt(StrictEvidence):
    """D3 runtime acceptance for E1; this does not accept an evaluation node."""

    schema_version: Literal[1, 2] = 1
    org_id: Name
    execution_id: Name
    node_id: Name
    flow_id: Name
    cycle: int = Field(ge=1)
    accepted_plan_version: int = Field(ge=1)
    claim_id: Name
    claim_generation: int = Field(ge=1)
    operation_key: Name
    repo: Name
    source_revision: Sha
    actual_revision: Sha
    merge_operation_key: Name
    workflow_operation_keys: list[Name] = Field(max_length=32)
    manifest_entry_ids: list[Name] = Field(max_length=32)
    targets: list[dict] = Field(max_length=32)
    components: list[RuntimeComponent | WorkflowRuntimeComponent] = Field(max_length=32)
    delivery_complete: bool = False
    docs_only: bool = False
    observed_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def complete(self):
        if any(isinstance(item, WorkflowRuntimeComponent) != (self.schema_version == 2) for item in self.components):
            raise ValueError("receipt components must use the declared evidence version")
        if self.observed_at.tzinfo is None or self.valid_until.tzinfo is None or self.valid_until <= self.observed_at:
            raise ValueError("deployment receipt requires bounded, aware observation validity")
        if self.docs_only:
            if self.components or self.targets or self.workflow_operation_keys or self.actual_revision != self.source_revision:
                raise ValueError("documentation receipt cannot fabricate runtime evidence")
        elif not self.components or not self.targets or not self.workflow_operation_keys:
            raise ValueError("deployment receipt requires runtime component evidence")
        if any(item.actual_revision != self.actual_revision for item in self.components):
            raise ValueError("component revisions must identify the same accepted release")
        return self
