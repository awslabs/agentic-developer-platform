variable "environment" {
  description = "Deployment environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "owner_email" {
  description = "Email of the experiment owner (used for tagging)"
  type        = string
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state and adp-source.zip (used by CodeBuild)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID to deploy into (shared ADP VPC)"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for Fargate tasks and RDS"
  type        = list(string)
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC (for security group rules)"
  type        = string
  default     = "10.0.0.0/16"
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GB"
  type        = number
  default     = 20
}

variable "desired_count" {
  description = "Number of Fargate tasks to run"
  type        = number
  default     = 1
}

variable "cpu" {
  description = "Fargate task CPU units (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "memory" {
  description = "Fargate task memory in MB"
  type        = number
  default     = 2048
}

variable "container_image_digest" {
  description = "Verified OCI digest. Empty only for the storage/build bootstrap targets; ECS refuses activation without a digest."
  type        = string
  default     = ""
  validation {
    condition     = var.container_image_digest == "" || can(regex("^sha256:[0-9a-f]{64}$", var.container_image_digest))
    error_message = "container_image_digest must be an OCI sha256 digest."
  }
}

variable "endpoint_security_group_ids" {
  description = "Existing private AWS endpoint security groups that admit Gbrain task HTTPS"
  type        = set(string)
  default     = []
}
