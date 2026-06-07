variable "name_prefix" {
  description = "Resource name prefix"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for the DB subnet group"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the DB subnet group"
  type        = list(string)
}

variable "fargate_sg_id" {
  description = "Security group ID of the Fargate service (allowed ingress)"
  type        = string
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Allocated storage in GB"
  type        = number
  default     = 20
}

variable "db_name" {
  description = "Name of the database to create"
  type        = string
  default     = "gbrain"
}
