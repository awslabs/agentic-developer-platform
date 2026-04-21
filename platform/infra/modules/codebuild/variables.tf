variable "name_prefix" {
  description = "Prefix for resource names (e.g. adp-dev)"
  type        = string
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state and CodeBuild source zips"
  type        = string
}

variable "common_tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default     = {}
}
