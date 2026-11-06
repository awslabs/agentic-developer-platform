terraform {
  required_version = ">= 1.10"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
  backend "s3" {}
}
provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
}
