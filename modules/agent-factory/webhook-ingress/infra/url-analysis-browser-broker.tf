# The reasoning worker executes generated code, so it must never hold browser
# credentials. This broker is a separate pod and role whose only API performs a
# complete guarded capture; no raw session, CDP endpoint or InvokeBrowser action
# is exposed to callers.

locals {
  url_analysis_browser_actions = [
    "bedrock-agentcore:ConnectBrowserAutomationStream",
    "bedrock-agentcore:GetBrowserSession",
    "bedrock-agentcore:ListBrowserSessions",
    "bedrock-agentcore:StartBrowserSession",
    "bedrock-agentcore:StopBrowserSession",
  ]
}

# Independently deployable boundary for existing workers. The broader worker
# policy also denies these APIs, but updating that policy can pull the separate
# worker-authority migration into its dependency graph. Keep this deny owned by
# the browser rollout so adopting the broker does not require that migration.
resource "aws_iam_role_policy" "agent_scaledjob_browser_deny" {
  name = "deny-direct-agentcore-browser"
  role = aws_iam_role.agent_scaledjob.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DenyDirectAgentCoreBrowser"
      Effect   = "Deny"
      Action   = ["bedrock-agentcore:*"]
      Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "url_analysis_browser_broker_boundary" {
  name = "${local.name_prefix}-url-analysis-browser-broker-boundary"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "GuardedBrowserLifecycleOnly"
      Effect   = "Allow"
      Action   = local.url_analysis_browser_actions
      Resource = "*"
      Condition = {
        StringEquals = { "aws:RequestedRegion" = var.aws_region }
      }
    }]
  })
}

resource "aws_iam_role" "url_analysis_browser_broker" {
  name                 = "${local.name_prefix}-url-analysis-browser-broker-role"
  permissions_boundary = aws_iam_policy.url_analysis_browser_broker_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:url-analysis-browser-broker-sa"
          "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "url_analysis_browser_broker" {
  name = "guarded-browser-lifecycle"
  role = aws_iam_role.url_analysis_browser_broker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "GuardedBrowserLifecycleOnly"
      Effect   = "Allow"
      Action   = local.url_analysis_browser_actions
      Resource = "*"
      Condition = {
        StringEquals = { "aws:RequestedRegion" = var.aws_region }
      }
    }]
  })
}

resource "kubernetes_service_account" "url_analysis_browser_broker" {
  metadata {
    name      = "url-analysis-browser-broker-sa"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.url_analysis_browser_broker.arn
    }
    labels = {
      "app.kubernetes.io/name"       = "url-analysis-browser-broker"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

resource "kubernetes_deployment" "url_analysis_browser_broker" {
  metadata {
    name      = "url-analysis-browser-broker"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/name"       = "url-analysis-browser-broker"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    replicas = 2
    selector {
      match_labels = { "app.kubernetes.io/name" = "url-analysis-browser-broker" }
    }
    template {
      metadata {
        labels = {
          "app.kubernetes.io/name"      = "url-analysis-browser-broker"
          "app.kubernetes.io/part-of"   = "adp-agent-factory"
          "app.kubernetes.io/component" = "security-boundary"
        }
      }
      spec {
        service_account_name             = kubernetes_service_account.url_analysis_browser_broker.metadata[0].name
        termination_grace_period_seconds = 330
        security_context {
          run_as_non_root = true
          run_as_user     = 1001
          run_as_group    = 1001
          fs_group        = 1001
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name    = "browser-broker"
          image   = local.agent_image
          command = ["python3"]
          args    = ["/app/skills/url-analysis/browser_broker.py"]
          port {
            name           = "http"
            container_port = 8765
          }
          env {
            name  = "AWS_REGION"
            value = var.aws_region
          }
          env {
            name  = "AWS_DEFAULT_REGION"
            value = var.aws_region
          }
          resources {
            requests = { cpu = "250m", memory = "512Mi" }
            limits   = { cpu = "1", memory = "1Gi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities { drop = ["ALL"] }
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 8765
            }
            initial_delay_seconds = 10
            period_seconds        = 15
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 8765
            }
            initial_delay_seconds = 5
            period_seconds        = 10
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "tmp"
          empty_dir {}
        }
      }
    }
  }
}

resource "kubernetes_service" "url_analysis_browser_broker" {
  metadata {
    name      = "url-analysis-browser-broker"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  spec {
    selector = { "app.kubernetes.io/name" = "url-analysis-browser-broker" }
    port {
      name        = "http"
      port        = 8765
      target_port = "http"
    }
  }
}

resource "kubernetes_network_policy" "url_analysis_browser_broker" {
  metadata {
    name      = "url-analysis-browser-broker"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { "app.kubernetes.io/name" = "url-analysis-browser-broker" }
    }
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector {
          match_labels = { "app.kubernetes.io/name" = "agent-scaledjob" }
        }
      }
      ports {
        port     = 8765
        protocol = "TCP"
      }
    }
    egress {
      ports {
        port     = 53
        protocol = "UDP"
      }
      ports {
        port     = 53
        protocol = "TCP"
      }
    }
    egress {
      ports {
        protocol = "TCP"
      }
    }
  }
}
