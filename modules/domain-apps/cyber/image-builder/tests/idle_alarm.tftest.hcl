mock_provider "aws" {
  mock_data "aws_vpc" { defaults = { id = "vpc-0123456789abcdef0" } }
  mock_data "aws_subnets" { defaults = { ids = ["subnet-0123456789abcdef0"] } }
}
override_resource {
  target = aws_s3_bucket.cape_assets
  values = { id = "adp-dev-cape-assets", arn = "arn:aws:s3:::adp-dev-cape-assets" }
}
override_resource {
  target = aws_s3_bucket_public_access_block.cape_assets
  values = { id = "adp-dev-cape-assets" }
}
override_resource {
  target = aws_s3_bucket_versioning.cape_assets
  values = { id = "adp-dev-cape-assets" }
}
override_resource {
  target = aws_s3_bucket_server_side_encryption_configuration.cape_assets
  values = { id = "adp-dev-cape-assets" }
}
override_resource {
  target = aws_s3_bucket_lifecycle_configuration.cape_assets
  values = { id = "adp-dev-cape-assets" }
}
variables {
  account_id          = "123456789012"
  build_host_enabled  = true
  builder_name_suffix = "ci"
  builder_ami_id      = "ami-0123456789abcdef0"
  idle_period_seconds = 21600
}
run "new_builder_needs_observed_idle_cpu" {
  command = plan
  assert {
    condition     = aws_cloudwatch_metric_alarm.builder_idle[0].treat_missing_data == "missing"
    error_message = "Absent CPU history must never terminate a newly launched builder."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.builder_idle[0].evaluation_periods * aws_cloudwatch_metric_alarm.builder_idle[0].period == 21600
    error_message = "The CI idle cutoff must require six hours of observed samples."
  }
}
