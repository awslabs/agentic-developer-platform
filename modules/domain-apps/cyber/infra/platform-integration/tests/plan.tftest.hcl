mock_provider "aws" {
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock-boundary" }
  }
}
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

run "archive_first_isolated_browser_release" {
  # Both providers are mocked. Apply resolves generated ARNs for the IAM assertion.
  command = apply
  variables {
    common_crawl_partitions = ["CC-MAIN-2026-39", "CC-MAIN-2026-34"]
    session_owner_routing   = true
  }
  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.worker_common_crawl[0].policy).Statement :
      toset(statement.Resource) == toset([
        "arn:aws:s3:::commoncrawl/crawl-data/CC-MAIN-2026-39/segments/*/warc/*.warc.gz",
        "arn:aws:s3:::commoncrawl/crawl-data/CC-MAIN-2026-34/segments/*/warc/*.warc.gz"
      ]) && statement.Action == ["s3:GetObject"] if try(statement.Sid, "") == "ReadSelectedArchivePages"
    ])
    error_message = "Archived page reads must be read-only and scoped to the configured Common Crawl partitions."
  }
  assert {
    condition     = aws_athena_workgroup.common_crawl[0].configuration[0].enforce_workgroup_configuration && aws_athena_workgroup.common_crawl[0].configuration[0].bytes_scanned_cutoff_per_query == 1073741824
    error_message = "Historical lookups must enforce a per-query scan limit."
  }
  assert {
    condition     = aws_glue_catalog_table.common_crawl[0].parameters["projection.crawl.type"] == "injected" && output.worker_environment.CYBER_CC_CRAWLS == "CC-MAIN-2026-39,CC-MAIN-2026-34"
    error_message = "Every lookup must constrain explicitly configured crawl partitions."
  }
  assert {
    condition     = kubernetes_service.url_analysis_browser_broker.spec[0].session_affinity == "None" && kubernetes_deployment.url_analysis_browser_broker.spec[0].template[0].spec[0].container[0].readiness_probe[0].http_get[0].path == "/readyz"
    error_message = "New session admission must use available replicas instead of worker-IP affinity."
  }
}

run "native_browser_has_no_broker_dependency" {
  command = plan
  variables {
    browser_mode           = "native"
    browser_broker_enabled = false
  }
  assert {
    condition     = output.worker_environment.URL_ANALYSIS_BROWSER_MODE == "native" && !contains(keys(output.worker_environment), "URL_ANALYSIS_BROWSER_BROKER") && length(output.worker_egress) == 0
    error_message = "Native workers must not require the broker endpoint or network policy."
  }
  assert {
    condition     = kubernetes_deployment.url_analysis_browser_broker.spec[0].replicas == "0"
    error_message = "The drained legacy broker must scale to zero."
  }
  assert {
    condition = toset(output.worker_browser_permissions[0].Action) == toset([
      "bedrock-agentcore:StartBrowserSession", "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:StopBrowserSession", "bedrock-agentcore:ListBrowserSessions",
      "bedrock-agentcore:ConnectBrowserAutomationStream"
    ]) && output.worker_browser_permissions[0].Condition.StringEquals["aws:RequestedRegion"] == var.aws_region
    error_message = "Native permissions must grant only regional Browser lifecycle and CDP access."
  }
}

run "websearch_route_is_opt_in_and_preserves_url_tools" {
  command = plan
  variables {
    tools_endpoint         = "https://api.example/dev/tools/cyber"
    task_url_tools_enabled = true
    websearch_enabled      = true
  }
  assert {
    condition     = jsondecode(output.worker_environment.ADP_TASK_TOOL_ROUTES)["websearch.search"] == "https://api.example/dev/tools/websearch" && jsondecode(output.worker_environment.ADP_TASK_TOOL_ROUTES)["cyber.browser_start"] == "local:cyber_tools.task_browser.TaskBrowser" && jsondecode(output.worker_environment.ADP_TASK_TOOL_ROUTES)["cyber.common_crawl_scan"] == "https://api.example/dev/tools/cyber/common-crawl"
    error_message = "Search routing must not replace the browser or Common Crawl paths."
  }
}
