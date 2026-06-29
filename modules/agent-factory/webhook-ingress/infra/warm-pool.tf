# =============================================================================
# Agent Warm Pool — overprovisioning "balloon" for instant agent starts
# =============================================================================
# Problem: on a cluster running scale-from-zero with few nodes, a summoned agent
# pod (KEDA ScaledJob, minReplicaCount=0) waits for BOTH a cold node to be
# provisioned (~60-90s on EKS Auto Mode / Karpenter) AND the ~650 MB agent image
# to be pulled (~30s) before it can run. Observed: first comment in 1-2 min on a
# 1-node test cluster, vs 10-15s on a warm multi-node cluster. (Issue context:
# PR #1304 follow-up — agent latency on fresh/small accounts.)
#
# Fix (delivers warm capacity AND a pre-pulled image in one mechanism): run N
# "balloon" pods that
#   - run the SAME agent image (so the node they hold has it pre-pulled), and
#   - sit at a NEGATIVE PriorityClass.
# A real agent job (default priority 0) PREEMPTS a balloon, so it lands instantly
# on that already-running, image-cached node. The evicted balloon reschedules and
# triggers a fresh warm node in the background for the next summon — self-
# replenishing. Set agent_warm_pool_replicas = 0 to disable.
#
# Applied via kubectl local-exec (same rationale as scaledjob.tf): avoids the
# kubernetes_manifest server-side read-back/diff churn for objects the scheduler
# and Karpenter mutate.
# =============================================================================

# -----------------------------------------------------------------------------
# RBAC for the kubectl-apply runner SA (github-runner-sa in arc-runners).
# The existing runner-keda-manage Role only covers KEDA CRDs; the warm pool also
# needs Deployments (namespaced) + the cluster-scoped PriorityClass.
# -----------------------------------------------------------------------------

