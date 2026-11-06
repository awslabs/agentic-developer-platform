mock_provider "aws" {
  mock_data "aws_availability_zones" { defaults = { names = ["us-east-1a", "us-east-1b"] } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
}
mock_provider "kubernetes" {}
mock_provider "tls" {}
mock_provider "time" {}

variables { environment = "dev" }

run "handoff_uses_a_separate_group_and_named_workloads" {
  command = plan
  variables { enable_tools_handoff_deployment = true }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/test-deployer" }
  }
  assert {
    condition = (
      length(kubernetes_role_v1.tools_handoff) == 2 &&
      kubernetes_config_map_v1.tools_adapter_runtime[0].metadata[0].name == "adp-tool-adapter-runtime" &&
      kubernetes_config_map_v1.tools_adapter_runtime[0].metadata[0].namespace == "adp-gateway" &&
      anytrue([for rule in kubernetes_role_v1.tools_handoff["gateway"].rule :
        rule.resources == toset(["configmaps"]) && rule.resource_names == toset(["adp-tool-adapter-runtime"]) && rule.verbs == toset(["get", "patch"])
      ]) &&
      anytrue([for rule in kubernetes_role_v1.tools_handoff["adapter"].rule :
        rule.resources == toset(["ingresses"]) && rule.resource_names == toset(["tool-adapters-private"]) && rule.verbs == toset(["get"])
      ]) &&
      alltrue([for binding in kubernetes_role_binding_v1.tools_handoff : binding.subject[0].name == "adp:tools-handoff-dev"]) &&
      alltrue([for role in kubernetes_role_v1.tools_handoff : alltrue([
        for rule in role.rule : length(rule.resource_names) > 0 && !contains(rule.verbs, "create") && !contains(rule.verbs, "delete")
      ])]) &&
      anytrue([for rule in kubernetes_role_v1.tools_release[0].rule :
        contains(coalesce(rule.resource_names, []), "bedrockgateway") && rule.verbs == toset(["get"])
      ])
    )
    error_message = "Gateway patches belong only to the separate handoff group; evaluation remains read-only."
  }
}

run "release_group_only_edits_precreated_tools_objects" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/test-deployer" }
  }
  assert {
    condition = (
      kubernetes_role_v1.tools_release[0].metadata[0].namespace == "adp-gateway" &&
      kubernetes_role_binding_v1.tools_release[0].metadata[0].namespace == "adp-gateway" &&
      kubernetes_role_binding_v1.tools_release[0].subject[0].kind == "Group" &&
      kubernetes_role_binding_v1.tools_release[0].subject[0].name == "adp:tools-release-dev" &&
      kubernetes_role_binding_v1.tools_release[0].role_ref[0].kind == "Role"
    )
    error_message = "Only the selected tools release group is bound to the adp-gateway Role."
  }
  assert {
    condition     = length(null_resource.tools_release_namespace) == 1
    error_message = "A fresh install must ensure adp-gateway exists before the tools release Role is created."
  }
  assert {
    condition = (
      length(kubernetes_role_v1.tools_release[0].rule) == 8 &&
      anytrue([for rule in kubernetes_role_v1.tools_release[0].rule :
        contains(rule.resources, "deployments") && toset(rule.resource_names) == toset(["tools-facade"]) && contains(rule.verbs, "patch") && contains(rule.verbs, "watch")
      ]) &&
      anytrue([for rule in kubernetes_role_v1.tools_release[0].rule :
        contains(rule.resources, "serviceaccounts") && toset(rule.resource_names) == toset(["tools-facade", "tools-facade-reconciler"]) && contains(rule.verbs, "patch")
      ]) &&
      anytrue([for rule in kubernetes_role_v1.tools_release[0].rule :
        contains(rule.resources, "cronjobs") && toset(rule.resource_names) == toset(["tools-facade-reconciler"]) && contains(rule.verbs, "patch")
      ])
    )
    error_message = "Release may patch only the named pre-created workload objects."
  }
  assert {
    condition = alltrue([for rule in kubernetes_role_v1.tools_release[0].rule :
      !contains(rule.verbs, "create") && !contains(rule.verbs, "delete") &&
      !contains(rule.verbs, "*") && !contains(coalesce(rule.resource_names, []), "other-deployment") &&
      !contains(rule.resources, "secrets") && !contains(rule.resources, "roles") &&
      !contains(rule.resources, "rolebindings") && !contains(rule.resources, "pods/exec") &&
      !contains(rule.resources, "serviceaccounts/token") && !contains(rule.resources, "*") &&
      (length(coalesce(rule.resource_names, [])) > 0 ||
        (toset(rule.resources) == toset(["pods"]) && toset(rule.verbs) == toset(["list"])) ||
        (toset(rule.resources) == toset(["jobs"]) && toset(rule.verbs) == toset(["list"])) ||
      (toset(rule.resources) == toset(["replicasets"]) && toset(rule.verbs) == toset(["get"])))
    ])
    error_message = "Other workloads, secret/exec/token/RBAC access and resource creation must be denied."
  }
  assert {
    condition = alltrue([for rule in kubernetes_role_v1.tools_release[0].rule :
      !contains(coalesce(rule.resource_names, []), "bedrockgateway") ||
      (toset(rule.resources) == toset(["deployments"]) && toset(rule.verbs) == toset(["get"]))
    ])
    error_message = "Gateway evaluation may only read the named Deployment, never patch or update it."
  }
}

