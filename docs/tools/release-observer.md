# Protected tools release observation

`SCN-8068-04` uses `tests/e2e/tools/release_observer.py` from the reviewed checkout.
The workflow needs no installed private Python module. Put the applied component receipts and
expected digests in the protected `ADP_TOOLS_RELEASE_MANIFEST_JSON` secret, and put resource
selectors in the separate `ADP_TOOLS_RELEASE_SELECTORS_JSON` secret in `adp-tools-deploy-dev`.
The observer accepts only selectors; expected digests and stored readbacks are rejected.

This fictional selector document shows the supported shape. Replace its values with the existing
resources owned by the selected deployment. Account, region and cluster must equal the protected
workflow bindings. Do not commit the completed document.

```json
{
  "version": 1,
  "account": "123456789012",
  "region": "us-east-1",
  "cluster": "example-cluster",
  "components": {
    "gateway": {
      "namespace": "adp-gateway",
      "deployment": "bedrockgateway",
      "container": "bedrockgateway"
    },
    "facade and reconciler": {
      "namespace": "adp-gateway",
      "deployment": "tools-facade",
      "container": "tools-facade",
      "cronjob": "tools-facade-reconciler",
      "reconciler_container": "reconciler"
    },
    "shared Lambda and Browser": {
      "function_arn": "arn:aws:lambda:us-east-1:123456789012:function:example-shared",
      "namespace": "example-agents",
      "deployment": "example-shared-browser",
      "container": "browser"
    },
    "platform targets": {
      "gateway_id": "example-gateway",
      "target_ids": ["example-search", "example-interpreter", "example-browser"]
    }
  }
}
```

Gateway and façade/reconciler are required. Include the shared provider and platform targets when
selected by the release manifest. The observed component set must equal the manifest's set exactly.
Other release components currently require an additional maintained reader before they can pass
this scenario; an unsupported component never falls back to its expected digest.

Each `before` and `after` pass makes fresh metadata reads. It verifies the account, Deployment
readiness, ReplicaSet ownership and every selected pod's actual image ID. Images must use immutable
ECR digests in the selected account and region. The reconciler requires an unsuspended CronJob,
an owned successful Job and pod completed within five minutes, and matching images in any active
Jobs. A newer failed Job rejects the observation. Lambda must be active with a successful update;
the observed digest comes from `GetFunction.Code.ResolvedImageUri`.

For AgentCore, the observer reads the selected ready gateway, lists the complete target set and
reads every ready target. Unexpected, missing, duplicate or incompletely listed targets fail.
The `targets` digest is `sha256:` followed by the existing platform release's canonical
`target_sync_revision`: sorted target IDs and normalized synchronization timestamps (or the last
update timestamp for Lambda targets). This shares the release implementation in
`modules/tools/platform/scripts/smoke.py` and never hashes an expected value.

The existing workflow preflight independently compares the deployed gateway and façade images
against their immutable source tags in ECR. The maintained tools IAM policy permits only
`ecr:DescribeImages` on the gateway repository; its explicit ceiling still denies gateway image
pulls and writes, and image metadata reads from other repositories. Existing exact Lambda and
AgentCore read grants suffice for the observer. The existing inline `tools-platform-denials` keeps `DenyOtherAPIs`; the resource denials
move to a customer-managed policy because IAM's 10,240-byte inline quota applies to the whole role.
Terraform first attaches `aws_iam_policy.tools_deployment_resource_ceiling` through
`aws_iam_role_policy_attachment.tools_deployment_resource_ceiling`, then replaces the existing
grant document with the smaller scoped policy, and only then installs the standalone inline ceiling.
The replacement grant document retains the same action denial atomically, so existing attached
policies cannot bypass it during the transition. The duplicate standalone denial preserves its
identity for installations already owning it. This ordering preserves resource/action bounds and
avoids adding an inline policy while a large legacy combined document still occupies the quota. Tests cover
three selected targets plus the optional inputs secret, the 6,144-byte managed-policy limit, the
aggregate inline limit, and equivalent exact SSM reads/writes. The owner must review the actual
existing inline and attached policies and replace any superseded broad grant through its existing
state owner. IAM trust and effective write/state/resource ceilings remain unchanged.

