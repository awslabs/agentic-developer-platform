# Managed workspace infrastructure

VPC, EKS and IAM for **one** Superplane tenant workspace.
Issue [#5532](https://github.com/aws-e/adp/issues/5532) (w6-09), EPIC A #4910, requirements row A3.

This module is instantiated **once per workspace**, with its own Terraform state. It creates a
private network (or references a supplied one), a physically separate EKS cluster, and the
least-privilege identities that cluster needs.

## What merging this authorizes: nothing live

Implementation and offline verification only. No account vending, AWS or Kubernetes apply,
feature activation or workload spend is authorized by this code or by merging it. Live
evidence — an `ACTIVE` cluster — belongs to the Wave 5 gate and the Wave 6 operations
evaluator, per #5540 AC-02.

The module refuses to plan against an unnamed target: `account_id`, `org_id`, `workspace_id`, `workspace_name`,
`aws_region` and `cluster_version` are all required with no defaults, and a precondition fails
the plan if `account_id` disagrees with the caller's real identity.

It also refuses to plan against an *unavailable* target. `aws_region` and `cluster_version` are
checked against a reviewed, dated allowlist (`scripts/region_version_policy.py`) rather than a
regex, because a pattern describes the shape of an identifier while availability is a fact about
the world on a date: `xx-fake-1` matches every regex for a region, `us-east-2` is real and not
reviewed for this platform, and `1.25` matches every version pattern while being retired. All
three used to fail at apply, after the VPC and NAT gateway existed. See
[Region and version support policy](#region-and-version-support-policy).

## There is no committed tfvars file, on purpose

Other ADP modules have `environments/dev/modules/<module>.tfvars`. This one does not, and that
is a design consequence rather than an omission:

- **The state key is per workspace.** `<environment>/modules/superplane-workspaces/v2/<org_id>/<workspace_id>/terraform.tfstate`
  — a single committed backend file cannot express the workspace segment, and the segment is
  the only thing separating one tenant's record of what exists from another's.
- **The `ACCOUNT_ID` placeholder convention would be refused here.** `variables.tf` validates
  `account_id` against `^[0-9]{12}$` precisely so an unsubstituted file stops the plan. See
  the run block `an_unsubstituted_account_placeholder_is_refused` in
  `tests/no_inherited_defaults.tftest.hcl`.

Values are supplied per invocation by the caller that owns the workspace request.

## Immutable workspace identity and migration

`org_id` and `workspace_id` come from the trusted, versioned provisioning contract's
`OperationBinding`. The provider must copy those fields from the authorized binding;
caller-supplied `ProvisioningIntent.parameters` must never choose them. Both IDs are
required, with no name-derived default. IDs accept 1–128 ASCII letters, digits, `.`,
`_`, `:`, and `-`, starting with a letter or digit; path separators are refused.

`workspace_name` is a display label. Renaming it preserves state and AWS names. The
infrastructure identifier is the first 32 hexadecimal characters (128 bits) of
SHA-256 over the compact JSON array `[org_id, workspace_id]`. Terraform and Python
use the same encoding. AWS names use `adp-<environment>-spw-<identifier>`; environment
is limited to 2–10 characters so even the longest IAM role name fits 64 characters.
Resources carry full `OrgId` and `WorkspaceId` tags alongside the display `Workspace`
tag. Ownership checks require immutable IDs on each present resource side; named
resources must match the exact immutable prefix, and attachments resolve only to
resources whose ownership was verified. The backend uses the full IDs, without hashing.

Authorization schema 3 binds both immutable IDs as well as account, region,
environment, display label, the exact binary/JSON and ordered destructive inventory.
Preparation records the canonical live IAM provisioning principal from the plan.
Apply re-resolves STS/IAM immediately before Terraform and requires that same
principal for both owned and supplied encryption keys; another session of the same
role is allowed, another user or role in the same account is refused.

Previously prepared name-only plans and schema-1/2 authorizations are **superseded**.
Discard them as apply candidates and prepare a fresh plan in a fresh module directory
against the v2 backend key. Preparation refuses a directory initialized for another
key, local-state adoption, or implicit migration; apply rejects old evidence.

An existing name-only deployment is **not automatically migratable** by this module.
Do not copy/move old state to v2 or retag/import resources to satisfy the guard. Keep
its state and workloads intact. Any such deployment needs a separately reviewed
migration that verifies its owner, backs up state, enumerates replacements/data
movement, and specifies recovery before mutation. Roll back an unapplied preparation
by discarding only the new review directory; after an apply, use actual v2 state and
a newly reviewed plan, never a name-only binary. This initial selected deployment had
no workspace resources or state when its old plans were superseded.

## Networking modes

| Mode | Who owns the network | This module declares |
|------|---------------------|----------------------|
| `owned` | this module | VPC, subnets, gateways, routes, EIP |
| `supplied` | the operator | **no network resource at all** — the VPC is read, not adopted |

`supplied` mode does not silently adopt the supplied network's lifecycle (design item 3):
destroying the workspace cannot destroy a VPC it did not create.
`tests/test_networking_modes.py` asserts every network resource declaration is gated on
`owned`, so that property holds by construction rather than by review.

The `network_ownership` output publishes which mode is active, and resources carry a
`NetworkOwnership` tag (`adp-created` / `supplied`). The plan guard treats a flip of that tag
between before and after as an **ownership change** and refuses it.

## Plan safety, change inventory and cost estimate

The following shows the artifact guard in isolation. It does not initialize or validate a deployment backend; use the maintained init/plan/apply path below for actual deployment. Design item 4 requires that the plan is checked before
an apply is considered:

```bash
terraform plan -out=ws.tfplan            # produce a saved plan
terraform show -json ws.tfplan > ws.json # the guard reads plan JSON, never live AWS

python3 scripts/check_workspace_plan.py \
  --plan-json ws.json --plan-file ws.tfplan \
  --environment dev --workspace-name tenant-alpha \
  --org-id "$BOUND_ORG_ID" --workspace-id "$BOUND_WORKSPACE_ID" \
  --account-id 111122223333 --aws-region us-east-1 \
  --cluster-version 1.33 \
  --inventory inventory.json \
  --estimate estimate.json
```

`--account-id` and `--aws-region` are both required: they are what the guard's identity rules
and its authorization binding are checked *against*, so a missing one is refused up front rather
than treated as "no constraint". Every invocation also requires `--plan-file`, `--inventory`
and `--estimate`. The cluster version is read from the plan; `--cluster-version` supplies it
when unknown, because the control-plane rate depends on its support tier.

Exit `0` means verification passed. A denial prints `DENIED: <reason>`. Inventory and
estimate files are produced only after ownership, saved-artifact and target validation.
The apply entry point below additionally requires the reviewed authorization.

What it refuses, beyond destructive changes:

- resources belonging to another workspace, environment or account;
- an **ownership change** — a `terraform import` of a network-owning resource, or a
  `NetworkOwnership` tag flip. Neither is a delete, so a destructive-change check alone
  passes both;
- an address this module does not declare in its own `*.tf` source;
- an unrecognised action verb, or a plan document with no `format_version`;
- a relationship whose *target* is a resource this plan has not proven is owned — a route table
  association, a route, or a security group rule carries no tags of its own, so being a permitted
  *type* is not evidence of being this workspace's *instance*.

### Authorizing a destroy or replacement

Deletions and replacements require an authorization document bound to **this exact plan**:

```bash
# 1. Have the guard emit the document for the plan under review.
python3 scripts/check_workspace_plan.py --plan-json ws.json --plan-file ws.tfplan \
  --environment dev --workspace-name tenant-alpha \
  --org-id "$BOUND_ORG_ID" --workspace-id "$BOUND_WORKSPACE_ID" \
  --account-id 111122223333 --aws-region us-east-1 \
  --inventory inventory.json --estimate estimate.json \
  --emit-authorization proposed.json

# 2. Review it, then supply it back.
python3 scripts/check_workspace_plan.py --plan-json ws.json --plan-file ws.tfplan \
  --environment dev --workspace-name tenant-alpha \
  --org-id "$BOUND_ORG_ID" --workspace-id "$BOUND_WORKSPACE_ID" \
  --account-id 111122223333 --aws-region us-east-1 \
  --inventory inventory.json --estimate estimate.json \
  --authorize-destroy proposed.json
```

Both `--plan-json` and `--plan-file` are **required for every plan**, including creates and updates. `terraform apply` consumes the saved plan *artifact*; the JSON is only its rendering, so
an approval bound to the rendering alone leaves the applied object unbound. The guard verifies the
artifact is a real saved plan, re-derives its JSON with `terraform show -json`, and refuses if the
document under review is not that artifact's own rendering. Use `--module-dir` when not running
from an initialized module directory, since `show -json` decodes a plan using the provider schemas
in that directory's `.terraform/`.

The document is JSON, not a list of addresses, and it binds:

| Bound to | Why an address list was not enough |
|----------|-----------------------------------|
| the plan JSON's **SHA-256** | a changed plan with an identical address set is a different plan — re-planning a node group from `max_size` 3 to 60 destroys and recreates the same addresses |
| the saved **artifact's SHA-256** | the artifact is what gets applied. Two plans of the same unchanged state render identically on every compared field and differ in their bytes, so this is *not* redundant with the JSON digest — it is the only thing that distinguishes the reviewed plan file from another with the same rendering |
| the target **account, region, environment and workspace**, established from the **plan itself** | the same addresses exist in every workspace, so an approval for one was an approval for all of them. Taken from the plan's own `variables` and resource ARNs — *not* from the command line, because a document written from a flag and then compared against that same flag can never disagree |
| the **ordered destructive actions per address** | approving a `delete` is not approving a `delete,create` replacement. Ordered because `['delete','create']` and `['create','delete']` sort identically and are different operations — the second keeps the old resource alive until the new one exists |
| the **concrete destroyed identities** | an address is a label this module's source chooses; the `id` and `arn` of the *before* side are what identify the object that stops existing |
| a **schema version** | so a future format change cannot be read as a permissive older one |

Matching is **symmetric**: a mismatch in either direction denies. An authorization covering more
than the plan destroys is a reusable approval, and a reusable approval is not an approval of
*this* plan. Note that `--emit-authorization` still exits non-zero for a destructive plan — it
writes the document a reviewer would need and *does not* approve it.

Apply all reviewed plans through the maintained entry point:

```bash
python3 scripts/apply_workspace_plan.py \
  --plan-json ws.json --plan-file ws.tfplan --authorization proposed.json \
  --environment dev --workspace-name tenant-alpha \
  --org-id "$BOUND_ORG_ID" --workspace-id "$BOUND_WORKSPACE_ID" \
  --account-id 111122223333 --aws-region us-east-1 --module-dir . \
  --inventory apply-inventory.json --estimate apply-estimate.json
```

It snapshots the reviewed inputs into a private directory, runs the complete guard,
rechecks the saved binary digest immediately before invoking Terraform, and applies that
same copy. Replacing the original path during verification cannot substitute another plan.
It never replans. Create/update authorizations use the same target and digest checks with
an empty destructive set. Emitting a document records proposed scope; operator review is
still required before calling the apply entry point.

### What the estimate is, and is not

Bounded, offline and deliberately pessimistic: node cost is computed at `scaling_config`
`max_size` with the dearest permitted instance type, against a rate table pinned in the
script (`us-east-1`, dated). An instance type with no pinned rate is **refused**, not priced
at zero, and so is a required value the plan leaves unknown.

Five things decide whether the figure is really a bound:

- **the plan's actions.** `create`, `update` and `no-op` all describe capacity that exists after
  the apply, so all three are priced. Counting only `create` reported a plan that raised
  `max_size` from 3 to 60 as costing **$0.00**. `delete` and `read` are excluded explicitly, and
  the output says so in `not_priced_because_removed_or_read` rather than leaving the reader to
  infer it. An action verb outside those five is refused.
- **the region.** Bounded pricing is available only for `us-east-1`. Other allowed
  infrastructure regions are refused by `--estimate` until their service-specific rates
  are verified; an EC2 multiplier cannot establish a bound for the other services.
- **the version's support tier.** EKS charges $0.10/hour for a cluster in standard support and
  **$0.60/hour** in extended support. A single pinned constant understated an extended-support
  cluster by $365/month.
- **the public IPv4 address.** Owned-mode NAT includes $0.005/hour ($3.65 per
  730-hour month), including an existing or replaced EIP.
- **the node root volumes.** Priced from the launch template's `block_device_mappings`, which is
  also what makes them customer-key encrypted.

It is an upper bound on the priced components, not a forecast. The estimate names the components
it cannot bound — data transfer, CloudWatch ingest, NAT data processing, snapshots and
dynamically-provisioned PVCs, and GPU capacity (#5533) — rather than omitting them and looking
complete. `priced_for_region`, `region_price_multiplier`, `support_policy_reviewed_on` and
`upper_bound_scope` are in the output so the figure can be read with its assumptions rather than
on trust.

Regional multipliers in the policy are reference estimates, not authorization evidence.
The required bounded-estimate step refuses every non-base region, including `us-west-2`
whose reference multiplier is 1.0. Add verified per-service regional pricing before
expanding the deployment lane beyond `us-east-1`.

## Region and version support policy

`scripts/region_version_policy.py` holds the reviewed region allowlist, the EKS version support
calendar, the per-tier control-plane rates and the per-region price multipliers, all under one
`POLICY_REVIEWED_ON` date. Regions and versions are checked **before any mutation** — the
`variables.tf` validations refuse the plan at the variable, and the plan guard re-checks before
it prices anything.

Three tiers, and they are not interchangeable: **standard** and **extended** are createable
(extended deliberately, because a tenant mid-upgrade has a legitimate reason to be there — it is
priced at 6× so the cost of staying is visible rather than silent), and **retired** is not, since
AWS auto-upgrades clusters off retired versions and a workspace created at one would not stay at
the version that was reviewed.

The honest limitation: a pinned snapshot goes stale, so a version whose support lapses after the
review date is still listed here on the day it lapses. That is a deliberate trade against
calling `DescribeAddonVersions` at plan time, which needs a credential, leaves the offline lane,
and makes an API outage indistinguishable from an unsupported version. A stale allowlist fails
**closed** on anything it does not know; a live lookup fails **open** on an outage.

The policy exists in two places — Python for the guard, literals in `variables.tf` because a
Terraform `validation` block cannot call Python — and `tests/test_region_version_policy.py`
parses the literals out of `variables.tf` and fails if the two halves disagree. A copy nobody
compares is how two policies come to disagree.

## Tests

```bash
terraform init -backend=false -input=false   # -backend=false: no credentials needed
terraform validate
terraform test                                # all mock_provider runs, command = plan

python3 -m pytest tests/ -q                   # source-level and guard tests
```

Everything here runs **provider-free**: `mock_provider "aws"` plus `override_data`, and
`command = plan` throughout, so no AWS call is made and nothing is created (AC-01).

Both layers run in CI as the `workspaces` leg of `superplane-infra-plan.yml`'s `tests` job.
`tests/test_ci_coverage.py` fails if a root module with `.tftest.hcl` files has no matrix leg
or no path trigger — a suite CI never invokes reports nothing, which is indistinguishable from
passing.

| File | Covers |
|------|--------|
| `tests/no_inherited_defaults.tftest.hcl` | required inputs, target mismatch, refused accounts and version labels (AC-02) |
| `tests/backend.tftest.hcl` | per-workspace state key shape |
| `tests/cluster_security.tftest.hcl` | encryption, logging, endpoint and access controls |
| `tests/kms_key_policy.tftest.hcl` | the log group's key grants CloudWatch Logs for this log group only, and stays administrable |
| `tests/admin_trust.tftest.hcl` | named machine roles are trusted without MFA; humans need MFA; foreign principals are denied |
| `tests/region_version_policy.tftest.hcl` | unavailable regions and retired/future versions are refused at the variable, before any resource |
| `tests/supplied_networking.tftest.hcl` | supplied-mode plan shape |
| `tests/test_networking_modes.py` | every network resource is gated on `owned` |
| `tests/test_workspace_isolation.py` | only workspace-scoped resource types enter this state |
| `tests/test_control_plane_policy_unchanged.py` | the shared policy still refuses VPCs for the control plane |
| `tests/test_least_privilege.py` | no wildcard actions/resources in the IAM policies |
| `tests/test_plan_safety.py` | the guard above, driven as a subprocess |
| `tests/test_region_version_policy.py` | the support policy is dated, cannot contradict itself (a `standard` tier whose own end date has passed is refused at import), prices every lapsed version at the extended rate, and its Terraform and Python halves cannot drift apart |
| `tests/test_admin_trust_separation.py` | machine-role trust and human MFA trust are separate statements |
| `tests/test_workspace_backend_state_key.py` | the backend block stays empty — `terraform test` cannot see it |
| `tests/test_comment_references_resolve.py` | comments citing a test cite one that exists |

### Two limits worth knowing

- **The plan fixtures in `test_plan_safety.py` are hand-built**, because a real
  `terraform show -json` needs credentials, and AC-01 requires these tests be provider-free.
  Every attribute the guard's identity rules read was therefore verified against
  `terraform providers schema -json` for the pinned provider. Redo that after a provider
  upgrade; the procedure is in the test module's docstring.
- **`test_workspace_backend_state_key.py` reads Terraform as text**, which is weaker than a plan
  assertion. It is used for the one property with no stronger form available: Terraform
  resolves the `backend` block at `init`, so it never enters the plan graph and no
  `terraform test` assertion can see it.

## Boundaries

This module does **not** own:

- **the AWS account** — `account_id` names one that already exists; creation is #5531 (w6-08),
  and account closure is never a consequence of removing a workspace;
- **Kubernetes objects** — no `kubernetes` or `helm` provider is declared, which also keeps
  every plan reviewable before the cluster exists. Bootstrap is #5533 (w6-10);
- **a supplied VPC's lifecycle** — see the modes table;
- **registration of the cluster as a usable target** — #5533, #5534.

Tenant capacity never lands on the ADP management cluster (design item 2). This module does
not read `data.terraform_remote_state.platform`, so there is no reference to that cluster
anywhere in it — the property holds by construction, and
`tests/test_workspace_isolation.py` asserts the absence rather than assuming it.

### Plan-time target prerequisites

A supplied KMS key must belong to the selected partition, region and account. Cross-account
keys are outside this module's supported contract. Owned subnet AZ names must be available
standard zones in the selected region. Supplied subnets are read without adoption and must
belong to the selected VPC, in at least two distinct standard AZs. The supported supplied-network path uses an available zonal public NAT gateway: plan-time
reads validate VPC DNS, disabled automatic public IPv4 assignment, effective route tables
(including main-table fallback), absence of node IGW routes, and NAT-to-attached-IGW egress.
Transit and endpoint-only networks are not supported by this module. Free addresses,
NACLs and actual bootstrap connectivity remain live bootstrap checks (#5533).

### Provisioning caller permissions

The provisioning caller is the canonical IAM role/user behind the Terraform credentials,
resolved by `aws_iam_session_context`. The provider restricts credentials to `account_id`
on both plan and apply. The caller must already have `kms:DescribeKey` and `kms:CreateGrant`
on the selected key in its identity policy, in addition to ordinary provisioning permissions.
Do not put `kms:GrantIsForAWSResource` on CreateCluster's grant permission; EKS does not
supply that condition for this call.

Owned keys explicitly authorize that caller in the key policy. Supplied-key requirements
render the same caller statement, separately from the required identity-policy document
in `provisioning_caller_kms_requirements`. Before apply, verify both against the actual caller
and key, including any SCP/boundary/explicit deny. A supplied key template alone does not
establish caller permissions. The module never manages the existing caller's IAM policy.
For a newly created key, the caller's pre-existing IAM permissions must cover the new key
in this account/region; verify those permissions before starting creation.

Authorization identity evidence is checked symmetrically against the authenticated plan:
missing, added or altered destructive IDs, ARNs, names and relationship targets all deny.


## Maintained init, plan and apply path

Account bootstrap must establish the account-wide Auto Scaling service-linked role
before any workspace uses encrypted managed nodes. Preparation and apply verify
`AWSServiceRoleForAutoScaling` with a read-only IAM lookup in the selected account
for both owned and supplied KMS keys, and bind the role ARN into authorization.
A missing or unreadable role refuses preparation before backend initialization;
the workspace module never owns, imports or deletes it. For a newly created
account, the account-bootstrap operator uses the separately authorized command
`aws iam create-service-linked-role --aws-service-name autoscaling.amazonaws.com`,
then reruns workspace preparation. An existing role is reused; do not recreate it
or add it to per-workspace Terraform state. Account Factory's child-account
bootstrap (#5531) is responsible for this prerequisite in newly vended accounts.

Use Terraform **1.9.8**, AWS provider **6.65.0** and TLS provider **4.4.1**.
The backend reader is explicitly versioned against Terraform 1.9.8's saved-plan
format. Other Terraform versions are refused rather than interpreting an unknown
binary format. `node-image-pins.json` records exact AL2023 x86-64 managed-node
release versions for each reviewed region and Kubernetes minor. These were read
from AWS public EKS SSM parameters on 2026-09-20; planning never resolves a moving
recommendation. Updates to the image policy require source and plan review.

The maintained preparation command derives the S3 key from the target in the
JSON tfvars. It refuses a directory initialized for another backend, any local
state migration, non-default Terraform workspaces and implicit CLI flags. It
records the backend actually embedded in the saved binary, not merely the output
naming convention. All managed-resource imports are refused; this provisioning
path does not authorize adopting existing EKS, IAM, KMS or network resources.

From this module directory, prepare the selected target (example backend names
must be replaced with the reviewed deployment's names):

```sh
python3 scripts/prepare_workspace_plan.py \
  --module-dir . --variables /secure/primary.tfvars.json \
  --output-dir /secure/workspace-review-1 \
  --backend-bucket adp-terraform-state-123456789012 \
  --backend-region us-east-1 --lock-table adp-terraform-locks \
  --terraform-binary /opt/terraform/1.9.8/terraform
```

Review the inventory, image pin, estimate and `workspace-authorization.proposed.json`.
Approval applies only to that artifact and backend. Use the reviewed target values
and the same initialized module directory to apply it:

```sh
python3 scripts/apply_workspace_plan.py \
  --module-dir . --terraform-binary /opt/terraform/1.9.8/terraform \
  --plan-file /secure/workspace-review-1/workspace.tfplan \
  --plan-json /secure/workspace-review-1/workspace-plan.json \
  --authorization /secure/workspace-review-1/workspace-authorization.proposed.json \
  --inventory /secure/workspace-review-1/workspace-inventory.json \
  --estimate /secure/workspace-review-1/workspace-estimate.json \
  --account-id 123456789012 --aws-region us-east-1 \
  --environment dev --workspace-name primary \
  --org-id "$BOUND_ORG_ID" --workspace-id "$BOUND_WORKSPACE_ID"
```

Apply verifies both the embedded and initialized backend against the authorization,
runs the full artifact/ownership/estimate guard, and verifies the same private
binary again immediately before Terraform. A JSON-only guard result is not a
substitute for this complete apply path. Direct `terraform apply` is not the
maintained deployment command.

For a rollback, check out the explicitly reviewed previous source into a fresh
module directory and run the same preparation command against the **same** backend
and target, using a new output directory. Review the actual resulting update or
replacement before applying; an old saved plan is not a rollback recipe. For
separately authorized deprovisioning, add `--destroy` to preparation; its first
result is intentionally nonzero until the emitted destructive inventory is reviewed
and authorized. Apply uses the same command above. Supplied networks/keys remain
outside workspace ownership, and no AWS account is closed by this path.

### Supplied-key preflight

A supplied ARN or policy template alone is insufficient. Preparation reads key
metadata, the real key policy and the actual canonical provisioning identity,
checks IAM permission simulation, and asks KMS for a **CreateGrant dry run**. No
grant is created. The key must be an enabled, symmetric, customer-managed encryption
key. Required Logs, Auto Scaling and caller permissions must be established.

This verifier deliberately supports conservative policy shapes: named required
principals (or account-root delegation to verified caller IAM), allow actions, and
the documented service/context conditions or less restrictive conditions. Explicit
Deny, NotAction/NotPrincipal/NotResource and other conditional forms are refused
because this tool is not a general IAM policy evaluator. Missing permissions,
unreadable evidence, an unusable key or an inconclusive dry run prevents apply.
The caller needs read access to key policy and IAM simulation in addition to its
provisioning permissions; absence is a preflight failure, not permission to skip.
The dry run checks a grant to the provisioning caller; it is not an emulation of
every EKS service request, session policy or Organizations condition. The policy
contract and successful checks establish the documented prerequisites, while the
actual AWS operations still enforce their own service-context authorization.

The authorization includes the checked key ARN, caller and key-policy hash.
Immediately before apply, the verifier repeats the live checks and refuses a changed
policy/identity. An old timestamp is never accepted instead of a fresh check.
Permissions can still change after the check; retain the normal Terraform partial-
apply recovery procedure. This module never edits or adopts a supplied key policy
or the caller's IAM policies.

Human MFA trust accepts exact IAM **user** principals using an MFA-authenticated
session. Federated human role sessions are not supported by this input; do not place
human SSO roles in the machine list to work around that restriction. Automation
trust accepts exact IAM roles only, and the two lists must be disjoint. A future
federated-human model needs an explicit reviewed identity-provider/MFA contract.


Relationship ownership is checked independently on every present before/after side.
Deleting or replacing an attachment, inline policy, route association, security-group
rule, node group, or security group requires its old target to resolve against the old
side of an independently verified parent of the correct AWS type. A replacement's new
parent ID never supplies missing evidence for its old target. Unknown create-side IDs
require Terraform's explicit unknown marker plus an authenticated configuration
reference to a verified parent; names, arbitrary local expressions and unrelated
resource types do not substitute for that evidence.

Security-group identity includes VPC scope. Owned mode binds to the verified workspace
VPC; supplied mode requires the saved plan's networking mode, supplied_vpc_id and
local.vpc_id configuration binding. Neither path adopts the supplied VPC's lifecycle.
Explicit conflicting OrgId, WorkspaceId or Environment tags are refused on either side,
including conflicts between tags and tags_all, even for resources with matching names.
These relationships are included in destructive identity evidence and checked for drift.


### Node credential boundary and bootstrap handoff

The launch template requires IMDSv2 with response hop limit 1. The node role has
no CNI permissions and cannot enumerate or pull arbitrary ECR repositories.
`node_image_repository_arns` adds only exact reviewed repository ARNs; the pinned
AWS EKS system repositories are recorded in `node-network-pins.json` with their
AWS source. The ECR authentication API alone requires a wildcard resource.

VPC CNI `v1.22.4-eksbuild.3` uses a dedicated IRSA role restricted to this cluster's
OIDC provider, STS audience and `kube-system/aws-node` service account. The addon
is established before node creation. AWS DescribeAddonVersions was checked on
2026-09-20 for Kubernetes 1.31–1.35 in all five supported regions. The node role
retains the AWS-managed EKS worker discovery policy.

New nodes carry `superplane.aws-e/bootstrap=pending:NoSchedule`. Infrastructure
creation does not establish tenant readiness. Bootstrap (#5533) must configure
Restricted Pod Security admission in tenant namespaces, prevent tenants changing
namespace admission labels or obtaining system namespace privileges, prove normal
pods cannot retrieve node credentials from IPv4/IPv6 IMDS, and prove hostNetwork,
hostPID, privileged and hostPath requests are rejected. Only then may bootstrap
remove the taint and register readiness. Reapplying this infrastructure can restore
the bootstrap taint; bootstrap must revalidate before clearing it again. Tenant
schedulers must not add a bypass toleration before that verification.

Saved-plan ownership checks inspect cluster role ARNs, subnet/security-group
lists, node role ARNs, launch templates and CNI addon bindings on each present
before/after side. Destructive authorization includes these complete relationship
lists. An unknown create reference needs typed authenticated configuration; an
existing target must resolve on its own side. Supplied networking is accepted only
against the authenticated supplied VPC/subnet inputs.
