terraform {
  required_version = ">= 1.5"

  backend "s3" {
    # Configured via -backend-config during terraform init
    # See environments/dev/modules/image-builder-backend.tfvars
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
