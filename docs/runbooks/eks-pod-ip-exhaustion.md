# Runbook: Relieving EKS Pod IP Exhaustion

Covers the case where existing pods keep running but nothing new can start
because the cluster's private subnets have no free IP addresses left: how to
confirm that is what you are looking at, the supported way to add already-existing
capacity subnets, the exact plan to expect before applying, and rollback.

**Issue:** #5830 · **Blocked epic:** #3959

---

## Section 1 — Confirming the diagnosis

The symptom is asymmetric, and that asymmetry is the tell: running workloads are
healthy and serving, while every newly scheduled pod stays `Pending` or
`ContainerCreating`. The CNI reports it on the pod's events:

```bash
kubectl describe pod -n <namespace> <pod> | grep -A3 "failed to assign an IP"
```

Confirm it at the source — the cluster's subnets, not the node count:

```bash
CLUSTER=adp-<env>-eks-cluster
SUBNETS=$(aws eks describe-cluster --name "$CLUSTER" \
  --query 'cluster.resourcesVpcConfig.subnetIds[]' --output text)
aws ec2 describe-subnets --subnet-ids $SUBNETS \
  --query 'Subnets[].{Subnet:SubnetId,AZ:AvailabilityZone,Free:AvailableIpAddressCount}' \
  --output table
```

`Free: 0` on the subnets the cluster uses is the confirmation. This is capacity
exhaustion, not scheduling pressure and not a broken CNI: nodes may well launch
successfully and then be unable to hand out pod addresses.

**Not the problem, and not the fix:**

- Scaling the node pool. More nodes in an exhausted subnet get no more addresses.
- The managed `default` NodeClass. On Auto Mode it exposes no
  `subnetSelectorTerms` — it derives its subnets from the cluster's
  `resourcesVpcConfig.subnetIds`. Editing it is not a fix and not safe: Auto Mode
  reconciles such edits away. (`kubectl get nodeclass default -o yaml` to see
  this; the `karpenter.k8s.aws/v1` `EC2NodeClass` CRD is not present on an Auto
  Mode cluster.)
- Deleting pods to "free" addresses. That destroys running work and buys nothing
  durable.

---

## Section 2 — What the supported fix is

Add **already-existing** private subnets, with free addresses, to the cluster's
own subnet set. `UpdateClusterConfig` supports changing an existing cluster's
subnets (at least two, in at least two availability zones; the VPC cannot
change), and because the managed NodeClass reads that set, widening it is what
lets newly launched nodes obtain pod addresses.

Properties of the change, so you know what you are and are not authorising:

- **Additive.** The original subnets always remain in the set. Nothing is swapped
  out, so no existing node or pod loses its network.
- **Creates nothing.** No subnet, NAT gateway or VPC endpoint. You supply subnets
  that already exist and already route outbound through the existing private
  path. If no such subnet exists, creating one is a separate, separately reviewed
  change.
- **Cluster-scoped.** Only the EKS cluster's subnet set changes. The RDS subnet
  group, load balancers, Lambda VPC configurations and VPC endpoints all select
  from the networking module's private subnets, which this does not touch.
- **Forward-looking only.** Existing nodes are not moved or recycled. Only nodes
  launched after the apply can use the added subnets, so relief arrives as
  capacity turns over — it is not instantaneous for already-pending pods on
  existing nodes.

---

## Section 3 — Choosing the subnets

Pick one subnet per availability zone, in zones the cluster already has capacity
in, each with a comfortable number of free addresses:

```bash
VPC=$(aws eks describe-cluster --name "$CLUSTER" \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" \
  --query 'Subnets[?AvailableIpAddressCount>`100`].{Subnet:SubnetId,AZ:AvailabilityZone,Free:AvailableIpAddressCount,Public:MapPublicIpOnLaunch}' \
  --output table
```

For each candidate, confirm the default route goes through NAT rather than an
internet gateway:

```bash
aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=<subnet>" \
  --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0`]' --output json