resource "kubernetes_role" "runner_warm_pool_manage" {
  metadata {
    name      = "runner-warm-pool-manage"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "daemonsets"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
}

resource "kubernetes_role_binding" "runner_warm_pool_manage" {
  metadata {
    name      = "runner-warm-pool-manage"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runner_warm_pool_manage.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

# PriorityClass is cluster-scoped → needs a ClusterRole, scoped by resourceNames
# to just the one PriorityClass this module manages (least privilege).
resource "kubernetes_cluster_role" "runner_priorityclass_manage" {
  metadata {
    name = "runner-agent-overprovision-priorityclass"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  # create cannot be name-restricted (the object doesn't exist yet); the
  # mutating verbs are pinned to this one PriorityClass.
  rule {
    api_groups = ["scheduling.k8s.io"]
    resources  = ["priorityclasses"]
    verbs      = ["get", "list", "watch", "create"]
  }
  rule {
    api_groups     = ["scheduling.k8s.io"]
    resources      = ["priorityclasses"]
    resource_names = ["adp-agent-overprovision"]
    verbs          = ["update", "patch", "delete"]
  }
}

resource "kubernetes_cluster_role_binding" "runner_priorityclass_manage" {
  metadata {
    name = "runner-agent-overprovision-priorityclass"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.runner_priorityclass_manage.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

locals {
  agent_warm_pool_yaml = <<-YAML
    apiVersion: scheduling.k8s.io/v1
    kind: PriorityClass
    metadata:
      name: adp-agent-overprovision
      labels:
        app.kubernetes.io/name: agent-warm-pool
        app.kubernetes.io/part-of: adp-agent-factory
        app.kubernetes.io/managed-by: terraform
    value: -1
    globalDefault: false
    description: "Negative priority for the agent warm-pool balloon; real agent jobs (priority 0) preempt it."
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: agent-warm-pool
      namespace: ${kubernetes_namespace.adp_agents.metadata[0].name}
      labels:
        app.kubernetes.io/name: agent-warm-pool
        app.kubernetes.io/part-of: adp-agent-factory
        app.kubernetes.io/managed-by: terraform
    spec:
      replicas: ${var.agent_warm_pool_replicas}
      selector:
        matchLabels:
          app.kubernetes.io/name: agent-warm-pool
      template:
        metadata:
          labels:
            app.kubernetes.io/name: agent-warm-pool
            app.kubernetes.io/part-of: adp-agent-factory
        spec:
          priorityClassName: adp-agent-overprovision
          # Evict instantly when a real agent preempts — no grace period needed
          # for a do-nothing placeholder.
          terminationGracePeriodSeconds: 0
          securityContext:
            runAsNonRoot: true
            runAsUser: 1001
            runAsGroup: 1001
            fsGroup: 1001
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: balloon
              image: ${local.agent_image}
              # Do nothing; just hold the node + keep the image warm. Exit cleanly
              # on SIGTERM so preemption is immediate.
              command: ["/bin/sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"]
              # MUST match the ScaledJob agent-worker requests (scaledjob.tf) so the
              # balloon reserves an agent-sized slot the real pod can take over.
              resources:
                requests:
                  cpu: "1"
                  memory: 4Gi
                  ephemeral-storage: 50Gi
                limits:
                  cpu: "1"
                  memory: 4Gi
                  ephemeral-storage: 50Gi
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop:
                    - ALL
  YAML
}

resource "null_resource" "agent_warm_pool" {
  triggers = {
    manifest_sha   = sha256(local.agent_warm_pool_yaml)
    replicas       = var.agent_warm_pool_replicas
    namespace      = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name   = var.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${var.eks_cluster_name} --region ${var.aws_region} >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.agent_warm_pool_yaml}
EOF
    CMD
  }

  # Best-effort destroy — see the keda_trigger_auth comment in scaledjob.tf.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "kubectl delete deployment agent-warm-pool -n ${self.triggers.namespace} --ignore-not-found && kubectl delete priorityclass adp-agent-overprovision --ignore-not-found || true"
  }

  # Namespace must exist; RBAC must let the runner SA create the Deployment +
  # cluster-scoped PriorityClass before kubectl apply runs.
  depends_on = [
    kubernetes_namespace.adp_agents,
    kubernetes_role_binding.runner_warm_pool_manage,
    kubernetes_cluster_role_binding.runner_priorityclass_manage,
  ]
}

# =============================================================================
# Image pre-pull DaemonSet — caches the agent image on EVERY node
# =============================================================================
# The warm pool removes the cold-node BOOT delay, but only its own node has the
# image cached. This DaemonSet seeds the ~650 MB agent image onto every node in
# the cluster (a minimal container running the agent image but just sleeping), so
# a summoned agent skips the ~30s pull regardless of which node it lands on —
# covering parallel agents beyond agent_warm_pool_replicas and agents scheduled
# onto existing/other nodes. Mirrors the proven embark1 `chat-agent-image-prepull`
# DaemonSet (modules/agent-factory/agent/k8s/image-prepull-daemonset.yaml).
#
# The DaemonSet places one pod per node, so every node — including newly
# provisioned ones during a scale-out burst — caches the image automatically.
# We intentionally DO NOT set karpenter.sh/do-not-disrupt: that annotation only
# blocks scale-IN, and on a per-node image-cache DaemonSet it vetoed Auto Mode
# consolidation on EVERY node, pinning the cluster at peak size 24/7 even when
# the agent queue was empty all night. Caching on a live node is unaffected;
# idle nodes we no longer need are now free to be reclaimed (a later burst
# re-pulls onto fresh nodes — a one-time pull per node). Toleration
# `operator: Exists` still lands the prepull on every node.
# =============================================================================

locals {
  agent_image_prepull_yaml = <<-YAML
    apiVersion: apps/v1
    kind: DaemonSet
    metadata:
      name: agent-image-prepull
      namespace: ${kubernetes_namespace.adp_agents.metadata[0].name}
      labels:
        app.kubernetes.io/name: agent-image-prepull
        app.kubernetes.io/part-of: adp-agent-factory
        app.kubernetes.io/component: prepull
        app.kubernetes.io/managed-by: terraform
    spec:
      selector:
        matchLabels:
          app.kubernetes.io/name: agent-image-prepull
      template:
        metadata:
          labels:
            app.kubernetes.io/name: agent-image-prepull
        spec:
          # Land on every node, tolerating system/control-plane taints.
          tolerations:
            - operator: Exists
          terminationGracePeriodSeconds: 5
          securityContext:
            runAsNonRoot: true
            runAsUser: 1001
            runAsGroup: 1001
            fsGroup: 1001
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: prepull
              image: ${local.agent_image}
              # Override the agent entrypoint: just keep the image cached, do nothing.
              command: ["/bin/sh", "-c", "while true; do sleep 3600; done"]
              resources:
                requests:
                  cpu: "10m"
                  memory: 32Mi
                limits:
                  cpu: "50m"
                  memory: 64Mi
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop:
                    - ALL
  YAML
}

resource "null_resource" "agent_image_prepull" {
  count = var.agent_image_prepull_enabled ? 1 : 0

  triggers = {
    manifest_sha   = sha256(local.agent_image_prepull_yaml)
    namespace      = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name   = var.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${var.eks_cluster_name} --region ${var.aws_region} >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.agent_image_prepull_yaml}
EOF
    CMD
  }

  # Best-effort destroy — see the keda_trigger_auth comment in scaledjob.tf.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "kubectl delete daemonset agent-image-prepull -n ${self.triggers.namespace} --ignore-not-found || true"
  }

  depends_on = [
    kubernetes_namespace.adp_agents,
    kubernetes_role_binding.runner_warm_pool_manage,
  ]
}
