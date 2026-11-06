mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "tls" {}

variables {
  environment                      = "dev"
  name_prefix                      = "adp-dev"
  vpc_id                           = "vpc-00000000000000000"
  private_subnet_ids               = ["subnet-00000000000000001", "subnet-00000000000000002"]
  eks_security_group_id            = "sg-00000000000000000"
  eks_cluster_role_arn             = "arn:aws:iam::123456789012:role/adp-dev-role-eks-cluster"
  node_group_role_arn              = "arn:aws:iam::123456789012:role/adp-dev-role-eks-node-group"
  eks_public_access_cidrs          = ["10.0.0.0/8"]
  tools_agentcore_gateway_arn      = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  tools_internal_authorize_api_arn = "arn:aws:execute-api:us-east-1:123456789012:exampleapi/dev/POST/internal/v1/tools/authorize"
  tools_execution_table_arn        = "arn:aws:dynamodb:us-east-1:123456789012:table/adp-tools-dev-executions"
}

run "dedicated_invoke_only_role" {
  command = apply
  override_data {
    target = data.aws_region.current
    values = { region = "us-east-1" }
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.tools_reconciler_irsa.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub"] == "system:serviceaccount:adp-gateway:tools-facade-reconciler" &&
      jsondecode(aws_iam_role_policy.tools_reconciler[0].policy).Statement[0].Action == ["dynamodb:Query"] &&
      jsondecode(aws_iam_role_policy.tools_reconciler[0].policy).Statement[0].Resource == "${var.tools_execution_table_arn}/index/cleanup-due" &&
      jsondecode(aws_iam_role_policy.tools_reconciler[0].policy).Statement[1].Action == ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"] &&
      jsondecode(aws_iam_role_policy.tools_reconciler[0].policy).Statement[1].Resource == var.tools_execution_table_arn
    )
    error_message = "The deadline worker may query the due index and read, scan or update only the execution table."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.tools_facade_executions[0].policy).Statement[0].Action == ["dynamodb:ConditionCheckItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"] &&
      jsondecode(aws_iam_role_policy.tools_facade_executions[0].policy).Statement[0].Resource == var.tools_execution_table_arn &&
      aws_iam_role_policy.tools_facade_executions[0].role == aws_iam_role.tools_facade_irsa.id
    )
    error_message = "The façade may claim, read, scan and update only the execution table; historical cleanup requires the scan."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.tools_facade_authorize[0].policy).Statement[0].Action == ["execute-api:Invoke"] &&
      jsondecode(aws_iam_role_policy.tools_facade_authorize[0].policy).Statement[0].Resource == [var.tools_internal_authorize_api_arn, replace(var.tools_internal_authorize_api_arn, "/tools/authorize", "/tools/native-dispatch")] &&
      aws_iam_role_policy.tools_facade_authorize[0].role == aws_iam_role.tools_facade_irsa.id &&
      jsondecode(aws_iam_role_policy.tools_reconciler_native_cleanup[0].policy).Statement[0].Resource == replace(var.tools_internal_authorize_api_arn, "/tools/authorize", "/tools/native-cleanup") &&
      aws_iam_role_policy.tools_reconciler_native_cleanup[0].role == aws_iam_role.tools_reconciler_irsa.id
    )
    error_message = "Facade dispatch and reconciler cleanup must use separate exact internal routes and roles."
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_resource {
    target = aws_eks_cluster.main
    values = {
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE" }] }]
      certificate_authority = [{ data = "TFNUQVJUQ0VSVElGSUNBVEU=" }]
    }
  }
  override_data {
    target = data.tls_certificate.cluster
    values = { certificates = [{ sha1_fingerprint = "0123456789abcdef0123456789abcdef01234567" }] }
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.tools_facade_irsa.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub"] == "system:serviceaccount:adp-gateway:tools-facade" &&
      jsondecode(aws_iam_role_policy.tools_facade_invoke[0].policy).Statement[0].Action == ["bedrock-agentcore:InvokeGateway"] &&
      jsondecode(aws_iam_role_policy.tools_facade_invoke[0].policy).Statement[0].Resource == var.tools_agentcore_gateway_arn &&
      aws_iam_role_policy.tools_facade_invoke[0].role == aws_iam_role.tools_facade_irsa.id
    )
    error_message = "Only the façade service account may assume the invoke-only role for this AgentCore gateway."
  }
}
