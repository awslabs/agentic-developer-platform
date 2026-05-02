# =============================================================================
# Runner RBAC for KEDA CRDs in adp-agents namespace
# =============================================================================
# The null_resource.keda_{trigger_auth,scaledjob} entries in scaledjob.tf use
# `kubectl apply` local-exec to create/update/delete KEDA CRDs. kubectl runs
# as the runner pod's K8s service account (`github-runner-sa` in
# `arc-runners`), which by default has no access to KEDA CRDs in the
# `adp-agents` namespace.
#
# Without this Role + RoleBinding, destroy provisioners in scaledjob.tf fail
# mid-apply with:
#
#   Error from server (Forbidden): scaledjobs.keda.sh "agent-scaledjob"
#   is forbidden: User "system:serviceaccount:arc-runners:github-runner-sa"
#   cannot delete resource "scaledjobs" in API group "keda.sh" in the
#   namespace "adp-agents"
#
# Scoped to the adp-agents namespace only — the runner SA gets no KEDA
# access anywhere else.
# =============================================================================

resource "kubernetes_role" "runner_keda_manage" {
  metadata {
    name      = "runner-keda-manage"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

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
}

resource "kubernetes_role_binding" "runner_keda_manage" {
  metadata {
    name      = "runner-keda-manage"
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
    name      = kubernetes_role.runner_keda_manage.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}
