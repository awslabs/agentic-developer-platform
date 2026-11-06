mock_provider "kubernetes" {}
override_data {
  target = data.kubernetes_resource.lifecycle_claim[0]
  values = { object = {
    metadata = { name = "lifecycle-state", namespace = "superplane", uid = "00000000-0000-4000-8000-000000000001", labels = { "adp.aws-e.io/installation" = "0123456789abcdef01234567" } }
    spec     = { storageClassName = "superplane-lifecycle-0123456789abcdef01234567", volumeName = "pvc-00000000-0000-4000-8000-000000000001" }
    status   = { phase = "Bound" }
  } }
}

variables {
  kubeconfig_path      = "/dev/null"
  installation_id      = "0123456789abcdef01234567"
  namespaces           = ["superplane", "skypilot"]
  preflight_namespaces = ["sp-preflight-0123456789abcdef"]
}
run "storage_is_optional" {
  command = plan
  assert {
    condition     = length(kubernetes_storage_class_v1.lifecycle) == 0 && length(data.kubernetes_resource.lifecycle_claim) == 0 && length(kubernetes_cluster_role_v1.namespaces.rule) == 4
    error_message = "Omitting native storage must preserve existing resources and permissions."
  }
}
run "retained_auto_mode_storage_with_named_read" {
  command = plan
  variables {
    lifecycle_storage = { provisioner = "ebs.csi.eks.amazonaws.com" }
  }
  assert {
    condition = (
      kubernetes_storage_class_v1.lifecycle[0].metadata[0].name == "superplane-lifecycle-0123456789abcdef01234567" &&
      kubernetes_storage_class_v1.lifecycle[0].reclaim_policy == "Retain" &&
      kubernetes_storage_class_v1.lifecycle[0].volume_binding_mode == "WaitForFirstConsumer" &&
      kubernetes_storage_class_v1.lifecycle[0].parameters == tomap({ encrypted = "true", type = "gp3" }) &&
      kubernetes_storage_class_v1.lifecycle[0].allowed_topologies[0].match_label_expressions[0].key == "eks.amazonaws.com/compute-type"
    )
    error_message = "Native storage must use encrypted retained EBS on the selected Auto Mode nodes."
  }
  assert {
    condition = length(kubernetes_cluster_role_v1.namespaces.rule) == 5 && length([
      for rule in kubernetes_cluster_role_v1.namespaces.rule : rule
      if rule.api_groups == tolist(["storage.k8s.io"]) && rule.resources == tolist(["storageclasses"]) && rule.resource_names == tolist(["superplane-lifecycle-0123456789abcdef01234567"]) && rule.verbs == tolist(["get"])
    ]) == 1
    error_message = "Initial native storage grants only get on the exact owned class, with no PV discovery."
  }
}
run "standard_ebs_has_no_auto_mode_topology" {
  command = plan
  variables {
    lifecycle_storage = { provisioner = "ebs.csi.aws.com" }
  }
  assert {
    condition     = length(kubernetes_storage_class_v1.lifecycle[0].allowed_topologies) == 0
    error_message = "Standard EBS must not require Auto Mode nodes."
  }
}
run "owned_bound_claim_grants_only_actual_pv_read" {
  command = plan
  variables {
    lifecycle_storage = {
      provisioner = "ebs.csi.eks.amazonaws.com"
      claim       = { namespace = "superplane", name = "lifecycle-state", uid = "00000000-0000-4000-8000-000000000001" }
    }
  }
  assert {
    condition = length(kubernetes_cluster_role_v1.namespaces.rule) == 6 && length([
      for rule in kubernetes_cluster_role_v1.namespaces.rule : rule
      if rule.api_groups == tolist([""]) && rule.resources == tolist(["persistentvolumes"]) && rule.resource_names == tolist(["pvc-00000000-0000-4000-8000-000000000001"]) && rule.verbs == tolist(["get"])
    ]) == 1
    error_message = "Only the bound volume of the selected owned PVC may be read."
  }
}
run "replaced_claim_refused" {
  command = plan
  variables {
    lifecycle_storage = {
      provisioner = "ebs.csi.eks.amazonaws.com"
      claim       = { namespace = "superplane", name = "lifecycle-state", uid = "00000000-0000-4000-8000-000000000002" }
    }
  }
  expect_failures = [data.kubernetes_resource.lifecycle_claim]
}
run "foreign_namespace_refused" {
  command = plan
  variables {
    lifecycle_storage = {
      provisioner = "ebs.csi.eks.amazonaws.com"
      claim       = { namespace = "another-app", name = "lifecycle-state", uid = "00000000-0000-4000-8000-000000000001" }
    }
  }
  expect_failures = [var.lifecycle_storage]
}
run "foreign_claim_owner_refused" {
  command = plan
  variables {
    lifecycle_storage = {
      provisioner = "ebs.csi.eks.amazonaws.com"
      claim       = { namespace = "superplane", name = "lifecycle-state", uid = "00000000-0000-4000-8000-000000000001" }
    }
  }
  override_data {
    target = data.kubernetes_resource.lifecycle_claim[0]
    values = { object = {
      metadata = { name = "lifecycle-state", namespace = "superplane", uid = "00000000-0000-4000-8000-000000000001", labels = { "adp.aws-e.io/installation" = "another-installation" } }
      spec     = { storageClassName = "superplane-lifecycle-0123456789abcdef01234567", volumeName = "pvc-00000000-0000-4000-8000-000000000001" }
      status   = { phase = "Bound" }
    } }
  }
  expect_failures = [data.kubernetes_resource.lifecycle_claim]
}