```

You do not have to get this right by inspection alone — the configuration
re-checks all of it at plan time and refuses (§4). Checking first just means you
find out before you are reading a failed plan.

---

## Section 4 — Supplying the subnets

Subnet IDs are account-specific and are deliberately not committed to this
repository, so they are supplied per-invocation rather than in
`environments/<env>/platform.tfvars`. An assignment in a `-var-file` — even
`= {}` — overrides `TF_VAR_`, so adding one there would silently defeat both the
export below and the CI resolution described further down.

```bash
export TF_VAR_additional_private_subnet_ids_by_az='{"us-east-1a":"subnet-...","us-east-1b":"subnet-..."}'
```

For a CI apply, set the `ADDITIONAL_PRIVATE_SUBNETS_BY_AZ` repository variable to
the same JSON object.

**Unset or blank is not a no-op once the cluster has been widened**, and this is
the trap worth understanding before you rely on any of it. The declared default
is `{}`, so on a cluster whose exhaustion has already been relieved, "nothing
configured" means "the subnet set is just the networking private subnets" — and
the plan removes the additions, re-breaking pod IP assignment for every node
launched afterwards. Nobody asked for that change; it is the absence of
configuration being read as an instruction.

Because the ids live outside the repository, unset and "deliberately none" are
indistinguishable at the variable. Only the live cluster knows which is true, so
the deployment paths resolve the effective value against it
(`platform/scripts/resolve-capacity-subnets.py`, rules in `capacity_subnets.py`):

| configuration | result |
|---|---|
| unset | retain what the cluster has |
| blank / whitespace | retain what the cluster has |
| omits a live addition | **refuse**, naming the subnet |
| names more | allowed — an operator adding capacity |
| narrowing | only with `ALLOW_CAPACITY_SUBNET_REMOVAL` |

The same resolved value is used for the plan and the apply, so the plan you
review is the plan that applies.

**A bare `terraform apply` bypasses all of this.** The protection lives in the
deployment paths, not in the variable's default. On a widened cluster, follow §5.2
rather than exporting the variable above and planning — and note that "run the
resolver, then export the result" is not one command: exporting straight from a
command substitution reports export's exit status, so it hides the refusal and
plans the additions away. §5.2 has the checked form.

The map is keyed by the availability zone each subnet is expected to be in. That
is what makes one-subnet-per-zone structural: a subnet pasted under the wrong
zone is refused rather than quietly collapsing the added capacity into a single
zone while the configuration claims two.

**The plan refuses**, before anything is applied, if a subnet is not in the
cluster's VPC, is not really in the zone it is keyed by, is in a zone the cluster
has no existing private subnet in, assigns public IPs on launch, has no
`0.0.0.0/0` route, or reaches `0.0.0.0/0` through an internet gateway instead of
NAT. Malformed IDs and the same subnet listed under two zones are refused before
any AWS lookup happens.

---

## Section 5 — Rolling it out: prerequisite, preserved inputs, scoped saved plan

### 5.1 — Prerequisite: the provider/schema mismatch (#5831)

**A full `terraform plan`/`apply` of `platform/infra` is not currently an
available route on `879318057152/dev`, and this is a hard prerequisite rather
than a caveat.** The platform source constrains AWS to `~> 5.0` and resolves
provider 5.100.0, while existing state for `module.eks.aws_eks_addon.coredns`
carries newer schema fields (`namespace_config`, and resource identity
`account_id`/`addon_name`/`cluster_name`/`region`). The observable consequences:

- a read-only targeted plan of the cluster warns `Failed to decode resource from
  state ... unsupported attribute "namespace_config"`;
- `terraform show -json <saved-plan>` **fails** with `no resource identity schema
  found for aws_eks_addon.coredns`.

The second one is what blocks rollout: if the saved plan cannot be rendered as
JSON, it cannot be inspected, and the reviewed-plan requirement cannot be met.
**Do not work around this** by stripping unsupported fields or identity from
state, by forgetting/reimporting the add-on, or by applying without the saved-plan
inspection. #5831 owns the durable repair; this change waits on it.

Also note the root module carries unrelated pending changes, so an ordinary full
apply would sweep in collateral nobody reviewed. Target the cluster (§5.3).

### 5.2 — Resolve the inputs, in a private run directory, and check the refusal

Three variables identify the target before anything below runs: `ENV` (the
environment being repaired, e.g. `dev`), `ACCOUNT_ID` (its 12-digit AWS account)
and `STATE_BUCKET` (`adp-terraform-state-$ACCOUNT_ID`). Set them in the shell you
will use for the whole of §5.

On a cluster that has already been widened, resolve the effective subnet map
before planning, so the plan preserves the live additions instead of defaulting
them away (§4):

```bash
# runbook-block: resolve-inputs
set -euo pipefail
cd platform/infra

