# The gateway phase creates adp-gateway later in deploy-all.sh, so on a fresh
# install the namespace does not exist yet when this Role is planned. Ensure it
# here with the same idempotent check; the gateway phase still applies the Pod
# Security labels from modules/gateway/k8s/namespace.yaml.
resource "null_resource" "tools_release_namespace" {
  count = var.environment == "dev" ? 1 : 0

  triggers = {
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
      kubectl get namespace adp-gateway >/dev/null 2>&1 || kubectl create namespace adp-gateway
    CMD
  }

  # Keep first-install authorizer propagation upstream without making a scoped
  # RBAC plan reconcile every addon, IRSA role and workload in the EKS module.
  depends_on = [module.eks.access_entry_ready]
}

resource "kubernetes_role_v1" "tools_release" {
  count = var.environment == "dev" ? 1 : 0

  metadata {
    name      = "adp-tools-release"
    namespace = "adp-gateway"
  }

  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["tools-facade"]
    verbs          = ["get", "patch", "update", "watch"]
  }

  rule {
    api_groups     = [""]
    resources      = ["services"]
    resource_names = ["tools-facade"]
    verbs          = ["get", "patch", "update"]
  }

  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts"]
    resource_names = ["tools-facade", "tools-facade-reconciler"]
    verbs          = ["get", "patch", "update"]
  }

  rule {
    api_groups     = ["batch"]
    resources      = ["cronjobs"]
    resource_names = ["tools-facade-reconciler"]
    verbs          = ["get", "patch", "update"]
  }

  # Evaluation reads gateway runtime without receiving its deployment authority.
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["bedrockgateway"]
    verbs          = ["get"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get"]
  }
  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["list"]
  }

  depends_on = [module.eks.access_entry_ready, null_resource.tools_release_namespace]
}

resource "kubernetes_role_binding_v1" "tools_release" {
  count = var.environment == "dev" ? 1 : 0

  metadata {
    name      = "adp-tools-release"
    namespace = "adp-gateway"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.tools_release[0].metadata[0].name
  }

  subject {
    kind      = "Group"
    name      = "adp:tools-release-${var.environment}"
    api_group = "rbac.authorization.k8s.io"
  }
}
