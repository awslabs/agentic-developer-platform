output "lambda_role_arn" { value = try(aws_iam_role.service[0].arn, null) }
output "lambda_name" { value = try(aws_lambda_function.service[0].function_name, null) }
output "operations_table" { value = try(aws_dynamodb_table.operations[0].name, null) }
output "route_resource_id" { value = try(aws_api_gateway_resource.cyber[0].id, null) }
output "route_execution_arn" { value = var.enabled ? local.route_arn : null }
output "api_stage_deployment_required" {
  value       = var.enabled
  description = "The shared API owner must publish a deployment containing this route; this stack never modifies its stage."
}
output "websearch_route_execution_arn" { value = var.enabled ? "${var.api_execution_arn}/${var.stage_name}/POST/tools/websearch" : null }
