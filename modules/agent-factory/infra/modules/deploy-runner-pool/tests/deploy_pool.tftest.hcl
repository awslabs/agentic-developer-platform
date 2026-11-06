mock_provider "kubernetes" {}
mock_provider "helm" {}
variables {
  github_org  = "example"
  github_repo = "repo"
}
run "deploy_pool_registers_its_own_label_with_the_shared_identity" {
  command = apply
  assert {
    condition     = helm_release.deploy.name == "arc-runner-deploy" && yamldecode(helm_release.deploy.values[0]).runnerScaleSetName == "arc-runner-deploy"
    error_message = "The pool must register the routing label deploy workflows target."
  }
  assert {
    condition     = jsondecode(jsonencode(yamldecode(helm_release.deploy.values[0]))).template.spec.serviceAccountName == "github-runner-sa"
    error_message = "Moving a deploy job here must not change its identity: same service account as the org pool."
  }
  assert {
    condition     = yamldecode(helm_release.deploy.values[0]).minRunners == 1 && yamldecode(helm_release.deploy.values[0]).maxRunners == 8
    error_message = "One warm runner, bounded burst."
  }
  assert {
    condition     = yamldecode(helm_release.deploy.values[0]).template.metadata.annotations["karpenter.sh/do-not-disrupt"] == "true"
    error_message = "A deploy in flight must not be evicted for node consolidation."
  }
  assert {
    condition     = yamldecode(helm_release.deploy.values[0]).githubConfigUrl == "https://github.com/example/repo" && yamldecode(helm_release.deploy.values[0]).githubConfigSecret == "github-arc-secret"
    error_message = "Registration reuses the org pool's GitHub App secret by name."
  }
  assert {
    condition     = !can(jsondecode(jsonencode(yamldecode(helm_release.deploy.values[0]))).template.spec.nodeSelector) && !can(jsondecode(jsonencode(yamldecode(helm_release.deploy.values[0]))).template.spec.tolerations)
    error_message = "Without the trusted NodePool inputs the pod template must carry no nodeSelector or tolerations."
  }
}
run "trusted_nodepool_placement_is_opt_in" {
  command = apply
  variables {
    node_selector = { "adp.aws/trust" = "deployment" }
    tolerations   = [{ key = "adp.aws/trust", operator = "Equal", value = "deployment", effect = "NoSchedule" }]
  }
  assert {
    condition     = jsondecode(jsonencode(yamldecode(helm_release.deploy.values[0]))).template.spec.nodeSelector["adp.aws/trust"] == "deployment"
    error_message = "The pool must be movable onto the trusted deployment NodePool without a new module."
  }
  assert {
    condition     = length(jsondecode(jsonencode(yamldecode(helm_release.deploy.values[0]))).template.spec.tolerations) == 1
    error_message = "The taint toleration must be rendered when supplied."
  }
}
run "ceiling_below_floor_is_refused" {
  command = plan
  variables {
    min_runners = 3
    max_runners = 2
  }
  expect_failures = [helm_release.deploy]
}
