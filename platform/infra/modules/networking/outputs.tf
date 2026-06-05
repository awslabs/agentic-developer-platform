# VPC Outputs
output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "vpc_cidr_block" {
  description = "CIDR block of the VPC"
  value       = aws_vpc.main.cidr_block
}

# Subnet Outputs
output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets"
  value       = aws_subnet.private[*].id
}

output "public_subnet_cidr_blocks" {
  description = "CIDR blocks of the public subnets"
  value       = aws_subnet.public[*].cidr_block
}

output "private_subnet_cidr_blocks" {
  description = "CIDR blocks of the private subnets"
  value       = aws_subnet.private[*].cidr_block
}

# Internet Gateway Output
output "internet_gateway_id" {
  description = "ID of the Internet Gateway"
  value       = aws_internet_gateway.main.id
}

# NAT Gateway Outputs
output "nat_gateway_ids" {
  description = "IDs of the NAT Gateways"
  value       = aws_nat_gateway.main[*].id
}

output "nat_gateway_public_ips" {
  description = "Public IPs of the NAT Gateways"
  value       = aws_eip.nat[*].public_ip
}

# Security Group Outputs
output "alb_security_group_id" {
  description = "Security Group ID for ALB"
  value       = aws_security_group.alb.id
}

output "eks_security_group_id" {
  description = "Security Group ID for EKS"
  value       = aws_security_group.eks.id
}

output "rds_security_group_id" {
  description = "Security Group ID for RDS"
  value       = aws_security_group.rds.id
}

output "redis_security_group_id" {
  description = "Security Group ID for Redis"
  value       = aws_security_group.redis.id
}

# Route Table Outputs
output "public_route_table_id" {
  description = "ID of the public route table"
  value       = aws_route_table.public.id
}

output "private_route_table_ids" {
  description = "IDs of the private route tables"
  value       = aws_route_table.private[*].id
}

# Availability Zone Outputs
output "availability_zones" {
  description = "List of availability zones used"
  value       = data.aws_availability_zones.available.names[*]
}

# VPC Endpoint Outputs
output "vpc_endpoint_sg_id" {
  description = "Security Group ID for VPC interface endpoints"
  value       = aws_security_group.vpc_endpoints.id
}

output "vpc_endpoint_s3_id" {
  description = "ID of the S3 Gateway VPC endpoint"
  value       = aws_vpc_endpoint.s3.id
}

output "vpc_endpoint_dynamodb_id" {
  description = "ID of the DynamoDB Gateway VPC endpoint"
  value       = aws_vpc_endpoint.dynamodb.id
}

output "vpc_endpoint_sts_id" {
  description = "ID of the STS Interface VPC endpoint"
  value       = aws_vpc_endpoint.sts.id
}

output "vpc_endpoint_secretsmanager_id" {
  description = "ID of the SecretsManager Interface VPC endpoint"
  value       = aws_vpc_endpoint.secretsmanager.id
}

output "vpc_endpoint_bedrock_runtime_id" {
  description = "ID of the Bedrock Runtime Interface VPC endpoint"
  value       = aws_vpc_endpoint.bedrock_runtime.id
}

output "vpc_endpoint_sqs_id" {
  description = "ID of the SQS Interface VPC endpoint"
  value       = aws_vpc_endpoint.sqs.id
}

output "vpc_endpoint_execute_api_id" {
  description = "ID of the Execute API Interface VPC endpoint"
  value       = aws_vpc_endpoint.execute_api.id
}
