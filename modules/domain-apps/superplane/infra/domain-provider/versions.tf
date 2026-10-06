terraform {
  required_version = "= 1.9.8"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.65.0" }
  }
}
provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
  default_tags { tags = local.owner_tags }
}
