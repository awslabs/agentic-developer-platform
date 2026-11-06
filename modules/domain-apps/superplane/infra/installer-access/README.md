# Superplane installer access

This app-owned Terraform root prepares a dedicated installer role and its EKS
access on an existing ADP management cluster. It does not own the selected
bootstrap operator, management cluster, existing access entries, or another
application's grants. No shared principal is imported. The role and access entry
are protected against accidental destruction; retiring them requires an explicit
review after installation/recovery sessions have stopped.

The selected machine/operator applies reviewed infrastructure plans. The private
`installer_policy_json` supplies the separately reviewed AWS permissions for the
rollout identity. It should permit only the selected installation's reads,
Secrets, state/locks and conditional route writes. It need not grant IAM policy
mutation: prepare the app's control-plane/domain-runtime infrastructure through
their maintained Terraform owners first, and require the domain installer's cloud
plan to be non-destructive and unchanged. Required additional Secret references
must be selected explicitly for the native lifecycle path. Infrastructure roles,
runtime principals, workspace provider credentials and human bootstrap authority
remain distinct.

The entrypoint authenticates the selected operator and immutable RoleId before
Terraform initialization. Run from the reviewed checkout:

```bash
python3 modules/domain-apps/superplane/scripts/plan-installer-access.py \
  --configuration /private/installer-access.json \
  --output /private/installer-access-plan
```

The private JSON contains the required variables declared by this root, with
optional `native_runtime_state_access` (default `false`). Its
`namespace_bootstrap` value defaults operationally to `false` and must be explicit
in the JSON. The planner saves the source pins, exact plan digest and five-resource
inventory. It applies nothing. Planning disables remote locking to remain
read-only; approved apply must use normal state locking. The separate app state
is `<environment>/modules/superplane-installer-access/<installation>/terraform.tfstate`.
The initial planner refuses an existing target role rather than importing it by
name. The `--update` path below verifies retained ownership before an update.

## Native runtime state access

For a Superplane installation that will prepare the native lifecycle runtime,
set `"native_runtime_state_access": true` in the installer access configuration.
This app-owned module derives the permissions from the selected account, region
and environment, using the same backend convention as the runtime preparer:

- Get/Put on the exact S3 object
  `adp-terraform-state-<account>/<environment>/modules/superplane-domain-runtime/terraform.tfstate`.
- ListBucket only for that runtime prefix.
- Get/Put/Delete on only the state lock and `-md5` checksum keys in
  `adp-terraform-locks`.

It adds these statements to the dedicated installer policy. It does not change
role trust, Kubernetes access, shared ADP roles or workspace runtime permissions.
The access is a declared installation dependency, retained for subsequent plans
and updates; it is not a temporary manual grant. The module grants no S3 state
deletion. Runtime infrastructure apply still requires its separate saved-plan
approval.

Omitting the option preserves the supplied policy unchanged. Core ADP setup does
not invoke this app-owned root, so installations without Superplane acquire no
new resources or permissions. Management-only Superplane can also omit it.

For an existing installation, run the same planner against a new output directory:

```bash
python3 modules/domain-apps/superplane/scripts/plan-installer-access.py \
  --configuration /private/installer-access.json \
  --output /private/installer-access-update --update
```

The update must find all five resources in the existing app-owned backend and
match the retained installer role's ARN and immutable RoleId to AWS. It refuses
missing state, role adoption, creation, replacement, deletion, trust changes and
Kubernetes access changes. Only an in-place installer policy update or a no-op
is accepted. Repeating it after apply produces a reviewable no-op when neither
inputs nor infrastructure have changed. Both modes save the generated Terraform
inputs, source hashes and exact plan digest; neither applies anything.

Review the saved policy diff, then apply that exact saved plan through the
authorized state owner with normal Terraform locking. Keep the configuration and
versioned evidence for future runs. Updating the option requires a fresh plan;
an older approved plan does not approve changed bytes. If migrating a private
configuration containing manually assembled `NativeRuntimeState`,
`NativeRuntimeStateList` or `NativeRuntimeStateLocks` statements, remove those
three statements and select the option instead. Duplicate statements are refused,
so the module remains their single source. Existing unrelated policy statements
are preserved and still require review.

## Kubernetes Namespace bootstrap

Steady state uses namespace-scoped `AmazonEKSClusterAdminPolicy` for exactly the
two installation namespaces and `sp-preflight-*`, plus cluster `AmazonEKSViewPolicy`.
The namespace admin policy includes probe ResourceQuota creation and optional
KEDA custom resources. `AmazonEKSAdminPolicy` alone does not cover those writes.
Neither namespace policy grants cluster-scoped Namespace mutations.

