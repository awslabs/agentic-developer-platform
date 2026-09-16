# Only the production WAF's neighbouring resources are fixtures. There is no
# backend, and every run in waf.tftest.hcl uses the mocked AWS provider.
terraform {
  required_version = ">= 1.14"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

locals {
  name_prefix = "adp-test"
}

variable "gitlab_webhook_enabled" {
  type    = bool
  default = false
}

resource "aws_kms_key" "cloudwatch" {}

resource "aws_api_gateway_stage" "dev" {
  deployment_id = "deployment"
  rest_api_id   = "api123"
  stage_name    = "test"
}
