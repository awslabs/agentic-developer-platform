# Installation decommissioning

Use the authorized installation operator and selected AWS profile. Workspace
retirement remains a separate lifecycle operation. These commands support a full
installation reset and retain private state backups and saved plans.

`platform/scripts/undeploy.sh` dispatches these app-owned operations. Add `--help`
after selecting an operation to see its required arguments:

- `--superplane-quiesce`: preview controller pauses for explicit installation
  namespaces. Add `--apply` with a new output directory to execute UID and
  resource-version guarded pauses. It verifies EKS transport and namespace
  ownership, waits for workload pods to stop, and retains storage/completed jobs.
- `--superplane-workspace-prepare`: the maintained workspace preparation command.
  Supply original or state-verified inputs, the exact backend, Terraform 1.9.8,
  `--destroy` and `--review-only`. Review the artifact-bound proposed authorization.
- `--superplane-workspace-apply`: the maintained workspace apply command. Supply
  the exact binary, JSON, authorization, initialized module and immutable target.
  It never replans or accepts a replacement as a delete.
- `--superplane-infrastructure`: prepare a saved destroy plan for one exact
  dedicated-reader, domain-runtime, provider, foundation or installer-namespace
  state, using its original private inputs and a new output directory. Review
  `review.json`, `destroy.json` and the saved plan. Repeat with `--apply`, the
  same inputs/output directory, and the verified management kubeconfig,
  installation ID and complete namespace set. Apply checks artifact, state,
  original identities, dependencies and stopped workloads again. The provider
  root requires Terraform 1.9.8 through `--terraform-binary`.

Remove readers before their workspace clusters, workspace infrastructure before
its foundations, and foundations before provider identities. Remove the domain
runtime while its management cluster and KEDA role remain. Retained foundations
require `--include-retained`; their app-owned decommission root handles an already
removed provisioning role by verifying the recorded inputs, resource tags and
identities. It never recreates the provisioning role. KMS waiting periods remain.

Installer-namespace decommissioning accepts only its retained StorageClass.
Temporary grants must first use their recorded cleanup path. Class deletion does
not reclaim retained PVCs, PVs or cloud volumes. Use `--superplane-storage` with
the exact claim namespace/name and installation to prepare deletion of its
retained lifecycle PVC, PV and EBS volume. Review its private receipt, then repeat
with `--apply`. It checks the original claim UID, CSI volume binding, cluster tags
and detached volume, and uses Kubernetes UID/resource-version delete preconditions.
Verify absence before deleting the management cluster. Adopted
workspace infrastructure and shared platform state remain outside this scope.

Core webhook teardown invokes the app's provider-authority decommissioning hook.
It validates the app-owned addresses and original table ownership and TableId,
prepares a delete-only plan without provisioning dependencies, and releases AWS
table deletion protection only immediately before applying a reviewed deletion.
Provider authority remains in its original webhook state throughout cleanup.

After app states are empty, repeat normal core `undeploy.sh --dry-run`, then
execute core teardown in its documented order. Keep configuration, plans and
recovery evidence private. Never discard state to pass the auxiliary-state check.
