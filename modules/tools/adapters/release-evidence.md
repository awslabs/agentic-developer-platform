# Adapter release documents

After guarded publication and runtime observation, the existing
`tools-platform-deploy.yml` adapter stage can run:

```bash
python3 modules/tools/adapters/release_evidence.py --output "$RUNNER_TEMP/adp-release-tool-adapters"
```

Use the adapter's locked Python environment, including the shared contract
package. The output directory must not already exist. The helper writes private
`release.json` and `deployment-context.json`; it does not upload them or authorize
deployment. Load the existing gateway evidence publisher from the protected
workflow revision and publish the files as `release tool-adapters` and
`context tools-platform-deploy.yml`, respectively.

The helper consumes the same target and configuration as `deploy.py`:

- `ADP_TOOLS_ACCOUNT_ID`, `ADP_TOOLS_AWS_REGION`, `ADP_TOOLS_ENVIRONMENT`,
  `ADP_TOOLS_EKS_CLUSTER_NAME`, `ADP_TOOLS_ADAPTER_NAMESPACE`,
  `ADP_TOOLS_ADAPTER_ROLE_ARN`.
- `ADP_SOURCE_REVISION`, `ADP_DEFINITION_REVISION`, `ADP_CORRELATION` and the
  standard GitHub repository, run, attempt, ref and workflow identity variables.
- `ADP_TOOLS_ADAPTER_CONFIG_FILE`, `ADP_TOOLS_ADAPTER_PUBLICATION_FILE`,
  `ADP_TOOLS_ADAPTER_EVIDENCE_FILE` from this deployment.
- `ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON`: the complete effective
  `workflow_dispatch` input dictionary, including defaults and the adapter stage
  selector. Values must be strings. The helper validates environment/release mode
  and dispatch identity, then removes `adp_*` fields as the existing core context
  contract requires. The engine compares the remaining inputs against the actual
  dispatch and reviewed workflow definition; this helper invents no defaults.

The producer renders the expected manifest from source and deployment-owned
configuration, includes the shared contract, hashes both, and checks them against
the observation's release/configuration identity. It never derives its expected
manifest from cluster resources. The two expected documents travel inside the
existing signed S3 release payload so the verifier can independently reread the
runtime. The publisher supplies account confirmation and the GitHub signature.
Native lease ownership, workflow guards and subsequent engine acceptance remain
mandatory; these documents alone establish none of them.
