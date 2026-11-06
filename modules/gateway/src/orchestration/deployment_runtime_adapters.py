"""Independently observe the adapter using reviewed, hash-bound expectations.

The native caller owns workflow provenance, expected artifact retrieval, target
authorization and final receipt acceptance. This module never turns a workflow's
success or an apply-stage observation into runtime evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .review_cycle import CycleBlockedError

MAX_EXPECTATION_BYTES = 128 * 1024
ASSETS = frozenset({"adapter-manifest.json", "adapter-contract.json"})
RESOURCE_NAMES = {
    "ServiceAccount": "tool-adapters",
    "ConfigMap": "tool-adapters-contract",
    "Deployment": "tool-adapters",
    "Service": "tool-adapters",
    "PodDisruptionBudget": "tool-adapters",
}


def require(condition, reason):
    if not condition:
        raise CycleBlockedError("deployment_adapter_" + reason)


def canonical_bytes(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise CycleBlockedError("deployment_adapter_expectations_invalid") from None
    require(len(encoded) <= MAX_EXPECTATION_BYTES, "expectations_limit")
    return encoded


def _observer_path():
    gateway = Path(__file__).resolve().parents[2]
    repository = gateway.parent.parent
    if gateway.name == "gateway" and (repository / ".git").exists():
        return repository / "modules/tools/adapters/runtime_evidence.py"
    # stage-contracts.sh must copy this one stdlib module from its owning source.
    # No service package, renderer dependencies or provider implementation belongs
    # in the gateway image. A missing staged source refuses observation.
    return gateway / "contracts/tool-adapters/runtime_evidence.py"


@lru_cache(maxsize=1)
def _observer():
    path = _observer_path()
    try:
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 64 * 1024, "observer_unavailable")
        spec = importlib.util.spec_from_file_location("adp_tool_adapter_runtime_observer", path)
        require(spec is not None and spec.loader is not None, "observer_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        require(
            callable(getattr(module, "observe", None))
            and callable(getattr(module, "validate_configuration", None))
            and isinstance(getattr(module, "ObservationError", None), type),
            "observer_unavailable",
        )
        return module
    except (OSError, ImportError, AttributeError, SyntaxError):
        raise CycleBlockedError("deployment_adapter_observer_unavailable") from None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "configuration_duplicate_field")
        value[key] = item
    return value


def _expectations(kube, artifact, manifest, contract):
    require(isinstance(manifest, dict) and isinstance(contract, dict), "expectations_invalid")
    assets = artifact.assets
    require(isinstance(assets, dict) and set(assets) == ASSETS, "expectation_hashes_missing")
    for name, content in (("adapter-manifest.json", manifest), ("adapter-contract.json", contract)):
        require(assets[name] == hashlib.sha256(canonical_bytes(content)).hexdigest(), "expectation_hash_mismatch")
    require(
        set(contract) == {"contract_version", "schema_sha256"}
        and contract.get("contract_version") == "1.0"
        and isinstance(contract.get("schema_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", contract["schema_sha256"]),
        "contract_invalid",
    )
    require(
        artifact.component == "tool-adapters"
        and artifact.environment in {"dev", "prod"}
        and re.fullmatch(r"[0-9a-f]{40}", artifact.source_revision)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.image_digest or "")
        and re.fullmatch(r"[0-9]{12}", artifact.account_id)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", artifact.region),
        "release_invalid",
    )
    namespace = kube.namespace
    description = kube.description
    cluster = description.get("name", "")
    require(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
        and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,99}", cluster)
        and description.get("arn") == f"arn:aws:eks:{artifact.region}:{artifact.account_id}:cluster/{cluster}"
        and kube.scoped.region_name == artifact.region
        and artifact.resource_id in {f"{cluster}/{namespace}", f"{cluster}/{namespace}/tool-adapters"},
        "target_mismatch",
    )
    items = manifest.get("items")
    require(
        manifest.get("apiVersion") == "v1"
        and manifest.get("kind") == "List"
        and isinstance(items, list)
        and len(items) == 5
        and all(isinstance(item, dict) for item in items)
        and {item.get("kind") for item in items} == set(RESOURCE_NAMES),
        "manifest_resources_invalid",
    )
    resources = {item["kind"]: item for item in items}
    for kind, item in resources.items():
        metadata = item.get("metadata", {})
        require(metadata.get("name") == RESOURCE_NAMES[kind] and metadata.get("namespace") == namespace, "manifest_target_mismatch")
    raw = resources["ConfigMap"].get("data", {}).get("config.json")
    require(isinstance(raw, str) and len(raw.encode()) <= 65536, "configuration_invalid")
    configuration = json.loads(raw, object_pairs_hook=_unique_object)
    # The signed manifest binds the complete configuration. Its component-owned
    # verifier defines valid modes; the delivery engine must not duplicate them.
    module = _observer()
    try:
        module.validate_configuration(configuration)
    except module.ObservationError:
        raise CycleBlockedError("deployment_adapter_configuration_invalid") from None
    release = configuration["release"]
    require(
        isinstance(release, dict)
        and set(release)
        == {
            "contract_version",
            "source_revision",
            "image_digest",
            "group",
            "group_generation",
            "backend_generation",
        },
        "configuration_release_invalid",
    )
    require(
        release["contract_version"] == contract["contract_version"]
        and release["source_revision"] == artifact.source_revision
        and release["image_digest"] == artifact.image_digest
        and isinstance(release["group"], str)
        and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", release["group"])
        and all(type(release[key]) is int and release[key] > 0 for key in ("group_generation", "backend_generation")),
        "configuration_release_mismatch",
    )
    role = resources["ServiceAccount"].get("metadata", {}).get("annotations", {}).get("eks.amazonaws.com/role-arn", "")
    require(re.fullmatch(rf"arn:aws:iam::{artifact.account_id}:role/[A-Za-z0-9_+=,.@/-]+", role), "identity_target_mismatch")
    template = resources["Deployment"].get("spec", {}).get("template", {})
    containers = template.get("spec", {}).get("containers", [])
    image = f"{artifact.account_id}.dkr.ecr.{artifact.region}.amazonaws.com/adp-tools-{artifact.environment}-adapters@{artifact.image_digest}"
    require(len(containers) == 1 and containers[0].get("name") == "tool-adapters" and containers[0].get("image") == image, "manifest_image_mismatch")
    require(
        template.get("metadata", {}).get("annotations", {}).get("checksum/adp-adapter-contract") == hashlib.sha256(raw.encode()).hexdigest(),
        "configuration_checksum_mismatch",
    )
    return release


def _get(kube, path):
    # KubernetesRuntime.get passes params={} to httpx when kwargs are omitted,
    # which drops an existing query string. Preserve the observer's bounded
    # label selector/limit as explicit params rather than listing every workload.
    parsed = urlsplit(path)
    require(not parsed.scheme and not parsed.netloc and not parsed.fragment, "read_path_invalid")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    params = dict(pairs)
    require(len(params) == len(pairs) and set(params) <= {"labelSelector", "limit"}, "read_query_invalid")
    return kube.get(parsed.path, **params)


def verify_tool_adapters(kube, artifact, artifact_hash, evidence_ref, *, expected_manifest, expected_contract):
    """Return observation fields; the caller constructs/accepts its own receipt.

    Expected JSON must come from reviewed release inputs. The live ConfigMap or
    Deployment cannot choose these expectations; both files are bound to exact
    hashes in the authenticated release artifact before the first cluster read.
    """
    try:
        require(isinstance(artifact_hash, str) and re.fullmatch(r"[0-9a-f]{64}", artifact_hash), "artifact_hash_invalid")
        require(
            isinstance(evidence_ref, str) and 0 < len(evidence_ref) <= 512 and not any(character.isspace() for character in evidence_ref),
            "evidence_reference_invalid",
        )
        release = _expectations(kube, artifact, expected_manifest, expected_contract)
        module = _observer()
        try:
            observed = module.observe(lambda path: _get(kube, path), expected_manifest, expected_contract)
        except module.ObservationError:
            raise CycleBlockedError("deployment_adapter_runtime_mismatch") from None
        require(
            observed.get("release") == release
            and observed.get("namespace") == kube.namespace
            and observed.get("ready_replicas") == observed.get("routed_replicas") == 2
            and observed.get("execution_enabled") is False
            and observed.get("probe_path") == "api_server_proxy",
            "observation_invalid",
        )
        pods = observed.get("pods", [])
        require(
            len(pods) == 2
            and len({pod.get("uid") for pod in pods}) == 2
            and all(pod.get("ready") is True and pod.get("image_digest") == artifact.image_digest for pod in pods),
            "observation_invalid",
        )
        observed_at = datetime.fromisoformat(observed["observed_at"])
        require(observed_at.tzinfo is not None, "observation_time_invalid")
        return {
            "component": "tool-adapters",
            "actual_revision": release["source_revision"],
            "artifact_hash": artifact_hash,
            "image_digest": release["image_digest"],
            "healthy": True,
            "evidence_ref": evidence_ref,
            "observed_at": observed_at,
            "pod_uids": [pod["uid"] for pod in pods],
            "adapter_probe_path": "api_server_proxy",
        }
    except (TypeError, ValueError, KeyError, AttributeError):
        raise CycleBlockedError("deployment_adapter_evidence_invalid") from None
