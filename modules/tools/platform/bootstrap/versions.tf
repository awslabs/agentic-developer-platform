terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "= 2.7.1"
    }
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
