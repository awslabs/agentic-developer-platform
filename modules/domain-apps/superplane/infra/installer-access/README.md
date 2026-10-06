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

The private JSON contains exactly the variables declared by this root. Its
`namespace_bootstrap` value defaults operationally to `false` and must be explicit
in the JSON. The planner saves the source pins, exact plan digest and five-resource
inventory. It applies nothing. Planning disables remote locking to remain
read-only; approved apply must use normal state locking. The separate app state
is `<environment>/modules/superplane-installer-access/<installation>/terraform.tfstate`.
The initial planner refuses an existing target role rather than importing it by
name. Reconcile a retained state before any later update.

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