# Private run directory: a saved plan and its rendered JSON contain real
# infrastructure values, so they must not sit at a predictable path under a
# world-readable /tmp. mktemp -d creates it 0700; the chmod is belt-and-braces
# against a permissive umask.
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/adp-capacity-subnets.XXXXXX")
chmod 700 "$RUN_DIR"

# Checked assignment BEFORE export. `export VAR="$(cmd)"` reports export's own
# exit status, not the command's, so a refusal from the resolver would be
# swallowed, VAR would be empty, and the plan below would remove the additions —
# the exact failure this resolver exists to prevent. Under `set -e` a bare
# assignment from a command substitution does propagate the failure, so this
# stops here.
SUBNETS=$(python3 ../scripts/resolve-capacity-subnets.py \
  --environment "$ENV" --bucket "$STATE_BUCKET" \
  --configured "${ADDITIONAL_PRIVATE_SUBNETS_BY_AZ:-}")
export TF_VAR_additional_private_subnet_ids_by_az="$SUBNETS"
echo "$TF_VAR_additional_private_subnet_ids_by_az"

# Canonical retained inputs for this deployment, discovered read-only from live
# state and written 0600 into the private directory above. This is what makes the
# plan carry EVERY account-specific value (public access CIDRs, cluster-admin
# principals, ECR encryption, retained KMS keys) rather than only the two an
# operator happens to remember to export. It re-applies the §4 rules to the
# export above and refuses identically, so it is not a way around them.
python3 ../scripts/upgrade-state.py prepare \
  --directory "$RUN_DIR" --account "$ACCOUNT_ID" --environment "$ENV"
```

Echo and read the resolved value: it is the input the plan and the apply both use.

Two things about precedence, because getting them the wrong way round is how an
input goes missing. `$RUN_DIR/platform.tfvars.json` is passed **after** the
committed `environments/$ENV/platform.tfvars`, so the discovered live values win
over repository defaults — that is the point of discovering them. And because a
later `-var-file` also overrides `TF_VAR_`, the export above reaches the plan only
via that file, which is why discovery merges it in rather than the export standing
on its own.

A refusal from either command stops §5 here. That is the intended outcome, not an
error to work around: fix the configuration it names, then re-run this block.

### 5.3 — Save a scoped plan, then inspect that file

```bash
# runbook-block: scoped-plan
set -euo pipefail
terraform plan -var-file=../../environments/"$ENV"/platform.tfvars \
  -var-file="$RUN_DIR/platform.tfvars.json" \
  -target=module.eks.aws_eks_cluster.main -out="$RUN_DIR/subnets.tfplan"
terraform show -json "$RUN_DIR/subnets.tfplan" > "$RUN_DIR/subnets.plan.json"  # must exit 0 — see §5.1
```

Review `$RUN_DIR/subnets.plan.json` against §5.5, then apply **that saved file**
(`terraform apply "$RUN_DIR/subnets.tfplan"`) so the reviewed plan is the applied
plan. Re-planning between review and apply discards the review. Remove the run
directory afterwards (`rm -rf "$RUN_DIR"`).

> `.github/scripts/verify_scoped_plan.py` has **no scope entry for this change**,
> so there is no automated guard over this plan — the review is a human one
> against the expectations below. `-target` also prunes anything the target does
> not depend on, which is why the subnet and route-table validations are wired
> upstream of the cluster (`modules/eks/main.tf`); they run under a targeted plan
> and their regression tests assert exactly that.

The resolve block in §5.2 and this plan block are executed as a pair by
`platform/scripts/tests/test_capacity_subnet_runbook.py`, which extracts them from
this file and runs them with a refusing resolver and a recording `terraform` stub.
It passes only if the refusal stops the procedure with no `terraform` invocation at
all — so the failure-masking cannot be reintroduced here without turning that test
red. Keep the block markers and the checked-assignment shape when editing.

### 5.4 — Recheck free capacity immediately before applying

```bash
aws ec2 describe-subnets --subnet-ids <added-subnets> \
  --query 'Subnets[].{Subnet:SubnetId,Free:AvailableIpAddressCount}' --output table
