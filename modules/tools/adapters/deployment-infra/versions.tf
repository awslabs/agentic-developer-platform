terraform {
  required_version = ">= 1.9, < 2.0"

  # The normal deployment launcher supplies the reviewed bucket, key, region
  # and locking configuration. No environment shares an implicit local state.
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.68.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.23, < 4.0"
    }
  }
}
