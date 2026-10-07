mock_provider "kubernetes" {}

variables {
  kubeconfig_path      = "/dev/null"
  installation_id      = "0123456789abcdef01234567"
  namespaces           = ["superplane", "skypilot"]
  preflight_namespaces = ["sp-preflight-0123456789abcdef"]
}

run "only_default_nodeclass_read" {
  command = plan

  assert {
    condition = length([
      for rule in kubernetes_cluster_role_v1.namespaces.rule : rule
      if rule.api_groups == tolist(["eks.amazonaws.com"]) && rule.resources == tolist(["nodeclasses"]) && rule.resource_names == tolist(["default"]) && rule.verbs == tolist(["get"])
    ]) == 1
    error_message = "Only get on the exact default Auto Mode NodeClass is permitted."
  }

  assert {
    condition = alltrue([
      for rule in kubernetes_cluster_role_v1.namespaces.rule :
      rule.api_groups == tolist(["eks.amazonaws.com"]) || (rule.api_groups == tolist([""]) && rule.resources == tolist(["namespaces"]))
    ]) && length(kubernetes_cluster_role_v1.namespaces.rule) == 4
    error_message = "The authorizer must not grant additional cluster resource authority."
  }

  assert {
    condition = length([
      for rule in kubernetes_cluster_role_v1.namespaces.rule : rule
      if contains(rule.verbs, "delete") && rule.resource_names == tolist(["sp-preflight-0123456789abcdef"]) && rule.resources == tolist(["namespaces"])
    ]) == 1
    error_message = "Deletion remains restricted to the actual selected temporary Namespace."
  }
}
