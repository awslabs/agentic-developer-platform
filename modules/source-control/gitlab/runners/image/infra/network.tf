# Optional preparation for the private Packer builder. Job hosts use their own
# runner-stack security groups and never receive this profile or network group.
variable "builder_network" {
  description = "Reviewed EKS VPC and private SSM/ECR endpoint IDs for the disposable AMI builder. Null preserves identity-only installations."
  type = object({
    vpc_id                 = string
    interface_endpoint_ids = set(string)
  })
  default = null
  validation {
    condition = var.builder_network == null ? true : (
      can(regex("^vpc-[0-9a-f]{8,17}$", var.builder_network.vpc_id)) &&
      length(var.builder_network.interface_endpoint_ids) >= 4 &&
      alltrue([for id in var.builder_network.interface_endpoint_ids : can(regex("^vpce-[0-9a-f]{8,17}$", id))])
    )
    error_message = "Supply the exact build VPC and private SSM, SSMMessages, ECR API and ECR registry endpoint IDs."
  }
}

data "aws_vpc" "builder" {
  count = var.builder_network == null ? 0 : 1
  id    = var.builder_network.vpc_id
}

data "aws_vpc_endpoint" "builder" {
  for_each = var.builder_network == null ? toset([]) : var.builder_network.interface_endpoint_ids
  id       = each.key
}

locals {
  builder_required_services = toset([for name in ["ssm", "ssmmessages", "ecr.api", "ecr.dkr"] : "com.amazonaws.${var.aws_region}.${name}"])
  builder_allowed_services  = setunion(local.builder_required_services, toset(["com.amazonaws.${var.aws_region}.ec2messages"]))
  builder_eni_ids           = toset(flatten([for endpoint in data.aws_vpc_endpoint.builder : endpoint.network_interface_ids]))
}

data "aws_network_interface" "builder_endpoint" {
  for_each = local.builder_eni_ids
  id       = each.key
}

data "aws_prefix_list" "builder_s3" {
  count = var.builder_network == null ? 0 : 1
  name  = "com.amazonaws.${var.aws_region}.s3"
}

resource "aws_security_group" "image_builder" {
  count       = var.builder_network == null ? 0 : 1
  name        = local.name
  description = "Private GitLab AMI builder: SSM, ECR and regional S3 HTTPS only"
  vpc_id      = var.builder_network.vpc_id
  ingress     = []
  egress {
    description = "Exact private SSM and ECR endpoint interfaces"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = sort([for eni in data.aws_network_interface.builder_endpoint : "${eni.private_ip}/32"])
  }
  egress {
    description     = "Regional S3 packages and ECR image layers"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [data.aws_prefix_list.builder_s3[0].id]
  }
  tags = local.tags

  lifecycle {
    precondition {
      condition = (
        data.aws_vpc.builder[0].owner_id == var.account_id &&
        alltrue([for endpoint in data.aws_vpc_endpoint.builder :
          endpoint.vpc_id == var.builder_network.vpc_id && endpoint.vpc_endpoint_type == "Interface" &&
          endpoint.state == "available" && endpoint.private_dns_enabled &&
          contains(local.builder_allowed_services, endpoint.service_name)
        ]) &&
        length(setsubtract(local.builder_required_services, toset([for endpoint in data.aws_vpc_endpoint.builder : endpoint.service_name]))) == 0
      )
      error_message = "Builder endpoints must be available private SSM/ECR interfaces in the reviewed account, VPC and region."
    }
    precondition {
      condition = length(local.builder_eni_ids) >= 4 && alltrue([for eni in data.aws_network_interface.builder_endpoint :
        eni.vpc_id == var.builder_network.vpc_id && eni.owner_id == var.account_id
      ])
      error_message = "Resolve owned endpoint interfaces before granting builder egress."
    }
  }
}

output "builder_security_group_id" {
  value = try(aws_security_group.image_builder[0].id, null)
}
