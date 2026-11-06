terraform {
  required_version = ">= 1.10.0"
  backend "s3" {}
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.0" }
  }
}

variable "aws_region" { type = string }
variable "aws_account_id" { type = string }
variable "environment" { type = string }
variable "worker_role_arn" { type = string }
variable "worker_role_name" { type = string }
variable "gateway_role_name" {
  type    = string
  default = null
  validation {
    condition     = var.gateway_role_name == null ? true : can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.gateway_role_name))
    error_message = "Select the existing Gateway IAM role name."
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
  default_tags { tags = { "adp:module" = "L5-services", "adp:environment" = var.environment } }
}
