# Standalone, opt-in subnet provisioning. Review/apply this saved plan before
# adding its outputs to the EKS cluster through the capacity-subnet runbook.
terraform {
  required_version = ">= 1.5"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
  }
}

variable "account_id" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "capacity_by_az" {
  type = map(object({
    cidr_block     = string
    route_table_id = string
  }))
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

data "aws_eks_cluster" "existing" { name = var.cluster_name }
data "aws_subnet" "existing" {
  for_each = toset(data.aws_eks_cluster.existing.vpc_config[0].subnet_ids)
  id       = each.value
}
data "aws_route_table" "private" {
  for_each       = var.capacity_by_az
  route_table_id = each.value.route_table_id
  lifecycle {
    postcondition {
      condition     = self.vpc_id == data.aws_eks_cluster.existing.vpc_config[0].vpc_id
      error_message = "The existing private route table must belong to the cluster VPC."
    }
    postcondition {
      condition = anytrue([
        for route in self.routes : route.cidr_block == "0.0.0.0/0" && try(length(route.nat_gateway_id) > 0, false)
      ]) && alltrue([for route in self.routes : !try(startswith(route.gateway_id, "igw-"), false)])
      error_message = "Capacity must reuse a NAT-routed private table without an internet-gateway route."
    }
  }
}

resource "aws_subnet" "capacity" {
  for_each                = var.capacity_by_az
  vpc_id                  = data.aws_eks_cluster.existing.vpc_config[0].vpc_id
  availability_zone       = each.key
  cidr_block              = each.value.cidr_block
  map_public_ip_on_launch = false
  tags = {
    Name      = "${var.cluster_name}-capacity-${each.key}"
    ManagedBy = "terraform-private-capacity"
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = contains([for subnet in data.aws_subnet.existing : subnet.availability_zone], each.key)
      error_message = "Add capacity only in an availability zone already used by the cluster."
    }
  }
  depends_on = [data.aws_route_table.private]
}

resource "aws_route_table_association" "capacity" {
  for_each       = var.capacity_by_az
  subnet_id      = aws_subnet.capacity[each.key].id
  route_table_id = data.aws_route_table.private[each.key].id
  lifecycle { prevent_destroy = true }
}

output "additional_private_subnet_ids_by_az" {
  value      = { for zone, subnet in aws_subnet.capacity : zone => subnet.id }
  depends_on = [aws_route_table_association.capacity]
}
