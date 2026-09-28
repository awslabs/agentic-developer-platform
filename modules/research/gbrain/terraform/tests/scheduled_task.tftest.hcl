mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/test-role" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:111122223333:cluster/gbrain-test" }
  }
}

run "scheduler_uses_batch_task_without_server_override" {
  command = plan
  module { source = "./modules/scheduler" }
  variables {
    name_prefix = "gbrain-test"
    cluster_arn = "arn:aws:ecs:us-east-1:111122223333:cluster/gbrain-test"
    task_def    = "arn:aws:ecs:us-east-1:111122223333:task-definition/gbrain-test-dream:2"
    subnet_ids  = ["subnet-0123456789abcdef0"]
    sg_id       = "sg-0123456789abcdef0"
    role_arn    = "arn:aws:iam::111122223333:role/scheduler-test"
  }
  assert {
    condition = (
      aws_cloudwatch_event_target.dream_task.ecs_target[0].task_definition_arn == var.task_def &&
      aws_cloudwatch_event_target.dream_task.input == null &&
      aws_cloudwatch_event_rule.dream.schedule_expression == "cron(0 3 * * ? *)" &&
      aws_cloudwatch_event_rule.dream.state == "ENABLED"
    )
    error_message = "Keep the existing schedule and invoke the dedicated batch task without the obsolete server command override."
  }
}

run "scheduler_iam_permits_only_dedicated_revision" {
  command = apply
  variables {
    owner_email            = "test@example.invalid"
    state_bucket           = "test-state-bucket"
    vpc_id                 = "vpc-0123456789abcdef0"
    private_subnet_ids     = ["subnet-0123456789abcdef0"]
    container_image_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "111122223333" }
  }
  override_resource {
    target = module.fargate.aws_ecs_cluster.gbrain
    values = { arn = "arn:aws:ecs:us-east-1:111122223333:cluster/gbrain-test" }
  }
  override_resource {
    target = aws_iam_role.app
    values = { arn = "arn:aws:iam::111122223333:role/app-test" }
  }
  override_resource {
    target = aws_iam_role.task_execution
    values = { arn = "arn:aws:iam::111122223333:role/execution-test" }
  }
  override_resource {
    target = module.fargate.aws_ecs_task_definition.dream
    values = { arn = "arn:aws:ecs:us-east-1:111122223333:task-definition/gbrain-test-dream:2" }
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.scheduler_run_task.policy).Statement[0].Resource == "arn:aws:ecs:us-east-1:111122223333:task-definition/gbrain-test-dream:2"
    error_message = "RunTask permission must follow the exact dedicated dream revision."
  }
}
