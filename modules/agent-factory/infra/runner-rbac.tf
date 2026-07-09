# =============================================================================
# Runner RBAC — Namespace-scoped Roles (Issue #1204, synthesis #1200 Section 3)
# =============================================================================
# Replaces cluster-admin with per-namespace Roles that grant only the verbs and
# resources the runner actually uses. Each namespace gets a Role + RoleBinding
# for the runner service account.
#
# Existing `runner-keda-manage` Role in `adp-agents` namespace (in
# modules/agent-factory/webhook-ingress/infra/scaledjob-rbac.tf) is left as-is
# — it's already correct and scoped.
# =============================================================================

# -----------------------------------------------------------------------------
# adp-gateway: deploy lifecycle (manifests, rollouts, migrations, job cleanup)
# -----------------------------------------------------------------------------

resource "kubernetes_role" "runner_deploy_gateway" {
  metadata {
    name      = "adp-runner-deploy"
    namespace = "adp-gateway"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups = ["", "apps", "batch", "networking.k8s.io"]
    resources  = ["deployments", "configmaps", "secrets", "services", "ingresses", "serviceaccounts", "pods", "replicasets", "jobs"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods/exec", "pods/log"]
    verbs      = ["get", "list", "create"]
  }
}

resource "kubernetes_role_binding" "runner_deploy_gateway" {
  metadata {
    name      = "adp-runner-deploy"
    namespace = "adp-gateway"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runner_deploy_gateway.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

# -----------------------------------------------------------------------------
# adp-gateway-agents: KEDA ScaledJob + TriggerAuth + DaemonSet management
# -----------------------------------------------------------------------------

resource "kubernetes_role" "runner_keda_manage_gateway_agents" {
  metadata {
    name      = "adp-runner-keda-manage"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups = ["keda.sh"]
    resources  = ["scaledjobs", "scaledobjects", "triggerauthentications"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  rule {
    api_groups = ["", "apps"]
    resources  = ["configmaps", "serviceaccounts", "daemonsets"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
}

resource "kubernetes_role_binding" "runner_keda_manage_gateway_agents" {
  metadata {
    name      = "adp-runner-keda-manage"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runner_keda_manage_gateway_agents.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

# -----------------------------------------------------------------------------
# arc-systems: read-only for ARC health verification
# Only created when GitHub Apps are configured (ARC installs arc-systems ns)
# -----------------------------------------------------------------------------

resource "kubernetes_role" "runner_readonly_arc_systems" {
  count = var.enable_github_apps ? 1 : 0

  metadata {
    name      = "adp-runner-readonly"
    namespace = "arc-systems"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups = ["", "apps"]
    resources  = ["pods", "deployments", "replicasets", "events"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_role_binding" "runner_readonly_arc_systems" {
  count = var.enable_github_apps ? 1 : 0

  metadata {
    name      = "adp-runner-readonly"
    namespace = "arc-systems"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runner_readonly_arc_systems[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

# -----------------------------------------------------------------------------
# agent-context: deploy lifecycle (configmaps, jobs, pod logs)
# Only created when agent-context module is deployed (namespace exists)
# -----------------------------------------------------------------------------

resource "kubernetes_role" "runner_deploy_agent_context" {
  count = var.enable_agent_context_rbac ? 1 : 0

  metadata {
    name      = "adp-runner-deploy"
    namespace = "agent-context"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups = ["", "batch"]
    resources  = ["configmaps", "jobs", "pods", "pods/log"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
}

resource "kubernetes_role_binding" "runner_deploy_agent_context" {
  count = var.enable_agent_context_rbac ? 1 : 0

  metadata {
    name      = "adp-runner-deploy"
    namespace = "agent-context"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runner_deploy_agent_context[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

# -----------------------------------------------------------------------------
# keda: read-only for KEDA health verification
# -----------------------------------------------------------------------------

resource "kubernetes_role" "runner_readonly_keda" {
  metadata {
    name      = "adp-runner-readonly"
    namespace = "keda"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups = ["", "apps"]
    resources  = ["pods", "deployments", "services"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_role_binding" "runner_readonly_keda" {
  metadata {
    name      = "adp-runner-readonly"
    namespace = "keda"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runner_readonly_keda.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}

# -----------------------------------------------------------------------------
# ClusterRole: namespace management (only legitimately cluster-scoped operation)
# Restricted to specific named namespaces via resourceNames.
# -----------------------------------------------------------------------------

resource "kubernetes_cluster_role" "runner_namespace_manage" {
  metadata {
    name = "adp-runner-namespace-manage"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups     = [""]
    resources      = ["namespaces"]
    resource_names = ["arc-runners", "adp-gateway-agents", "adp-agents", "adp-gateway", "agent-context", "keda"]
    verbs          = ["get", "list", "create", "delete"]
  }
}

resource "kubernetes_cluster_role_binding" "runner_namespace_manage" {
  metadata {
    name = "adp-runner-namespace-manage"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.runner_namespace_manage.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}
