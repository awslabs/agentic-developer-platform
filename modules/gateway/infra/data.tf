# Reference platform infrastructure outputs
# These are read from the platform Terraform state

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = var.platform_state_bucket
    key    = var.platform_state_key
    region = var.aws_region
  }
}

locals {
  vpc_id             = data.terraform_remote_state.platform.outputs.vpc_id
  private_subnet_ids = data.terraform_remote_state.platform.outputs.private_subnet_ids
  public_subnet_ids  = data.terraform_remote_state.platform.outputs.public_subnet_ids
  eks_cluster_name   = data.terraform_remote_state.platform.outputs.eks_cluster_name
  eks_oidc_issuer    = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
}
