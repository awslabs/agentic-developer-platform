# =============================================================================
# CloudWatch Latency Dashboard Module - Variables
# =============================================================================
# Issue #144: Unified end-to-end latency dashboard
# CloudFront → ALB → Gateway Pod → Bedrock

variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
}

variable "name_prefix" {
  type        = string
  description = "Name prefix for resources (e.g. bedrockgw-dev)"
}

variable "common_tags" {
  type        = map(string)
  description = "Common tags to apply to all resources"
  default     = {}
}

variable "aws_region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
}

# CloudFront
variable "cloudfront_distribution_id" {
  type        = string
  description = "CloudFront distribution ID for latency metrics"
}

# ALB — this is dynamic (created by EKS Ingress controller), so it's optional.
# Format: app/<alb-name>/<alb-id> e.g. app/k8s-bedrockg-bedrockg-96a0136fc5/a04d4e1ab78a9b6c
variable "alb_arn_suffix" {
  type        = string
  description = "ALB ARN suffix for metrics (app/<name>/<id>). Empty string if ALB not yet created."
  default     = ""
}

# EKS / Container Insights
variable "eks_cluster_name" {
  type        = string
  description = "EKS cluster name for Container Insights log group"
}

variable "eks_namespace" {
  type        = string
  description = "Kubernetes namespace where the gateway pods run"
  default     = "bedrockgw"
}

variable "pod_deployment_name" {
  type        = string
  description = "Deployment name for pod health metrics (Container Insights PodName dimension)"
  default     = "bedrockgateway"
}