run "production_never_gets_direct_push_role" {
  command = plan
  variables { environment = "prod" }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/test-deployer" }
  }
  assert {
    condition     = length(kubernetes_role_v1.tools_release) == 0 && length(kubernetes_role_binding_v1.tools_release) == 0 && length(null_resource.tools_release_namespace) == 0
    error_message = "Production remains dispatch-only; no direct tools RBAC is installed."
  }
}

run "shared_provider_may_update_only_precreated_browser_objects" {
  command = plan
  variables {
    shared_tools_browser_namespace     = "fictional-agents"
    shared_tools_browser_resource_name = "fictional-shared-tools"
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/test-deployer" }
  }
  assert {
    condition = (
      kubernetes_role_v1.shared_tools_release[0].metadata[0].namespace == "fictional-agents" &&
      kubernetes_role_binding_v1.shared_tools_release[0].subject[0].name == "adp:shared-tools-release-dev" &&
      toset(flatten([for rule in kubernetes_role_v1.shared_tools_release[0].rule : rule.resources])) ==
      toset(["deployments", "serviceaccounts", "networkpolicies", "pods", "replicasets"]) &&
      alltrue([for rule in kubernetes_role_v1.shared_tools_release[0].rule :
        contains(rule.resources, "pods") ? (
          try(length(coalesce(rule.resource_names, [])), 0) == 0 && toset(rule.verbs) == toset(["get", "list"])
          ) : contains(rule.resources, "replicasets") ? (
          toset(rule.api_groups) == toset(["apps"]) &&
          try(length(coalesce(rule.resource_names, [])), 0) == 0 && toset(rule.verbs) == toset(["get"])
          ) : (
          toset(rule.resource_names) == toset(["fictional-shared-tools-browser"]) &&
          contains(rule.verbs, "get") && contains(rule.verbs, "update") && contains(rule.verbs, "patch") &&
          !contains(rule.verbs, "create") && !contains(rule.verbs, "delete") &&
          !contains(coalesce(rule.resource_names, []), "agent-warm-pool") &&
          !contains(coalesce(rule.resource_names, []), "agent-authority-worker-sa") &&
          !contains(coalesce(rule.resource_names, []), "default-deny-egress")
        )
      ])
    )
    error_message = "Only the approved Browser resources may be read or updated, never created or deleted."
  }
  assert {
    condition = (
      kubernetes_role_v1.tools_browser_observer[0].metadata[0].namespace == "fictional-agents" &&
      kubernetes_role_binding_v1.tools_browser_observer[0].subject[0].name == "adp:tools-release-dev" &&
      kubernetes_role_binding_v1.tools_browser_observer[0].role_ref[0].name == kubernetes_role_v1.tools_browser_observer[0].metadata[0].name &&
      length(kubernetes_role_v1.tools_browser_observer[0].rule) == 3 &&
      alltrue([for rule in kubernetes_role_v1.tools_browser_observer[0].rule :
        toset(rule.resources) == toset(["deployments"]) ? (
          toset(rule.resource_names) == toset(["fictional-shared-tools-browser"]) && toset(rule.verbs) == toset(["get"])
          ) : (
          (toset(rule.resources) == toset(["replicasets"]) && toset(rule.verbs) == toset(["get"])) ||
          (toset(rule.resources) == toset(["pods"]) && toset(rule.verbs) == toset(["list"]))
        )
      ])
    )
    error_message = "The tools group must observe Browser through a separate read-only Role, never the shared release writer Role."
  }
}
