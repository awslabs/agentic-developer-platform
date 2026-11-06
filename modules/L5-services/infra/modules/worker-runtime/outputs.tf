output "work_queue_url" {
  value = aws_sqs_queue.work.url
}

output "work_queue_arn" {
  value = aws_sqs_queue.work.arn
}

output "worker_function_name" {
  value = aws_lambda_function.worker.function_name
}

output "worker_role_arn" {
  value = aws_iam_role.worker.arn
}

output "worker_role_id" {
  value = aws_iam_role.worker.unique_id
}

output "repair_role_arn" { value = aws_iam_role.repair.arn }
output "repair_role_id" { value = aws_iam_role.repair.unique_id }

output "repair_function_name" { value = aws_lambda_function.repair.function_name }
output "event_source_mapping_id" { value = aws_lambda_event_source_mapping.work.uuid }
output "worker_security_group_id" { value = aws_security_group.worker.id }
output "worker_role_name" { value = aws_iam_role.worker.name }
