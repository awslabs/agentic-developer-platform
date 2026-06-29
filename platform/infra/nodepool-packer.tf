# =============================================================================
# Custom Karpenter NodePool — "aggressive-packer" (tight bin-packing)
# =============================================================================
# EKS Auto Mode ships a built-in `general-purpose` NodePool that is AWS-managed
# and CANNOT be edited. Its disruption budget is capped at 10% of nodes (so
# Karpenter reclaims ~1 node per cycle) and it has no instance-size cap, so it
# provisions oversized nodes (observed: an m5a.4xlarge / 16 vCPU sitting nearly
# empty) and then leaves a low-utilization cluster scattered across many
# single-pod nodes. Result: the idle node floor stayed at ~7-10 nodes overnight
# even with zero agent work and no do-not-disrupt pins remaining.
#
# This custom NodePool overrides that behaviour for workloads that select it:
#   - weight 100  -> preferred over general-purpose for new provisioning
#   - instance-size capped to <= xlarge -> no oversized half-empty nodes; small
#     always-on infra pods (KEDA 100m, adot, agent-context) pack tightly
#   - disruption budget 50% with reasons [Underutilized, Empty] -> re-packs many
#     nodes per cycle instead of one, so the cluster actually consolidates down
#
# References the existing AWS-managed `default` NodeClass (kind: NodeClass,
# group: eks.amazonaws.com) — no separate NodeClass needed; Auto Mode manages
# AMI/subnets/SG/role through it.
#
# API verified live (2026-06-29): kubectl apply --dry-run=server accepted this
# exact spec against the EKS Auto Mode admission webhook (karpenter.sh/v1,
# eks.amazonaws.com/v1 NodeClass, instance-size cap, reasons array, weight).
#
# Applied via null_resource + kubectl (mirrors the warm-pool/prepull pattern in
# webhook-ingress): NodePool is a CRD whose status the Karpenter controller
# mutates, which causes kubernetes_manifest server-side read-back churn.
# =============================================================================

locals {
  aggressive_packer_nodepool_yaml = <<-YAML
    apiVersion: karpenter.sh/v1
    kind: NodePool
    metadata:
      name: aggressive-packer
      labels:
        app.kubernetes.io/managed-by: terraform
        app.kubernetes.io/part-of: adp-platform
    spec:
      # Higher weight than the built-in general-purpose pool (which has no
      # explicit weight = 0) so Karpenter prefers this pool for new capacity.
      weight: 100
      template:
        metadata:
          labels:
            adp.io/pool: aggressive-packer
        spec:
          nodeClassRef:
            group: eks.amazonaws.com
            kind: NodeClass
            name: default
          requirements:
            - key: karpenter.sh/capacity-type
              operator: In
              values: ["on-demand"]
            - key: kubernetes.io/arch
              operator: In
              values: ["amd64"]
            - key: kubernetes.io/os
              operator: In
              values: ["linux"]
            - key: eks.amazonaws.com/instance-category
              operator: In
              values: ["c", "m", "r"]
            - key: eks.amazonaws.com/instance-generation
              operator: Gt
              values: ["4"]
            # Size cap: keep nodes small so tiny always-on infra pods bin-pack
            # instead of each landing on its own oversized node. xlarge (4 vCPU /
            # ~8-16Gi) is the ceiling — still fits the agent worker (1 vCPU/4Gi).
            - key: eks.amazonaws.com/instance-size
              operator: In
              values: ["medium", "large", "xlarge"]
      disruption:
        consolidationPolicy: WhenEmptyOrUnderutilized
        consolidateAfter: 30s
        budgets:
          # Allow up to half the pool's nodes to be consolidated at once, but
          # only for Underutilized/Empty reasons — never throttle voluntary
          # bin-packing the way the built-in 10% budget does.
          - nodes: "50%"
            reasons: ["Underutilized", "Empty"]
  YAML
}

resource "null_resource" "aggressive_packer_nodepool" {
  triggers = {
    manifest_sha   = sha256(local.aggressive_packer_nodepool_yaml)
    cluster_name   = module.eks.cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region} >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.aggressive_packer_nodepool_yaml}
EOF
    CMD
  }

  # Best-effort destroy — remove the NodePool so its nodes fall back to
  # general-purpose rather than being orphaned.
  provisioner "local-exec" {
    when       = destroy
    command    = "kubectl delete nodepool aggressive-packer --ignore-not-found || true"
    on_failure = continue
  }

  depends_on = [module.eks]
}
