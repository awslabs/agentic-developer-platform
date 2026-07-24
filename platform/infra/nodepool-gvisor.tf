# =============================================================================
# Custom Karpenter NodePool — "gvisor" (sandboxed agent execution)
# =============================================================================
# Provides a dedicated Karpenter NodePool for gVisor-capable nodes. Pods that
# request RuntimeClass "gvisor" and tolerate the taint land here; all other
# workloads are repelled by the NoSchedule taint.
#
# Background:
# - EKS Managed Node Groups DO NOT register on this EKS Auto Mode cluster
#   (11 launch-template versions attempted in #2374/#2430; no CSR ever produced).
# - A Karpenter NodePool registered a node Ready in <30s during live validation
#   on #2382 (2026-06-30).
# - EKS Auto Mode exposes only the eks.amazonaws.com/v1 NodeClass ("default");
#   the karpenter.k8s.aws/v1 EC2NodeClass CRD is NOT available.
#
# Design:
# - weight 10: below aggressive-packer (100) so it doesn't attract general pods
#   that happen to fit, but above the built-in general-purpose pool (weight 0).
#   Only pods that explicitly match the gvisor taint/nodeSelector land here.
# - Label adp.io/runtime=gvisor: for nodeSelector in RuntimeClass scheduling.
# - Taint adp.io/runtime=gvisor:NoSchedule: isolation fence — no existing
#   workload without an explicit toleration can schedule here.
# - Cost knobs (from #2326): on-demand only, c/m/r categories, gen>4,
#   instance-size cap (medium/large/xlarge), consolidation when empty/underutilized.
# - min=0 semantics: Karpenter only provisions nodes when a pending pod matches.
#   Zero cost when no gVisor agent work is running.
#
# Applied via null_resource + kubectl (same pattern as nodepool-packer.tf):
# NodePool is a CRD whose status the Karpenter controller mutates, causing
# kubernetes_manifest provider read-back churn.
#
# References: #2358 (sub-EPIC), #2511 (this story), #2382 (live validation),
#             #2374/#2430 (managed NG failures), #2326 (cost principles)
# =============================================================================

locals {
  gvisor_nodepool_yaml = <<-YAML
    apiVersion: karpenter.sh/v1
    kind: NodePool
    metadata:
      name: gvisor
      labels:
        app.kubernetes.io/managed-by: terraform
        app.kubernetes.io/part-of: adp-platform
        app.kubernetes.io/component: gvisor-runtime
    spec:
      # Weight 10: above built-in general-purpose (0) but well below
      # aggressive-packer (100). Karpenter only routes pods here if they
      # tolerate the gvisor taint AND match the nodeSelector.
      weight: 10
      template:
        metadata:
          labels:
            adp.io/runtime: gvisor
            adp.io/pool: gvisor
        spec:
          taints:
            - key: adp.io/runtime
              value: gvisor
              effect: NoSchedule
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
            # Size cap: keep gVisor nodes reasonably sized. Agent workers need
            # ~1-2 vCPU / 4-8Gi; xlarge (4 vCPU / 8-16Gi) is the ceiling.
            # Matches the aggressive-packer cost controls from #2326.
            - key: eks.amazonaws.com/instance-size
              operator: In
              values: ["medium", "large", "xlarge"]
      disruption:
        consolidationPolicy: WhenEmptyOrUnderutilized
        consolidateAfter: 30s
        budgets:
          - nodes: "50%"
            reasons: ["Underutilized", "Empty"]
  YAML
}

resource "null_resource" "gvisor_nodepool" {
  triggers = {
    manifest_sha   = sha256(local.gvisor_nodepool_yaml)
    cluster_name   = module.eks.cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    environment = {
      KUBECONFIG = "/tmp/adp-deploy-kubeconfig"
    }
    command = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region} --kubeconfig /tmp/adp-deploy-kubeconfig >/dev/null
      echo '--- Dry-run validation (server-side) ---'
      cat <<'EOF' | kubectl apply --dry-run=server -f -
${local.gvisor_nodepool_yaml}
EOF
      echo '--- Applying gVisor NodePool ---'
      cat <<'EOF' | kubectl apply -f -
${local.gvisor_nodepool_yaml}
EOF
    CMD
  }

  # Best-effort destroy — remove the NodePool so its nodes drain and fall back
  # to general-purpose rather than being orphaned.
  provisioner "local-exec" {
    when       = destroy
    environment = {
      KUBECONFIG = "/tmp/adp-deploy-kubeconfig"
    }
    command    = "kubectl delete nodepool gvisor --ignore-not-found || true"
    on_failure = continue
  }

  depends_on = [module.eks]
}
