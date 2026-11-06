# Observation has a separate trust boundary from facade principal assertions.
# Omitting an external key provisions both sides of KMS authorization here;
# an IAM role policy alone cannot authorize use of the facade's signing key.
resource "aws_kms_key" "observation" {
  for_each                 = { for name, settings in local.observation : name => settings if settings.assertion_key_arn == null }
  description              = "ADP ${var.environment} core-to-adapter observation assertions"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_2048"
  deletion_window_in_days  = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "AccountKeyAdministration", Effect = "Allow",
      Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" },
      Action    = ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*", "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"],
      Resource  = "*"
    },
    {
      Sid       = "CoreObservationSigner", Effect = "Allow",
      Principal = { AWS = each.value.core_role_arn },
      Action    = ["kms:Sign", "kms:Verify"], Resource = "*",
      Condition = { StringEquals = { "kms:SigningAlgorithm" = "RSASSA_PSS_SHA_256" } }
    },
    {
      Sid       = "AdapterObservationVerifier", Effect = "Allow",
      Principal = { AWS = aws_iam_role.runtime.arn },
      Action    = "kms:Verify", Resource = "*",
      Condition = { StringEquals = { "kms:SigningAlgorithm" = "RSASSA_PSS_SHA_256" } }
    }
  ] })
  tags = { "adp:component" = "tool-adapters", "adp:purpose" = "observation" }
}

locals {
  observation_assertion_keys = {
    for name, settings in local.observation : name => (
      settings.assertion_key_arn == null ? aws_kms_key.observation[name].arn : settings.assertion_key_arn
    )
  }
}
