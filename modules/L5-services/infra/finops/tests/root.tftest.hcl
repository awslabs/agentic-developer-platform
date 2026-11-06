mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "000000000000" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/example-l5-worker" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-worker" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:000000000000:rule/adp-dev-l5-repair" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:000000000000:adp-dev-l5-work"
      url = "https://sqs.us-east-1.amazonaws.com/000000000000/adp-dev-l5-work"
    }
  }
}

variables {
  aws_region                = "us-east-1"
  aws_account_id            = "000000000000"
  agent_registry_table_name = "example-registry"

  environment                = "dev"
  vpc_id                     = "vpc-00000000000000000"
  private_subnet_ids         = ["subnet-00000000000000000"]
  db_security_group_id       = "sg-00000000000000000"
  endpoint_security_group_id = "sg-00000000000000000"
  db_port                    = 5432
  permissions_boundary_arn   = "arn:aws:iam::000000000000:policy/example-l5-boundary"
  worker_artifact_bucket     = "example-l5-artifacts"
  worker_artifact_key        = "l5/security/example/workers.zip"
  worker_artifact_version    = "version-1"
  worker_artifact_sha256     = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  worker_factory             = "l5_services.runtime_finops:factory"
  executor_image_uri         = "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-dev-l5-finops-executor@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  executor_surface           = "{\"surface_revision\":\"unqualified-example\"}"
  gateway_api_arn            = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/*/internal/*"
  rds_connect_arn            = "arn:aws:rds-db:us-east-1:000000000000:dbuser:db-resource/bgadmin"
}


run "independent_root" {
  command = apply
  assert {
    condition     = output.worker_function_name == "adp-dev-l5-finops-worker" && output.repair_function_name == "adp-dev-l5-finops-repair"
    error_message = "Service roots must own separate compute identities, preserving Security names."
  }
  assert {
    condition     = aws_lambda_function.executor.package_type == "Image" && aws_lambda_function.executor.function_name == "adp-dev-l5-finops-executor"
    error_message = "FinOps requires a private, digest-pinned executor alongside the worker."
  }
}

run "wrong_factory_denied" {
  command = plan
  variables {
    worker_factory = "l5_services.runtime:factory"
  }
  expect_failures = [var.worker_factory]
}

run "foreign_executor_image_denied" {
  command = plan
  variables {
    executor_image_uri = "000000000000.dkr.ecr.us-east-1.amazonaws.com/foreign-executor@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [var.executor_image_uri]
}
