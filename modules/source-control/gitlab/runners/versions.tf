terraform {
  required_version = ">= 1.9"

  backend "s3" {}
}

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.23"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.0"
    }
  }
}

provider "kubernetes" {
  host                   = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca_certificate = base64decode(data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", data.terraform_remote_state.platform.outputs.eks_cluster_name, "--region", var.aws_region]
  }
}

provider "helm" {
  kubernetes {
    host                   = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
    cluster_ca_certificate = base64decode(data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", data.terraform_remote_state.platform.outputs.eks_cluster_name, "--region", var.aws_region]
    }
  }
}

locals {
  release_lock_path = var.release_lock_file == "" ? "${path.module}/versions.lock.json" : var.release_lock_file
  release_lock      = jsondecode(file(local.release_lock_path))
}

data "external" "release_ready" {
  program = ["python3", "${path.module}/scripts/release_lock.py"]
  query = {
    region      = var.aws_region
    host_ami_id = var.host_ami_id
    job_image   = var.job_image_digest
    lock_path   = var.release_lock_file == "" ? "" : abspath(var.release_lock_file)
    lock_sha256 = var.release_lock_sha256
  }
}

resource "terraform_data" "release_ready" {
  input = data.external.release_ready.result.ready
  lifecycle {
    precondition {
      condition     = data.external.release_ready.result.ready == "true"
      error_message = "The reviewed release lock, exact private AMI, host and container artifacts are required before activation."
    }
  }
}
