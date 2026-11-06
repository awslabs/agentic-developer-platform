terraform {
  required_version = ">= 1.10.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
  default_tags { tags = { "adp:module" = "L5-services", "adp:environment" = var.environment } }
}
variable "aws_region" { type = string }
variable "aws_account_id" { type = string }