```

Require **at least 6** free addresses per added subnet (EKS's minimum; 16+ is
recommended for real headroom). The plan-time postcondition checks the same
threshold, but it is **point-in-time at plan**: other workloads can consume those
addresses between plan and apply, and a plan that passed does not prove the
capacity is still there. This recheck, immediately before rollout, is what does.

### 5.5 — The plan to expect

**Expected:** exactly one in-place update.

```
  ~ resource "aws_eks_cluster" "main" {           # module.eks
      ~ vpc_config {
          ~ subnet_ids = [
              # the existing subnet IDs, unchanged
            + "subnet-<added>",
            ...
            ]
        }
    }

Plan: 0 to add, 1 to change, 0 to destroy.
```

**Stop and do not apply** if the plan shows any of the following:

- Any `destroy`, `replace`, or `# forces replacement` — most importantly on the
  cluster, the VPC, or any subnet. `subnet_ids` is an in-place attribute; a
  replacement means something else changed too.
- A **removal** from `subnet_ids`. The change is additive; an existing subnet
  leaving the set is a network outage for whatever launches next.
- Changes to the RDS subnet group, load balancer subnets, Lambda VPC
  configuration, or VPC endpoint subnets. These are fed by the networking
  module's private subnets and must not move.
- New paid capacity: `aws_subnet`, `aws_nat_gateway`, `aws_eip`,
  `aws_vpc_endpoint`.
- More than one resource changing, unless the extra changes have been separately
  reviewed as unrelated pre-existing drift.

Applying the saved plan calls `UpdateClusterConfig`; the cluster stays `ACTIVE`
and the API server is not interrupted. The change is also not instantaneous
relief — see §2's "forward-looking only".

---

## Section 6 — Verifying the relief

```bash
# 1. The cluster's subnet set now includes the additions:
aws eks describe-cluster --name "$CLUSTER" \
  --query 'cluster.resourcesVpcConfig.subnetIds' --output json

# 2. Existing work is still running — check this BEFORE looking for new pods:
kubectl get pods -A --field-selector=status.phase=Running | wc -l
kubectl get nodes

# 3. New nodes land in the added subnets and pods on them get addresses:
kubectl get pods -A --field-selector=status.phase=Pending
kubectl get nodes -o custom-columns='NODE:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone'
```

Relief is confirmed when a newly started representative pod reaches `Running`
with an assigned IP, and every previously running workload is still running. If
pending pods remain on existing, exhausted nodes, they need new node capacity —
the added subnets only serve nodes launched after the apply.

---

## Section 7 — Rollback

Rollback is a **deliberate narrowing**, so clearing the variable is deliberately
not enough — that is now read as "retain" (§4), precisely so an accidental blank
cannot roll back for you. To narrow on purpose, authorise it.

Prerequisites: `ENV`, `ACCOUNT_ID` and `STATE_BUCKET` as in §5.2, plus
`REDUCED_MAP` — the zone-keyed map you intend to be left with, `{}` to remove all
additions. It is a placeholder here; nothing below has been run for you.

```bash
# runbook-block: rollback
set -euo pipefail
cd platform/infra
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/adp-capacity-subnets.XXXXXX"); chmod 700 "$RUN_DIR"

# State the reduced map you intend, and authorise the removal. Checked assignment
# before export, for the §5.2 reason: `export VAR="$(cmd)"` would hide a refusal
# and plan an empty map you did not choose.
SUBNETS=$(python3 ../scripts/resolve-capacity-subnets.py \
  --environment "$ENV" --bucket "$STATE_BUCKET" \
  --configured "$REDUCED_MAP" --allow-removal)
echo "Rolling back to: $SUBNETS"

# The resolved map must actually reach the plan. Discovery would re-derive the
# CURRENT live set — which is what you are deliberately narrowing — so write the
# reduced map to its own file and pass it LAST, after the discovered inputs, so it
# is the value that wins. Precedence is last-var-file-wins, and this is the one
# place in this runbook where overriding discovery is the intent.
python3 ../scripts/upgrade-state.py prepare \
  --directory "$RUN_DIR" --account "$ACCOUNT_ID" --environment "$ENV"
umask 077
printf '{"additional_private_subnet_ids_by_az":%s}\n' "$SUBNETS" \
  > "$RUN_DIR/rollback.tfvars.json"

terraform plan -var-file=../../environments/"$ENV"/platform.tfvars \
  -var-file="$RUN_DIR/platform.tfvars.json" \
  -var-file="$RUN_DIR/rollback.tfvars.json" \
  -target=module.eks.aws_eks_cluster.main -out="$RUN_DIR/rollback.tfplan"
terraform show -json "$RUN_DIR/rollback.tfplan" > "$RUN_DIR/rollback.plan.json"
```

