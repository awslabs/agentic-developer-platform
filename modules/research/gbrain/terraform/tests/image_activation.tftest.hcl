mock_provider "aws" {}

run "refuse_unpublished_bootstrap_image" {
  command = plan
  module {
    source = "./modules/fargate"
  }
  variables {
    name_prefix        = "gbrain-test"
    vpc_id             = "vpc-0123456789abcdef0"
    subnet_ids         = ["subnet-0123456789abcdef0"]
    service_sg_id      = "sg-0123456789abcdef0"
    container_image    = "111122223333.dkr.ecr.us-east-1.amazonaws.com/gbrain@"
    db_endpoint        = "db.example.invalid:5432"
    db_credentials_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:db-test"
    mcp_token_arn      = "arn:aws:secretsmanager:us-east-1:111122223333:secret:mcp-test"
    s3_bucket_name     = "gbrain-test"
    log_group_name     = "/gbrain-test"
    task_role_arn      = "arn:aws:iam::111122223333:role/task-test"
    execution_role_arn = "arn:aws:iam::111122223333:role/exec-test"
  }
  expect_failures = [aws_ecs_task_definition.serve]
}

run "consume_exact_registry_digest" {
  command = plan
  module {
    source = "./modules/fargate"
  }
  variables {
    name_prefix        = "gbrain-test"
    vpc_id             = "vpc-0123456789abcdef0"
    subnet_ids         = ["subnet-0123456789abcdef0"]
    service_sg_id      = "sg-0123456789abcdef0"
    container_image    = "111122223333.dkr.ecr.us-east-1.amazonaws.com/gbrain@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    db_endpoint        = "db.example.invalid:5432"
    db_credentials_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:db-test"
    mcp_token_arn      = "arn:aws:secretsmanager:us-east-1:111122223333:secret:mcp-test"
    s3_bucket_name     = "gbrain-test"
    log_group_name     = "/gbrain-test"
    task_role_arn      = "arn:aws:iam::111122223333:role/task-test"
    execution_role_arn = "arn:aws:iam::111122223333:role/exec-test"
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.serve.container_definitions)[0].image == var.container_image
    error_message = "Task definition must preserve the verified digest exactly."
  }
}

run "bootstrap_build_without_an_image_or_ecs_activation" {
  command = plan
  plan_options {
    target = [module.storage, module.build]
  }
  variables {
    owner_email        = "test@example.invalid"
    state_bucket       = "test-state-bucket"
    vpc_id             = "vpc-0123456789abcdef0"
    private_subnet_ids = ["subnet-0123456789abcdef0"]
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "111122223333" }
  }
  assert {
    condition     = module.build.project_name == "adp-research-gbrain-build"
    error_message = "Bootstrap must make the build project available without an image."
  }
}
