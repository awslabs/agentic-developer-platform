mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/example-build" }
  }
  mock_resource "aws_iam_instance_profile" {
    defaults = { arn = "arn:aws:iam::000000000000:instance-profile/example-build" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_data "aws_prefix_list" {
    defaults = { id = "pl-0123456789abcdef0" }
  }
}

variables {
  account_id          = "000000000000"
  region              = "us-east-1"
  environment         = "example"
  vpc_id              = "vpc-0123456789abcdef0"
  vpc_cidr_block      = "10.0.0.0/16"
  endpoint_subnet_ids = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
  build_subnet_id     = "subnet-0123456789abcdef0"
  state_bucket        = "adp-terraform-state-000000000000"
}

run "endpoints_key_and_builder" {
  command = apply

  assert {
    condition     = length(aws_vpc_endpoint.ssm) == 3 && alltrue([for endpoint in aws_vpc_endpoint.ssm : endpoint.private_dns_enabled && endpoint.vpc_endpoint_type == "Interface"])
    error_message = "ssm, ssmmessages and ec2messages interface endpoints with private DNS are required for Session Manager without NAT."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.endpoints_https[0].cidr_ipv4 == var.vpc_cidr_block && aws_vpc_security_group_ingress_rule.endpoints_https[0].from_port == 443
    error_message = "The endpoints accept HTTPS from the platform VPC only."
  }
  assert {
    condition     = aws_kms_key.workspace.enable_key_rotation && aws_kms_alias.workspace.name == "alias/adp-example-dev-box-workspace"
    error_message = "The workspace key rotates and has a stable alias."
  }
  assert {
    condition     = aws_codebuild_project.image[0].name == "adp-example-dev-box-image" && aws_codebuild_project.image[0].source[0].buildspec == "modules/user-services/dev-box/image/buildspec.yml" && !aws_codebuild_project.image[0].environment[0].privileged_mode
    error_message = "The image project runs the module buildspec without Docker privileges."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.image_builder_ssm[0].referenced_security_group_id == aws_security_group.endpoints[0].id
    error_message = "The build instance reaches the SSM endpoints."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.image_builder_s3[0].prefix_list_id == "pl-0123456789abcdef0" && aws_vpc_security_group_egress_rule.image_builder_s3[0].from_port == 443 && aws_vpc_security_group_egress_rule.image_builder_s3[0].to_port == 443 && aws_vpc_security_group_egress_rule.image_builder_s3[0].ip_protocol == "tcp"
    error_message = "The build instance's only other egress is HTTPS to the S3 gateway endpoint prefix list (Crew bundle fetch)."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.image_builder_instance_inputs[0].policy).Statement : statement if statement.Sid == "ReadCrewBundleInputs" && statement.Action[0] == "s3:GetObject" && statement.Resource == "arn:aws:s3:::adp-terraform-state-000000000000/dev-box/image-inputs/*"]) == 1 && length(jsondecode(aws_iam_role_policy.image_builder_instance_inputs[0].policy).Statement) == 1
    error_message = "The build instance may read only the digest-addressed Crew bundle inputs."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.image_builder[0].policy).Statement : statement if statement.Sid == "PublishImageIdentity" && length(statement.Resource) == 7]) == 1
    error_message = "CodeBuild may publish only the five image identity parameters and the two Crew bundle parameters."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.image_builder[0].policy).Statement : statement if statement.Sid == "PublishCrewBundleInputs" && statement.Action[0] == "s3:PutObject" && statement.Resource == "arn:aws:s3:::adp-terraform-state-000000000000/dev-box/image-inputs/*"]) == 1
    error_message = "The bundle build may write only under the digest-addressed image-inputs prefix."
  }
  assert {
    condition     = aws_ssm_parameter.ssm_endpoint_security_group_id.value == aws_security_group.endpoints[0].id && aws_ssm_parameter.kms_key_arn.name == "/adp/example/dev-box/kms-key-arn"
    error_message = "The foundation stack reads the endpoint group and key from SSM."
  }
}

run "existing_endpoints_without_builder" {
  command = apply
  variables {
    create_ssm_endpoints                    = false
    existing_ssm_endpoint_security_group_id = "sg-22222222222222222"
    image_builder_enabled                   = false
  }

  assert {
    condition     = length(aws_vpc_endpoint.ssm) == 0 && aws_ssm_parameter.ssm_endpoint_security_group_id.value == "sg-22222222222222222"
    error_message = "An existing endpoint group is handed through unchanged."
  }
  assert {
    condition     = length(aws_codebuild_project.image) == 0 && aws_ssm_parameter.image_builder_project.value == " " && length(aws_vpc_security_group_egress_rule.image_builder_s3) == 0 && length(aws_iam_role_policy.image_builder_instance_inputs) == 0
    error_message = "The image builder is optional, including its S3 egress and bundle read grant."
  }
}
