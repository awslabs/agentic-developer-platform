terraform {
  required_version = ">= 1.10"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
  backend "local" {}
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
  default_tags {
    tags = { "adp:purpose" = "devops-queue-fixture", "adp:fixture" = var.fixture_id }
  }
}
