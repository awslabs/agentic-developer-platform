mock_provider "aws" {}

override_data {
  target = data.aws_iam_role.orchestrator
  values = {
    arn       = "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-orchestrator"
    id        = "adp-cli-uplift-eval-orchestrator"
    unique_id = "AROAEXAMPLEORCHESTRATOR"
    tags      = { Purpose = "cli-uplift-eval" }
  }
}

override_data {
  target = data.aws_iam_role.instance
  values = {
    arn       = "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-instance"
    id        = "adp-cli-uplift-eval-instance"
    unique_id = "AROAEXAMPLEINSTANCE"
    tags      = { Project = "adp", Environment = "dev", Purpose = "cli-uplift-eval", Issue = "5248" }
  }
}

variables {
  config = {
    account             = "000000000101"
    region              = "us-east-1"
    cluster             = "example-cluster"
    namespace           = "adp-gateway"
    orchestrator_role   = "adp-cli-uplift-eval-orchestrator"
    instance_role       = "adp-cli-uplift-eval-instance"
    registry_table      = "example-registry"
    internal_invoke_url = "https://example123.execute-api.us-east-1.amazonaws.com/dev"
    repository_id       = "17"
  }
}

run "retained_roles_receive_only_additive_grants" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role_policy.instance_retire.policy).Statement == [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = "arn:aws:execute-api:us-east-1:000000000101:example123/dev/POST/internal/v1/product-evaluation/retire"
    }]
    error_message = "The instance may invoke only the exact protected retirement route."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.orchestrator_retire.policy).Statement == [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = "arn:aws:execute-api:us-east-1:000000000101:example123/dev/POST/internal/v1/product-evaluation/retire"
    }]
    error_message = "Independent recovery may invoke only the exact protected retirement route."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.orchestrator_product_read.policy).Statement == [{
      Effect   = "Allow", Action = ["iam:GetRole"],
      Resource = "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-instance"
    }]
    error_message = "The orchestrator may identify only its retained instance role."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.orchestrator_runtime_proof.policy).Statement == [
      {
        Effect   = "Allow", Action = ["execute-api:Invoke"],
        Resource = "arn:aws:execute-api:us-east-1:000000000101:example123/dev/POST/internal/v1/runtime-proof/schema"
      },
      {
        Effect   = "Allow", Action = ["lambda:GetFunction"],
        Resource = "arn:aws:lambda:us-east-1:000000000101:function:adp-dev-orchestration-tick"
      }
    ]
    error_message = "Native observation must not add broad invoke, Lambda or Kubernetes execution authority."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.orchestrator_release_read[0].policy).Statement == [{
      Effect   = "Allow", Action = ["s3:GetObject"],
      Resource = "arn:aws:s3:::adp-dev-deployment-evidence-000000000101/deployment-evidence/v1/17/*/*/release/gateway-backend.json"
    }]
    error_message = "Only the selected repository's Gateway backend release records may be read."
  }
  assert {
    condition     = aws_eks_access_policy_association.gateway_view.access_scope[0].type == "namespace" && one(aws_eks_access_policy_association.gateway_view.access_scope[0].namespaces) == "adp-gateway" && length(aws_eks_access_policy_association.gateway_view.access_scope[0].namespaces) == 1 && aws_eks_access_policy_association.gateway_view.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"
    error_message = "Only the selected namespace receives EKS View."
  }
  assert {
    condition     = jsondecode(output.registration_item).role_arn.S == "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-instance" && jsondecode(output.registration_item).iam_role_id.S == "AROAEXAMPLEINSTANCE"
    error_message = "Conditional registration must bind the existing role and immutable role ID."
  }
  assert {
    condition     = jsondecode(output.recovery_registration_item).role_arn.S == "arn:aws:iam::000000000101:role/adp-cli-uplift-eval-orchestrator" && jsondecode(output.recovery_registration_item).iam_role_id.S == "AROAEXAMPLEORCHESTRATOR" && jsondecode(output.recovery_registration_item).agent_id.S == "00000000-0000-0000-0000-000008243001"
    error_message = "Independent recovery must register the exact existing orchestrator role."
  }
}
