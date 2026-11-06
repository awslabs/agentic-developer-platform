variable "atlassian_forge" {
  description = "A deployed Forge app registered for this ADP environment. Null keeps app setup unavailable. Contains no credentials."
  type = object({
    app_id         = string
    environment_id = string
    install_url    = string
  })
  default = null
  validation {
    condition = var.atlassian_forge == null ? true : (
      can(regex("^ari:cloud:ecosystem::app/[a-f0-9-]{36}$", var.atlassian_forge.app_id)) &&
      can(regex("^[a-f0-9-]{36}$", var.atlassian_forge.environment_id)) &&
      can(regex("^https://developer\\.atlassian\\.com/console/install/[A-Za-z0-9/_-]+$", var.atlassian_forge.install_url))
    )
    error_message = "Use the registered app ID, Forge environment UUID and Atlassian installation URL."
  }
}

resource "kubernetes_config_map_v1" "atlassian_forge" {
  count = var.atlassian_forge == null ? 0 : 1
  metadata {
    name      = "adp-atlassian-forge"
    namespace = "adp-gateway"
    labels = {
      "app.kubernetes.io/part-of" = "adp"
      "adp.environment"           = var.environment
    }
  }
  data = {
    ADP_ATLASSIAN_FORGE_APP_ID         = var.atlassian_forge.app_id
    ADP_ATLASSIAN_FORGE_ENVIRONMENT_ID = var.atlassian_forge.environment_id
    ADP_ATLASSIAN_FORGE_INSTALL_URL    = var.atlassian_forge.install_url
  }
}