Both maintained tools release roles scope `codebuild:BatchGetBuilds` to their exact build **project**
ARN. The shared role's `codebuild:StopBuild` uses that same project ARN. AWS authorizes these
operations against projects even though the requests identify build IDs; a grant on a build ARN
does not authorize polling or stopping. Tests reject grants for another project or a build ARN.

Kubernetes authorization remains namespaced. The existing tools release Role gains only `get` on
`bedrockgateway`, `get` on generated ReplicaSets, and `list` on pods and Jobs. Its existing named
façade and reconciler grants supply those workload reads. A separate Browser observer Role binds
the tools group to `get` on the configured Browser Deployment and its ReplicaSets and `list` on
pods in the configured Browser namespace. It does not bind the tools group to the shared provider's
write Role. Kubernetes cannot restrict pod or Job lists by label; the observer verifies controller
UIDs before accepting their metadata. These reads do not grant secrets, logs, exec, tokens, RBAC
changes or workload mutation. The owner deploys these Terraform changes through the existing
reviewed-plan procedure.

The RBAC resources and namespace prerequisite depend on `module.eks.access_entry_ready`.
That output retains the operator access entry, policy association and propagation wait needed
on a fresh install. Depending on the entire EKS module also pulls unrelated addons, runtime
roles and policies into a targeted RBAC plan. Use the canonical platform backend and preserve
the current owner inputs, including explicit endpoint CIDRs and additional capacity subnets.
Target all three Roles **and all three RoleBindings**. Review every remaining dependency change;
operator access-entry metadata may still differ, and a scoped plan does not resolve other
platform drift. Never delete state, disable refresh or ignore drift to force an empty dependency plan.

After `terraform init -backend=false` in `platform/infra`, run
`python3 tests/test_tools_release_plan_scope.py` to inspect a real targeted Terraform
plan with mocked providers. The credential-free platform PR job runs this same assertion.
The test verifies fresh-install access prerequisites and excludes unrelated EKS resources.
It does not read or change a live backend.

For legacy tools identities, inspect the EKS access entry and associated access policies as well
as Kubernetes RBAC. Install the intended group and namespaced bindings, verify group-only
authorization, then retire any superseded edit-policy association through its existing automation
state owner. Protected acceptance must verify the actual identity after retirement; successful
reads while a broader legacy policy remains do not prove the intended privilege boundary.

Before `SCN-8068-04` observes runtime, the protected workflow must pass
`tests.e2e.tools.release_authorization`. It uses the existing GitHub OIDC identity and
workflow-owned kubeconfig. Both STS and Kubernetes must identify the selected account's
canonical tools deployment role in the current `deploy-<run-id>-<attempt>` session.
Kubernetes groups must contain exactly the selected tools release group and
`system:authenticated`; another trusted deployment role or additional group fails the proof.
The approved selector file supplies workload coordinates, never expected permission decisions
or identity expectations. The facade Deployment, Service and ServiceAccount share a resource
name, as do the reconciler CronJob and ServiceAccount.

The full four-component selection runs 43 read-only authorization questions: 23 expected allows
and 20 expected denials. These cover named facade updates, gateway and Browser observation,
and refusal of foreign workloads, gateway/Browser writes, secret/log/exec/token access,
RBAC edits and cluster-wide listing. The helper only asks `kubectl auth can-i`; it does not
attempt those mutations. A retained namespace edit policy causes the negative checks to fail,
even when every required read succeeds. Incomplete reviews and provider diagnostics also fail
closed. Cluster-scoped questions explicitly clear kubectl's default namespace.

`authorization.json` records the definition revision, workflow run, a hash binding the protected
identity and selectors, identity-match booleans, and named check outcomes. It omits raw provider
responses, identity ARNs, selector values and error text, is written with mode `0600`, and is
retained with the private evaluation evidence. Failure stops the workflow before the runtime
observer. This step uses no personal refresh session, role chaining or copied credentials.

The workflow writes manifest, selectors and kubeconfig privately under `RUNNER_TEMP`, persists the
owned kubeconfig path through `GITHUB_ENV`, and removes all three files in its `always()` cleanup.
The scenario skips refresh-session import. Successful observations are written separately to
`runtime-before.json` and `runtime-after.json` beside the private evaluation evidence. Provider
errors fail closed without copying response bodies or stderr into the receipt. Clean-checkout and
fake-provider tests exercise this path; only the protected run against the deployed target can
satisfy live acceptance.
