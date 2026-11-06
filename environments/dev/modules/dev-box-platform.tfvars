# Dev Box platform prerequisites (#7998): modules/user-services/dev-box/infra/platform
# in the platform dev account. The platform VPC has ECR and S3 endpoints only, so
# the module creates the SSM interface endpoints the private guests and the Packer
# build instance need, the EBS key, and the AMI CodeBuild project.
vpc_id                = "vpc-0d6115bead9301d25"
vpc_cidr_block        = "10.0.0.0/16"
endpoint_subnet_ids   = ["subnet-0860c744097c41a03", "subnet-03ae2ea2ebdf611bb"]
build_subnet_id       = "subnet-0860c744097c41a03"
create_ssm_endpoints  = true
image_builder_enabled = true
