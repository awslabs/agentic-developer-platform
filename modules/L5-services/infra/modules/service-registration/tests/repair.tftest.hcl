mock_provider "aws" {}

variables {
  environment               = "dev"
  service_id                = "finops"
  agent_registry_table_name = "example-registry"
  worker_role_arn           = "arn:aws:iam::000000000000:role/example-finops-worker"
  worker_role_id            = "AROAEXAMPLEWORKER00000"
  repair_role_arn           = "arn:aws:iam::000000000000:role/example-finops-repair"
  repair_role_id            = "AROAEXAMPLEREPAIR00000"
  work_queue_url            = "https://sqs.us-east-1.amazonaws.com/000000000000/example-work"
}

run "wa_repair_has_schedule_admission_without_worker_credentials" {
  command = plan
  variables {
    service_id = "well_architected"
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_table_item.repair.item).agent_id.S == "l5-well-architected-repair" &&
      jsondecode(aws_dynamodb_table_item.repair.item).credential_scopes.SS == ["l5:contribution-repair", "l5:schedule-admission"] &&
      jsondecode(aws_dynamodb_table_item.worker.item).credential_scopes.SS == ["l5:credential-session"]
    )
    error_message = "Only WA repair may request schedule admission; provider credentials stay worker-only."
  }
}

run "distinct_contribution_only_repair_registry" {
  command = apply
  assert {
    condition = (
      jsondecode(aws_dynamodb_table_item.repair.item).agent_id.S == "l5-finops-repair" &&
      jsondecode(aws_dynamodb_table_item.repair.item).role_arn.S == var.repair_role_arn &&
      jsondecode(aws_dynamodb_table_item.repair.item).iam_role_id.S == var.repair_role_id &&
      jsondecode(aws_dynamodb_table_item.repair.item).credential_scopes.SS == ["l5:contribution-repair"] &&
      jsondecode(aws_dynamodb_table_item.worker.item).credential_scopes.SS == ["l5:credential-session"]
    )
    error_message = "Repair must retain a distinct exact role with no provider credential scope."
  }
}

run "transform_repair_has_schedule_admission_without_worker_credentials" {
  command = plan
  variables {
    service_id = "transform"
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_table_item.repair.item).agent_id.S == "l5-transform-repair" &&
      jsondecode(aws_dynamodb_table_item.repair.item).credential_scopes.SS == ["l5:contribution-repair", "l5:schedule-admission"] &&
      jsondecode(aws_dynamodb_table_item.worker.item).credential_scopes.SS == ["l5:credential-session"]
    )
    error_message = "Only Transform repair may request schedule admission; provider credentials stay worker-only."
  }
}

run "devops_worker_has_dedicated_knowledge_scope_without_repair_expansion" {
  command = plan
  variables {
    service_id = "devops"
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_table_item.worker.item).credential_scopes.SS == ["l5:credential-session", "l5:knowledge-publication"] &&
      jsondecode(aws_dynamodb_table_item.repair.item).credential_scopes.SS == ["l5:contribution-repair"]
    )
    error_message = "Native knowledge publication belongs only to the DevOps worker's separately authorized broker purpose."
  }
}
