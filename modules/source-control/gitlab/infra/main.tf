# =============================================================================
# GitLab CE Infrastructure
# =============================================================================
# Self-hosted GitLab CE 19.1.x on EC2 with internal ALB, private subnet,
# security groups, EBS storage, and Route53 DNS.
# Uses remote state to reference the shared platform VPC and networking.
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp"
      Environment = var.environment
      Module      = "gitlab"
      ManagedBy   = "terraform"
      Owner       = "platform-team"
      CostCenter  = "engineering"
    }
  }
}

# =============================================================================
# Shared Platform Remote State
# =============================================================================

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

data "aws_caller_identity" "current" {}

# =============================================================================
# Locals
# =============================================================================

locals {
  name_prefix     = "adp-${var.environment}-gitlab"
  vpc_id          = data.terraform_remote_state.platform.outputs.vpc_id
  vpc_cidr_block  = data.terraform_remote_state.platform.outputs.vpc_cidr_block
  private_subnets = data.terraform_remote_state.platform.outputs.private_subnet_ids

  common_tags = {
    Project     = "adp"
    Environment = var.environment
    Module      = "gitlab"
    ManagedBy   = "terraform"
    Owner       = "platform-team"
    CostCenter  = "engineering"
  }
}

# =============================================================================
# AMI Data Source — Ubuntu 22.04 LTS (Canonical official)
# =============================================================================

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}
