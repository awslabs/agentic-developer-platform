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
export below and CI's passthrough.

```bash
export TF_VAR_additional_private_subnet_ids_by_az='{"us-east-1a":"subnet-...","us-east-1b":"subnet-..."}'
```

For a CI apply, set the `ADDITIONAL_PRIVATE_SUBNETS_BY_AZ` repository variable to
the same JSON object; `platform-infra-apply.yml` passes it through. Unset or
blank is a no-op — the declared default (`{}`) applies and the cluster's subnet
set is left exactly as it is.

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

## Section 5 — The plan to expect

Run the plan and read it against this expectation before applying:

```bash
cd platform/infra
terraform plan -var-file=../../environments/<env>/platform.tfvars
```

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

`terraform apply` here calls `UpdateClusterConfig`; the cluster stays `ACTIVE`
and the API server is not interrupted.

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

Remove the entry from `TF_VAR_additional_private_subnet_ids_by_az` (or clear the
repository variable) and apply. The cluster's subnet set narrows back to the
networking module's private subnets.

**Rollback is not free, and it is not symmetric.** Nodes already launched into a
removed subnet keep running, but the cluster will no longer place new nodes
there — so rolling back reinstates the exhaustion for anything launched
afterwards. Before rolling back, know why the added subnet was wrong; if it was
the wrong subnet rather than a wrong approach, correcting the entry is better
than emptying it.

---

## Section 8 — Keeping the fix across upgrades

Because the subnet IDs live outside the repository, a routine update could plan
the empty default and shrink the subnet set back — re-breaking pod scheduling
without anyone changing anything on purpose. `--update` runs therefore
rediscover the live cluster's subnet set and re-export the additions Terraform
does not own, keyed by availability zone
(`retain_capacity_subnets` in `platform/scripts/upgrade-state.py`).

Two consequences worth knowing:

- During an update run, configure additions through the `TF_VAR_` export.
  Discovery merges it with what it finds, and the exported
  `platform.tfvars.json` is applied *after* the repository tfvars.
- Discovery refuses rather than dropping what it cannot represent: a subnet whose
  zone it cannot resolve, two additions in the same zone, or an export that
  contradicts the live zone. Each of those, dropped silently, would shrink the
  live subnet set — the exact failure this retention exists to prevent.

---

## Related

- [`docs/adp-platform-deployment/platform_upgrades.md`](../adp-platform-deployment/platform_upgrades.md)
  — "Additional cluster capacity subnets", upgrade-time behaviour
- `platform/infra/modules/eks/variables.tf` —
  `additional_private_subnet_ids_by_az`, the plan-time checks and their rationale
- `platform/infra/modules/eks/tests/additional_capacity_subnets.tftest.hcl` —
  the unchanged-default, additive and refusal contracts