The app-owned `namespace-rbac` subroot supplies those missing Namespace rights.
It also grants `get` on the exact Auto Mode `NodeClass/default` object so the
installer can verify its NetworkPolicy configuration. EKS ViewPolicy does not
cover this custom resource. Other NodeClasses and all NodeClass mutations remain
outside the grant; configured enforcement still requires the live packet probe.
Use **actual run IDs from fresh offline installer receipts** for
`preflight_namespaces`. It grants deletion only for those exact temporary names;
it does not grant deletion of the two persistent installation namespaces. It
grants update/patch only for the exact selected names. RBAC cannot constrain
Namespace CREATE by `resourceNames`, so an admission policy restricts that
app-specific group to the exact names and installation owner label. The denial
does not apply to unrelated operators. The creation grant is bound only after
the denial policy and binding exist. Keep the policy while its grant remains.

An existing authorized cluster owner can apply this subroot. If none is selected,
the app has an explicit temporary bootstrap stage:

1. Review and apply this root with `namespace_bootstrap=true`. Only the dedicated
   app role receives temporary cluster administration; shared operator access
   remains unchanged.
2. Use a private kubeconfig whose `aws eks get-token --role-arn` selects the
   created role, and apply the reviewed `namespace-rbac` plan from its own app
   state. Verify the actual group and permitted/denied Namespace operations.
3. Review and apply this root with `namespace_bootstrap=false`, then verify
   namespace-scoped access and denied unrelated cluster writes **before** running
   the installer. Retain both plans and actual apply receipts. Interrupted
   bootstrap is incomplete and must be reconciled/downscoped.

The initial steady-state plan is not a claim that Namespace access is ready.
Do not keep cluster administration enabled to work around a failed bounded grant.
Creating another installer receipt requires reviewing its temporary name and
updating the app-owned RBAC/admission plan through its owner.

## Optional native lifecycle storage

The same app-owned `namespace-rbac` root can provision a retained StorageClass
and its exact installer read. Omit `lifecycle_storage` for existing behavior:
no class or storage permission is added. Core ADP does not call this root.

Select the management cluster's verified EBS driver in private Terraform inputs:

```hcl
lifecycle_storage = { provisioner = "ebs.csi.eks.amazonaws.com" }
```

Use `ebs.csi.aws.com` for a cluster with the standard EBS CSI driver. The root
creates `superplane-lifecycle-<installation_id>` with encrypted gp3 volumes,
`Retain`, `WaitForFirstConsumer`, no default-class annotation and destruction
protection. Auto Mode classes select Auto Mode nodes. Existing resources must
remain in the original namespace-RBAC backend; do not copy the root into another
state or adopt an existing foreign class.

Apply a reviewed plan through the namespace bootstrap owner described above,
then remove temporary bootstrap access before running the installer. The steady
grant is only `get` on this exact StorageClass. No cluster-wide class/PV listing
or volume mutation is granted.

Native installation uses two explicit stages because the CSI volume name exists
only after the owned claim binds:

1. Select the class output in `lifecycle_foundations.state.storage_class`. Run
   normal installer planning/preflight and the reviewed execute command with
   `--prepare-lifecycle-state`. The installer performs its normal identity,
   image, saved-plan and shared-quiescence checks, prepares owned policy/state,
   and runs the credential-free write probe. It stops before migration, rollout
   and activation with `status: awaiting-volume-read`.
2. Read `lifecycle_foundations.volume_read_request` from that private receipt.
   Add its actual `namespace`, `name` and `uid` as `lifecycle_storage.claim`,
   keeping the selected provisioner. Review/apply the namespace-RBAC update.
   Terraform re-reads the PVC and requires its exact UID, installation owner,
   retained class and Bound status. It grants only `get` on the actual bound PV.
   Remove temporary bootstrap access again if that bootstrap path was used.
3. Resume the same installer receipt without `--prepare-lifecycle-state`. Run
   preflight and review its current plan digest before executing. Normal
   preparation must now verify actual PV ownership, retention and identities
   before rollout/admission; the binding-stage receipt is not readiness proof.

Preserve both saved plans, receipts and state identities in the private artifact
store. Retry the same receipt and original environment/release after interruption;
never invent a PVC UID or grant reads on a guessed CSI volume name.

Use the actual Terraform output plus fresh STS and IAM `GetRole` checks for the
installer's `deployment_identity`. Its connection label is an honest local
operator label, not a claim that a broker credential exists. Use a legitimate
assumed-role session and refresh within its actual expiration; AWS role chaining
limits sessions to one hour even when the role permits a longer maximum. Never
copy an old receipt or RoleId to imply a successful apply.

Runtime infrastructure preparation retains its separate enforced GitHub
saved-plan approval contract in
[`../../installation/RUNTIME-PLAN-APPROVAL.md`](../../installation/RUNTIME-PLAN-APPROVAL.md).
User authorization to operate the management account does not fabricate that
independent approval or workspace-account provider authority.
