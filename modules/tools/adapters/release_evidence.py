"""Produce adapter release documents for the existing authenticated S3 publisher.

Expected documents are rendered from reviewed source and deployment-owned
configuration, never copied from the live cluster. The engine independently
observes runtime after authenticating these documents; this producer does not
grant acceptance, publish evidence, or mutate a deployment.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from adp_tools.adapter_contracts import contract_metadata

from k8s.render import render
from tool_adapters.config import load_configuration

WORKFLOW = ".github/workflows/tools-platform-deploy.yml"
MAX_DOCUMENT = 65536


def require(condition, message):
    if not condition:
        raise ValueError("adapter_evidence_" + message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "duplicate_field")
        result[name] = value
    return result


def read_document(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "regular_input_required")
    with path.open("rb") as stream:
        raw = stream.read(MAX_DOCUMENT + 1)
    require(0 < len(raw) <= MAX_DOCUMENT, "input_size")
    value = json.loads(raw, object_pairs_hook=unique_object)
    require(isinstance(value, dict), "object_required")
    return value


def documents(env, configuration, publication, observation, effective_inputs):
    """Return (release, workflow context) from the exact protected-stage inputs."""
    source, revision = env["ADP_SOURCE_REVISION"], env["ADP_DEFINITION_REVISION"]
    account, region = env["ADP_TOOLS_ACCOUNT_ID"], env["ADP_TOOLS_AWS_REGION"]
    environment = env["ADP_TOOLS_ENVIRONMENT"]
    cluster, namespace = env["ADP_TOOLS_EKS_CLUSTER_NAME"], env["ADP_TOOLS_ADAPTER_NAMESPACE"]
    correlation = env.get("ADP_CORRELATION", "")
    require(
        re.fullmatch(r"[0-9a-f]{40}", source)
        and re.fullmatch(r"[0-9a-f]{40}", revision)
        and re.fullmatch(r"[0-9]{12}", account)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        and environment in {"dev", "prod"}
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster)
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
        and (
            not correlation
            or re.fullmatch(
                r"[0-9a-f]{64}|orch:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", correlation
            )
        ),
        "target_invalid",
    )
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_WORKFLOW_REF") == f"{env['GITHUB_REPOSITORY']}/{WORKFLOW}@refs/heads/main"
        and env.get("GITHUB_WORKFLOW_SHA") == revision,
        "protected_workflow_required",
    )
    identity = {
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
    }
    require(all(value > 0 for value in identity.values()) and identity["run_attempt"] == 1, "run_invalid")
    require(
        isinstance(effective_inputs, dict)
        and len(effective_inputs) <= 20
        and all(isinstance(k, str) and isinstance(v, str) for k, v in effective_inputs.items()),
        "workflow_inputs_invalid",
    )
    # The workflow supplies all effective inputs, including defaults. The engine
    # compares the non-adp values with the actual dispatched workflow definition.
    for name, expected in {
        "adp_source_revision": source,
        "adp_correlation": correlation,
    }.items():
        require(effective_inputs.get(name, "") in {"", expected}, "dispatch_identity_mismatch")
    identity_stage = effective_inputs.get("mode") == "identity"
    if not identity_stage:
        require(effective_inputs.get("adp_definition_revision", "") in {"", revision}, "dispatch_identity_mismatch")
    inputs = {
        name: value
        for name, value in effective_inputs.items()
        if not name.startswith("adp_") or (identity_stage and name == "adp_contract_digest")
    }
    require(
        inputs.get("environment") == environment
        and inputs.get("mode") in ({"release", "identity"} if identity_stage else {"release"})
        and inputs.get("adapter_action", "foundation") == "foundation"
        and inputs.get("manual_source_revision", "") in {"", source},
        "workflow_inputs_mismatch",
    )
    if identity_stage:
        approved = effective_inputs.get("adp_definition_revision", "")
        require(re.fullmatch(r"[0-9a-f]{40}", approved), "identity_definition_invalid")
        if approved != revision:
            try:
                subprocess.run(["git", "merge-base", "--is-ancestor", approved, revision],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                approved_workflow = subprocess.check_output(
                    ["git", "show", f"{approved}:{WORKFLOW}"], stderr=subprocess.DEVNULL
                )
                actual_workflow = subprocess.check_output(
                    ["git", "show", f"{revision}:{WORKFLOW}"], stderr=subprocess.DEVNULL
                )
                require(approved_workflow == actual_workflow, "identity_definition_changed")
            except subprocess.CalledProcessError:
                require(False, "identity_definition_changed")
        require(
            inputs.get("stage") == "tool-adapters"
            and inputs.get("adp_contract_digest") == env.get("ADP_IDENTITY_CONTRACT_DIGEST")
            and re.fullmatch(r"[0-9a-f]{64}", inputs["adp_contract_digest"]),
            "identity_contract_mismatch",
        )
    require(
        configuration is not None
        and configuration.release.source_revision == source
        and publication.get("source_revision") == source
        and publication.get("image_digest") == configuration.release.image_digest,
        "publication_mismatch",
    )
    manifest = render(
        image=(
            f"{account}.dkr.ecr.{region}.amazonaws.com/adp-tools-{environment}-adapters"
            f"@{configuration.release.image_digest}"
        ),
        namespace=namespace,
        role_arn=env["ADP_TOOLS_ADAPTER_ROLE_ARN"],
        configuration=configuration,
    )
    contract = contract_metadata()
    require(
        observation.get("schema_version") == "1.0"
        and observation.get("observation_kind") == "adapter_foundation"
        and observation.get("account_id") == account
        and observation.get("region") == region
        and observation.get("cluster") == cluster
        and observation.get("namespace") == namespace
        and observation.get("release") == configuration.release.model_dump(mode="json")
        and observation.get("publication") == publication
        and observation.get("contract") == contract
        and observation.get("configuration_sha256")
        == hashlib.sha256(configuration.model_dump_json().encode()).hexdigest()
        and observation.get("execution_enabled") is False
        and observation.get("ready_replicas") == 2
        and observation.get("routed_replicas") == 2
        and observation.get("probe_path") == "api_server_proxy",
        "observation_mismatch",
    )
    observed = datetime.fromisoformat(observation["observed_at"])
    require(observed.tzinfo is not None, "observation_timestamp_invalid")
    assets = {}
    for name, document in (("adapter-manifest.json", manifest), ("adapter-contract.json", contract)):
        payload = encoded(document)
        require(len(payload) <= 24 * 1024, "expected_document_size")
        assets[name] = hashlib.sha256(payload).hexdigest()
    compatibility = {
        "adapter": {
            "release": configuration.release.model_dump(mode="json"),
            "mode": configuration.mode,
            "contract": contract,
            "configuration_sha256": observation["configuration_sha256"],
            "manifest_sha256": assets["adapter-manifest.json"],
        },
    }
    if configuration.observation is not None:
        compatibility["identity"] = {
            "provider": configuration.observation.identity_provider,
            "workload": configuration.observation.identity_workload,
            "config_revision": configuration.observation.config_revision,
            "subject_schema_sha256": configuration.observation.subject_schema_sha256,
            "sites_schema_sha256": configuration.observation.sites_schema_sha256,
        }
    common = {
        "schema_version": 1,
        **identity,
        "workflow_path": WORKFLOW,
        "workflow_revision": revision,
        "source_revision": source,
        "account_id": account,
        "region": region,
        "resource_id": f"{cluster}/{namespace}",
    }
    release = {
        **common,
        "component": "tool-adapters",
        "environment": environment,
        "image_digest": configuration.release.image_digest,
        "assets": assets,
        "compatibility": compatibility,
        "adapter_manifest": manifest,
        "adapter_contract": contract,
        "produced_at": datetime.now(UTC).isoformat(),
    }
    context = {
        **common,
        "resource_kind": "eks-namespace",
        "inputs": inputs,
        "correlation": correlation,
    }
    require(all(len(encoded(value)) + 1 <= MAX_DOCUMENT for value in (release, context)), "output_size")
    return release, context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", required=True, help="New private directory for release.json and deployment-context.json"
    )
    args = parser.parse_args()
    env = os.environ
    inputs_raw = env["ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON"]
    require(len(inputs_raw.encode()) <= MAX_DOCUMENT, "workflow_inputs_size")
    release, context = documents(
        env,
        load_configuration(env["ADP_TOOLS_ADAPTER_CONFIG_FILE"]),
        read_document(env["ADP_TOOLS_ADAPTER_PUBLICATION_FILE"]),
        read_document(env["ADP_TOOLS_ADAPTER_EVIDENCE_FILE"]),
        json.loads(inputs_raw, object_pairs_hook=unique_object),
    )
    output = Path(args.output)
    # Require a new directory so retries cannot overwrite a previous receipt.
    output.mkdir(mode=0o700)
    for name, value in (("release.json", release), ("deployment-context.json", context)):
        descriptor = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(value) + b"\n")
    print("Adapter release documents prepared for authenticated publication and independent runtime verification")


if __name__ == "__main__":
    main()
