output "table_name" {
  value = aws_dynamodb_table.authority.name
}

output "gateway_policy_json" {
  value       = jsonencode(local.gateway_policy)
  description = "Minimum identity policy; protected table resource denials also apply. No attachment is made by this module."
}
