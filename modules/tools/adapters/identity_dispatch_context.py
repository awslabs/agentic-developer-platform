"""Sign the attempted Identity dispatch before changing its native target."""

import argparse
import json
import os
import re
from pathlib import Path

from deploy import Deployment, require, write_private

WORKFLOW = ".github/workflows/tools-platform-deploy.yml"


def context(env, inputs):
    require(
        env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REF") == "refs/heads/main",
        "identity_workflow_required",
    )
    require(
        env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_RUN_ATTEMPT") == "1",
        "identity_attempt_invalid",
    )
    require(
        env.get("GITHUB_WORKFLOW_REF") == env.get("GITHUB_REPOSITORY", "") + "/" + WORKFLOW + "@refs/heads/main",
        "identity_workflow_changed",
    )
    require(re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_WORKFLOW_SHA", "")), "identity_definition_invalid")
    required = {
        "mode", "stage", "environment", "adp_contract_digest",
        "adp_source_revision", "adp_definition_revision", "adp_correlation",
    }
    # A workflow_dispatch input the engine did not select is absent from the
    # inputs context rather than present with its declared default, so the
    # optional manual source is omitted on an engine dispatch. Its only accepted
    # value is the empty string and the refusal below still holds either way;
    # requiring the key itself refused every correlated Identity dispatch.
    require(
        isinstance(inputs, dict)
        and required <= set(inputs) <= required | {"manual_source_revision", "adapter_action"}
        and all(isinstance(value, str) for value in inputs.values()),
        "identity_inputs_invalid",
    )
    require(
        inputs.get("mode") == "identity" and inputs.get("stage") == "tool-adapters"
        and inputs.get("environment") == "dev", "identity_target_changed",
    )
    require(inputs.get("manual_source_revision", "") == "", "identity_manual_source_refused")
    require(inputs.get("adapter_action", "foundation") == "foundation", "identity_handoff_refused")
    require(re.fullmatch(r"[0-9a-f]{64}", inputs.get("adp_contract_digest", "")), "identity_contract_invalid")
    require(inputs["adp_contract_digest"] == env.get("ADP_IDENTITY_CONTRACT_DIGEST"), "identity_contract_changed")
    for name, expected in (
        ("adp_source_revision", env["ADP_SOURCE_REVISION"]),
        ("adp_correlation", env["ADP_CORRELATION"]),
    ):
        require(inputs.get(name) == expected, "identity_dispatch_changed")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", "")), "identity_run_invalid")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_REPOSITORY_ID", "")), "identity_repository_invalid")
    require(re.fullmatch(r"[0-9a-f]{40}", inputs["adp_definition_revision"]), "identity_definition_invalid")
    return {
        "schema_version": 1,
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": 1,
        "workflow_path": WORKFLOW,
        "workflow_revision": env["ADP_DEFINITION_REVISION"],
        "source_revision": env["ADP_SOURCE_REVISION"],
        "account_id": env["ADP_TOOLS_ACCOUNT_ID"],
        "region": env["ADP_TOOLS_AWS_REGION"],
        "resource_kind": "eks-namespace",
        "resource_id": env["ADP_TOOLS_EKS_CLUSTER_NAME"] + "/" + env["ADP_TOOLS_ADAPTER_NAMESPACE"],
        "inputs": {
            name: value for name, value in inputs.items()
            if not name.startswith("adp_") or name == "adp_contract_digest"
        },
        "correlation": env["ADP_CORRELATION"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    env = dict(os.environ)
    deployment = Deployment(env)
    identity = deployment.aws("sts", "get-caller-identity")
    require(
        identity.get("Account") == deployment.account
        and identity.get("Arn", "").startswith(
            f"arn:aws:sts::{deployment.account}:assumed-role/{deployment.role.rsplit('/', 1)[1]}/"
        ),
        "identity_role_changed",
    )
    require(deployment.run(["git", "rev-parse", "HEAD"]).strip() == deployment.source, "identity_source_changed")
    deployment.run(["git", "merge-base", "--is-ancestor", deployment.source, "refs/remotes/origin/main"])
    inputs = json.loads(env["ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON"])
    document = context(env, inputs)
    approved = inputs["adp_definition_revision"]
    actual = env["GITHUB_WORKFLOW_SHA"]
    deployment.run(["git", "merge-base", "--is-ancestor", approved, actual])
    require(
        deployment.run(["git", "show", f"{approved}:{WORKFLOW}"])
        == deployment.run(["git", "show", f"{actual}:{WORKFLOW}"]),
        "identity_definition_changed",
    )
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(args.output, document)


if __name__ == "__main__":
    main()
