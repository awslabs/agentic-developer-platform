terraform {
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
}
variable "environment" { type = string }
variable "service_id" { type = string }
variable "agent_registry_table_name" { type = string }
variable "worker_role_arn" { type = string }
variable "worker_role_id" { type = string }
variable "repair_role_arn" { type = string }
variable "repair_role_id" { type = string }
variable "work_queue_url" { type = string }
locals {
  agent_id = var.service_id == "security" ? "l5-worker" : "l5-${replace(var.service_id, "_", "-")}-worker"
}
resource "aws_ssm_parameter" "queue" {
  name  = "/adp/${var.environment}/l5-services/${var.service_id}/work-queue-url"
  type  = "String"
  value = var.work_queue_url
}
resource "aws_ssm_parameter" "legacy_security_queue" {
  count = var.service_id == "security" ? 1 : 0
  name  = "/adp/${var.environment}/gateway/l5-work-queue-url"
  type  = "String"
  value = var.work_queue_url
}
resource "aws_dynamodb_table_item" "worker" {
  table_name = var.agent_registry_table_name
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id          = { S = local.agent_id }
    role_arn          = { S = var.worker_role_arn }
    iam_role_id       = { S = var.worker_role_id }
    agent_name        = { S = local.agent_id }
    org_id            = { S = "__platform__" }
    team_id           = { S = "__agents__" }
    owner             = { S = "platform" }
    scope             = { S = "internal" }
    credential_scopes = { SS = concat(["l5:credential-session"], var.service_id == "devops" ? ["l5:knowledge-publication"] : []) }
    status            = { S = "active" }
  })
}

resource "aws_dynamodb_table_item" "repair" {
  table_name = var.agent_registry_table_name
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id          = { S = replace(local.agent_id, "-worker", "-repair") }
    role_arn          = { S = var.repair_role_arn }
    iam_role_id       = { S = var.repair_role_id }
    agent_name        = { S = replace(local.agent_id, "-worker", "-repair") }
    org_id            = { S = "__platform__" }
    team_id           = { S = "__agents__" }
    owner             = { S = "platform" }
    scope             = { S = "internal" }
    credential_scopes = { SS = concat(["l5:contribution-repair"], contains(["well_architected", "transform", "security"], var.service_id) ? ["l5:schedule-admission"] : []) }
    status            = { S = "active" }
  })
}
