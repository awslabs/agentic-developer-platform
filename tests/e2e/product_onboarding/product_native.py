"""Use the maintained signed release and runtime reader for Product acceptance."""

import hashlib
import json
import os
from functools import partial

from deployment_evidence.store import EvidenceStore
from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from src.orchestration.deployment_runtime_reader import (
    KubernetesRuntime,
    PilotRuntimeReader,
)

from tests.e2e.cli_uplift import deployment_provenance, ports


def observe(cfg, inventory, aws):
    try:
        target = deployment_provenance.binding(cfg)
        expected = {
            "account": str(cfg["platform_account"]),
            "environment": "dev",
            "region": cfg["region"],
            "repository": os.environ["GITHUB_REPOSITORY"],
            "repository_id": int(os.environ["GITHUB_REPOSITORY_ID"]),
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "kind": "release",
            "name": "gateway-backend",
            "workflow_path": ".github/workflows/gateway-deploy.yml",
            "workflow_revision": os.environ["GITHUB_WORKFLOW_SHA"],
        }
        stored = EvidenceStore(s3=aws.client("s3")).read(**expected)
        if stored is None:
            raise ValueError("Selected deployment release is missing")
        payload, digest, reference = stored
        release = ReleaseArtifact.model_validate_json(payload)
        required = {
            "repository_id": expected["repository_id"],
            "run_id": expected["run_id"],
            "run_attempt": expected["attempt"],
            "workflow_path": expected["workflow_path"],
            "workflow_revision": expected["workflow_revision"],
            "source_revision": cfg["expected_revision"],
            "account_id": expected["account"],
            "region": expected["region"],
            "resource_id": target["cluster"] + "/" + target["namespace"],
            "component": "gateway-backend",
            "image_digest": inventory["digest"],
            "product_profile_schema_version": "1.0",
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
        }
        if any(getattr(release, field) != value for field, value in required.items()):
            raise ValueError("Selected deployment release differs")
        description = aws.call("eks", "describe_cluster", name=target["cluster"])[
            "cluster"
        ]
        reader = PilotRuntimeReader(
            kubernetes=partial(
                KubernetesRuntime, proof_invoke_url=cfg["internal_invoke_url"]
            )
        )
        receipt = reader.inspect(
            aws.session(),
            description,
            target["namespace"],
            artifact=release,
            artifact_hash=digest,
            evidence_ref=reference,
            environment="dev",
        )
        if receipt.pod_uids != [pod["uid"] for pod in inventory["pods"]]:
            raise ValueError("Serving Product pods changed")
        observed = receipt.model_dump(mode="json")
        observation_hash = hashlib.sha256(
            json.dumps(observed, sort_keys=True).encode()
        ).hexdigest()
        return {
            "source_revision": receipt.actual_revision,
            "database_heads": [receipt.migration_head] * len(receipt.pod_uids),
            "image_heads": [receipt.migration_head] * len(receipt.pod_uids),
            "tick_verified": receipt.tick_digest == inventory["digest"],
            "tick_digest": receipt.tick_digest,
            "pod_uids": receipt.pod_uids,
            "cli_manifest_sha256": receipt.cli_manifest_sha256,
            "product_profile_enabled": receipt.product_profile_enabled,
            "product_onboarding_enabled": receipt.product_onboarding_enabled,
            "observation_ref": "runtime:sha256:" + observation_hash,
            "release_ref": reference,
            "release_hash": digest,
            "target": {
                "account_id": expected["account"],
                "region": expected["region"],
                "resource_id": required["resource_id"],
                "repository_id": expected["repository_id"],
            },
            "runtime_receipt": observed,
            "release_artifact": release.model_dump(mode="json"),
        }
    except Exception:
        raise ports.PortError(
            "Native migration-head and tick observation is unavailable or mismatched; Product writes refuse"
        ) from None
