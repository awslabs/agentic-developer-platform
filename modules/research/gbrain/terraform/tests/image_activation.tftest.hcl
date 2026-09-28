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
  expect_failures = [aws_ecs_task_definition.serve, aws_ecs_task_definition.dream]
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

run "preserve_runtime_settings_during_image_delivery" {
  command = plan
  module {
    source = "./modules/fargate"
  }
  variables {
    name_prefix          = "gbrain-test"
    container_command    = ["exec gbrain serve --http --port 3000"]
    container_entrypoint = ["/bin/sh", "-c"]
    container_environment = [
      { name = "AWS_REGION", value = "us-east-1" },
      { name = "LITELLM_BASE_URL", value = "http://litellm.example.invalid" },
      { name = "PORT", value = "3000" }
    ]
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
  assert {
    condition = (
      jsonencode(jsondecode(aws_ecs_task_definition.serve.container_definitions)[0].command) == jsonencode(var.container_command) &&
      jsonencode(jsondecode(aws_ecs_task_definition.serve.container_definitions)[0].entryPoint) == jsonencode(var.container_entrypoint) &&
      jsonencode(jsondecode(aws_ecs_task_definition.serve.container_definitions)[0].environment) == jsonencode(var.container_environment) &&
      length(jsondecode(aws_ecs_task_definition.serve.container_definitions)[0].secrets) == 3
    )
    error_message = "Image updates must preserve explicit runtime settings and secret references."
  }
}

run "dream_is_a_separate_batch_task" {
  command = apply
  module { source = "./modules/fargate" }
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
  # ECS adds default fields when it normalizes a registered serve container.
  # Dream must remain derived from configuration rather than this provider state.
  override_resource {
    target = aws_ecs_task_definition.serve
    values = {
      container_definitions = "[{\"name\":\"gbrain\",\"image\":\"111122223333.dkr.ecr.us-east-1.amazonaws.com/gbrain@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"cpu\":0,\"mountPoints\":[],\"systemControls\":[],\"volumesFrom\":[],\"secrets\":[{\"name\":\"GBRAIN_DB_PASSWORD\",\"valueFrom\":\"arn:aws:secretsmanager:us-east-1:111122223333:secret:db-test:password::\"},{\"name\":\"GBRAIN_DB_USER\",\"valueFrom\":\"arn:aws:secretsmanager:us-east-1:111122223333:secret:db-test:username::\"}]}]"
    }
  }
  assert {
    condition = (
      aws_ecs_task_definition.dream.family == "gbrain-test-dream" &&
      !contains(keys(jsondecode(aws_ecs_task_definition.dream.container_definitions)[0]), "cpu") &&
      jsondecode(aws_ecs_task_definition.dream.container_definitions)[0].image == var.container_image &&
      !contains(keys(jsondecode(aws_ecs_task_definition.dream.container_definitions)[0]), "healthCheck") &&
      !contains(keys(jsondecode(aws_ecs_task_definition.dream.container_definitions)[0]), "portMappings") &&
      jsondecode(aws_ecs_task_definition.dream.container_definitions)[0].entryPoint[1] == "-c" &&
      endswith(trimspace(jsondecode(aws_ecs_task_definition.dream.container_definitions)[0].command[0]), "exec gbrain dream") &&
      length(jsondecode(aws_ecs_task_definition.dream.container_definitions)[0].secrets) == 2
    )
    error_message = "Dream must initialize and exit as a dedicated immutable batch task without HTTP health checks or MCP credentials."
  }
}
