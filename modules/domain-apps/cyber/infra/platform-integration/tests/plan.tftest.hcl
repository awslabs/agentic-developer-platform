mock_provider "aws" {}
mock_provider "kubernetes" {}

variables {
  name_prefix       = "adp-dev"
  aws_region        = "us-east-1"
  environment       = "dev"
  account_id        = "123456789012"
  namespace         = "adp-agents"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/test"
  oidc_issuer       = "https://oidc.eks.us-east-1.amazonaws.com/id/test"
  worker_role_name  = "adp-dev-agent-scaledjob-role"
}

run "preserve_existing_deployment_contract" {
  command = plan
  assert {
    condition     = kubernetes_deployment.url_analysis_browser_broker.spec[0].template[0].spec[0].container[0].image == "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:7cd991c4b1498295bfa331da8deb367d4a02bba0e12b0075d73b61c7f9ced08f"
    error_message = "Reorganising source must preserve the recorded broker image."
  }
  assert {
    condition     = aws_iam_role.url_analysis_browser_broker.name == "adp-dev-url-analysis-browser-broker-role" && kubernetes_service.url_analysis_browser_broker.metadata[0].name == "url-analysis-browser-broker" && kubernetes_service.url_analysis_browser_broker.spec[0].session_affinity == "ClientIP"
    error_message = "The broker identity and session-affine service must remain stable."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.agent_scaledjob_browser_deny.policy).Statement[0].Effect == "Deny" && jsondecode(aws_iam_role_policy.agent_scaledjob_browser_deny.policy).Statement[0].Action[0] == "bedrock-agentcore:*"
    error_message = "The reasoning worker must continue to be denied direct browser access."
  }
  assert {
    condition     = output.worker_environment.URL_ANALYSIS_EVIDENCE_BUCKET == "adp-dev-url-analysis-evidence-v2-123456789012" && output.worker_egress[0].port == 8765
    error_message = "The worker's evidence and network integration must survive the move."
  }
}

run "independent_browser_release" {
  command = plan
  variables {
    broker_image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-cyber-browser@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    namespace    = "custom-agents"
  }
  assert {
    condition     = kubernetes_deployment.url_analysis_browser_broker.spec[0].template[0].spec[0].container[0].image == var.broker_image && kubernetes_service.url_analysis_browser_broker.metadata[0].namespace == "custom-agents"
    error_message = "The cyber broker must accept an independent image and platform namespace."
  }
  assert {
    condition     = jsondecode(aws_iam_role.url_analysis_browser_broker.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/test:sub"] == "system:serviceaccount:custom-agents:url-analysis-browser-broker-sa"
    error_message = "The IRSA trust must follow the configured namespace."
  }
}
