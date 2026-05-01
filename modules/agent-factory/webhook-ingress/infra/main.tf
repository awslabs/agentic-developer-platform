# =============================================================================
# Webhook Ingress Infrastructure
# =============================================================================
# Foundational infrastructure for the hosted multi-tenant webhook ingress layer.
# Receives GitHub webhooks via HTTP API Gateway v2, validates with a stub Lambda,
# and queues work onto SQS FIFO for downstream agent processing.
#
# State key: dev/modules/webhook-ingress/terraform.tfstate
# =============================================================================

terraform {
  backend "s3" {
    # Configured via -backend-config during terraform init
    # key = "<env>/modules/webhook-ingress/terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp-webhook-ingress"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name_prefix = "adp-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id
}