For a CI rollback, set the `ALLOW_CAPACITY_SUBNET_REMOVAL` repository variable for
the run instead; the resolver step already passes the authorisation through.

Review `$RUN_DIR/rollback.plan.json` and then apply that saved file, as in §5.3.
Expect the mirror image of §5.5: one in-place cluster update whose `subnet_ids`
**loses** the subnets you named and keeps everything else — the removal is the
intended change here, so it is the one context in this runbook where a removal
from `subnet_ids` is not a stop signal. A destroy, a replacement, or any other
resource changing still is. Remove the run directory afterwards.

**Rollback is not free, and it is not symmetric.** Nodes already launched into a
removed subnet keep running, but the cluster will no longer place new nodes
there — so rolling back reinstates the exhaustion for anything launched
afterwards. Before rolling back, know why the added subnet was wrong; if it was
the wrong subnet rather than a wrong approach, correcting the entry is better
than emptying it.

---

## Section 8 — Keeping the fix across upgrades

Because the subnet IDs live outside the repository, a routine deployment could
plan the empty default and shrink the subnet set back — re-breaking pod
scheduling without anyone changing anything on purpose. Passing a repository
variable through does not prevent that; the variable is exactly what goes stale.

So both paths that could drop the subnets decide the effective map against the
**live cluster**, using one shared rules module,
`platform/scripts/capacity_subnets.py`:

| path | entry point |
|---|---|
| CI apply | `platform-infra-apply.yml` → `resolve-capacity-subnets.py` |
| upgrade discovery | `retain_capacity_subnets` in `platform/scripts/upgrade-state.py` |

The rules are the §4 table: absent retains, an omission refuses, more is allowed,
narrowing needs authorisation. They live in one module because the failure is
identical on both paths, and they are tested once
(`platform/scripts/tests/test_capacity_subnets.py`).

Consequences worth knowing:

- The CI resolver runs for **scoped** applies too, not just full ones: a scoped
  target's dependency graph can reach the cluster, so skipping resolution there
  would reintroduce the removal on exactly the path used for rollout.
- The resolved value is used for both plan and apply, so a reviewed plan is not
  invalidated by re-resolution.
- A failed read is never treated as "no additions". A cluster that does not exist
  is a first deployment; any other AWS failure propagates, because interpreting
  it as "nothing to retain" is the silent-removal path itself.
- Retention refuses rather than dropping what it cannot represent: a subnet whose
  zone will not resolve, two additions in one zone, a configured subnet
  contradicting the live zone, or a live cluster subnet that platform Terraform
  manages but is not among the networking private subnets — an ownership shape
  this retention cannot reason about. Each, dropped silently, would shrink the
  live set.
- Baseline membership comes from the networking module's **private** subnets (the
  platform `private_subnet_ids` output), not from every Terraform-managed
  `aws_subnet` — public subnets are managed too, and counting them as
  already-wired would let a managed-but-not-baseline subnet leave the cluster's
  set unannounced.

---

## Related

- [`docs/adp-platform-deployment/platform_upgrades.md`](../adp-platform-deployment/platform_upgrades.md)
  — "Additional cluster capacity subnets", upgrade-time behaviour
- `platform/infra/modules/eks/variables.tf` —
  `additional_private_subnet_ids_by_az`, the plan-time checks and their rationale
- `platform/infra/modules/eks/tests/additional_capacity_subnets.tftest.hcl` —
  the unchanged-default, additive and refusal contracts, including that the
  refusals still hold under a plan that targets only the cluster
- `platform/scripts/capacity_subnets.py` — the retention rules shared by CI and
  upgrade discovery; `platform/scripts/resolve-capacity-subnets.py` is the CLI,
  `platform/scripts/tests/test_capacity_subnets.py` the tests
- Issue #5831 — the provider/schema mismatch that must be repaired before a
  saved plan for this change can be inspected on `dev` (§5.1)
