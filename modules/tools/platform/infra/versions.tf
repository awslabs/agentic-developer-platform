terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.68.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
}

provider "aws" {
  alias               = "oauth_secrets"
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
}
