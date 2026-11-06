# Exercise the actual reader root through the existing offline Terraform lane.
mock_provider "aws" {
  mock_data "aws_eks_cluster" {
    defaults = {
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/fixture" }] }]
      access_config         = [{ authentication_mode = "API" }]
      certificate_authority = [{ data = "fixture-ca" }]
    }
  }
}

run "reader_group_fits_eks_limit" {
  command = plan
  module { source = "../dedicated-readers" }
  variables {
    account_id         = "111122223333"
    region             = "us-east-1"
    environment        = "dev"
    installation_id    = "fixture"
    management_cluster = "management"
    namespace          = "superplane"
    workspace_clusters = {
      "22222222-2222-4222-8222-222222222222" = "workspace"
    }
  }
  assert {
    condition     = alltrue(flatten([for entry in aws_eks_access_entry.reader : [for group in entry.kubernetes_groups : length(group) <= 63]]))
    error_message = "EKS access-entry groups cannot exceed 63 characters, including the full workspace UUID."
  }
  assert {
    condition     = one(aws_eks_access_entry.reader["22222222-2222-4222-8222-222222222222"].kubernetes_groups) == "superplane:reader:22222222-2222-4222-8222-222222222222"
    error_message = "The group must match the native bootstrap delegation without truncating workspace identity."
  }
}
