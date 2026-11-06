# agent-context-deploy.yml (group adp:trusted-deployment) runs ensure-door-identity.py,
# which mirrors the agent authority's control verification keys into agent-context so
# the Knowledge Door can verify gateway-issued identities. That read is the only
# cross-namespace access it needs. Applied live on dev 2026-10-15 for the tools flow
# door chain (#7870); declared here with the namespace it reads from. Adopt:
#   terraform import kubernetes_role.door_identity_keys_read adp-agents/door-identity-keys-read
#   terraform import kubernetes_role_binding.door_identity_keys_read adp-agents/door-identity-keys-read

resource "kubernetes_role" "door_identity_keys_read" {
  metadata {
    name      = "door-identity-keys-read"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-authority"
    }
  }

  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["adp-control-verification-keys"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding" "door_identity_keys_read" {
  metadata {
    name      = "door-identity-keys-read"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-authority"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.door_identity_keys_read.metadata[0].name
  }

  subject {
    kind      = "Group"
    name      = "adp:trusted-deployment"
    api_group = "rbac.authorization.k8s.io"
  }
}
