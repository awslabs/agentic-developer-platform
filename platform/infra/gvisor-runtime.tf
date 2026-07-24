# =============================================================================
# gVisor RuntimeClass + RBAC
# =============================================================================
# Creates the Kubernetes RuntimeClass "gvisor" so pods can request sandboxed
# execution via `runtimeClassName: gvisor`. Also grants RBAC for:
# - agent-scaledjob-sa (adp-agents): get/list/watch runtimeclasses (pod needs
#   to reference it)
# - github-runner-sa (arc-runners): full CRUD on runtimeclasses (CI/CD kubectl
#   apply for this file's null_resource)
#
# Applied via null_resource + kubectl (same pattern as nodepool-packer.tf):
# RuntimeClass is a simple CRD but the kubernetes_manifest provider read-back
# causes spurious diffs when the API server normalizes empty scheduling fields.
#
# References: #2358 (parent), #2374, #2373 (Phase 0)
# =============================================================================

# -----------------------------------------------------------------------------
# RuntimeClass manifest
# -----------------------------------------------------------------------------

locals {
  gvisor_runtimeclass_yaml = <<-YAML
    apiVersion: node.k8s.io/v1
    kind: RuntimeClass
    metadata:
      name: gvisor
      labels:
        app.kubernetes.io/managed-by: terraform
        app.kubernetes.io/part-of: adp-platform
        app.kubernetes.io/component: gvisor-runtime
    handler: runsc
    scheduling:
      nodeSelector:
        adp.io/runtime: gvisor
      tolerations:
        - key: adp.io/runtime
          operator: Equal
          value: gvisor
          effect: NoSchedule
  YAML
}

resource "null_resource" "gvisor_runtimeclass" {
  triggers = {
    manifest_sha   = sha256(local.gvisor_runtimeclass_yaml)
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
      echo '--- Dry-run validation ---'
      cat <<'EOF' | kubectl apply --dry-run=server -f -
${local.gvisor_runtimeclass_yaml}
EOF
      echo '--- Applying RuntimeClass ---'
      cat <<'EOF' | kubectl apply -f -
${local.gvisor_runtimeclass_yaml}
EOF
    CMD
  }

  # Best-effort destroy — remove the RuntimeClass on terraform destroy
  provisioner "local-exec" {
    when       = destroy
    environment = {
      KUBECONFIG = "/tmp/adp-deploy-kubeconfig"
    }
    command    = "kubectl delete runtimeclass gvisor --ignore-not-found || true"
    on_failure = continue
  }

  depends_on = [module.eks]
}

# -----------------------------------------------------------------------------
# RBAC: agent-scaledjob-sa — read access to runtimeclasses
# -----------------------------------------------------------------------------
# The agent worker pods run as agent-scaledjob-sa in adp-agents namespace.
# They need to reference the gVisor RuntimeClass; the API server validates that
# the named RuntimeClass exists when a pod spec includes runtimeClassName.
# Granting get/list/watch is sufficient (no create/update/delete needed by pods).

resource "kubernetes_cluster_role" "agent_runtimeclass_reader" {
  metadata {
    name = "agent-runtimeclass-reader"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-platform"
      "app.kubernetes.io/component"  = "gvisor-rbac"
    }
  }

  rule {
    api_groups = ["node.k8s.io"]
    resources  = ["runtimeclasses"]
    verbs      = ["get", "list", "watch"]
  }

  depends_on = [module.eks]
}

resource "kubernetes_cluster_role_binding" "agent_runtimeclass_reader" {
  metadata {
    name = "agent-runtimeclass-reader"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-platform"
      "app.kubernetes.io/component"  = "gvisor-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.agent_runtimeclass_reader.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "agent-scaledjob-sa"
    namespace = "adp-agents"
  }

  depends_on = [module.eks]
}

# -----------------------------------------------------------------------------
# RBAC: github-runner-sa — full CRUD on runtimeclasses (CI/CD)
# -----------------------------------------------------------------------------
# The CI runner (github-runner-sa in arc-runners) runs `kubectl apply` for the
# RuntimeClass manifest via the null_resource above. It needs create/update/patch
# permissions on cluster-scoped runtimeclasses. Modeled on the PriorityClass
# RBAC pattern in warm-pool.tf.

resource "kubernetes_cluster_role" "runner_runtimeclass_manage" {
  metadata {
    name = "runner-runtimeclass-manage"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-platform"
      "app.kubernetes.io/component"  = "gvisor-rbac"
    }
  }

  # create cannot be name-restricted (the object doesn't exist yet on first apply)
  rule {
    api_groups = ["node.k8s.io"]
    resources  = ["runtimeclasses"]
    verbs      = ["get", "list", "watch", "create"]
  }

  # Mutating verbs restricted to just the "gvisor" RuntimeClass (least privilege)
  rule {
    api_groups     = ["node.k8s.io"]
    resources      = ["runtimeclasses"]
    resource_names = ["gvisor"]
    verbs          = ["update", "patch", "delete"]
  }

  depends_on = [module.eks]
}

resource "kubernetes_cluster_role_binding" "runner_runtimeclass_manage" {
  metadata {
    name = "runner-runtimeclass-manage"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-platform"
      "app.kubernetes.io/component"  = "gvisor-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.runner_runtimeclass_manage.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }

  depends_on = [module.eks]
}
