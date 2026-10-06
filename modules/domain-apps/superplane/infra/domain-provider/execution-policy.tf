locals {
  # Deliberate closed partition, verified below and by tests. Numeric membership
  # preserves each complete statement without splitting multi-resource grants.
  policy_shard_indexes = {
    network          = concat(range(2, 6), range(37, 45), [47])
    identity         = range(13, 23)
    lifecycle        = concat(range(6, 13), [23, 24, 25, 45, 46])
    state-validation = concat([0, 1], range(26, 37))
  }

  ec2_resources = "${local.ec2_prefix}:*/*"
  ec2_creates   = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "AllocateAddress", "CreateNatGateway", "CreateRouteTable", "CreateSecurityGroup", "CreateVpcEndpoint", "CreateLaunchTemplate", "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"]
  ec2_mutators  = ["ModifyVpcAttribute", "ModifySubnetAttribute", "AttachInternetGateway", "DetachInternetGateway", "DeleteInternetGateway", "ReleaseAddress", "DeleteNatGateway", "CreateRoute", "DeleteRoute", "ReplaceRoute", "AssociateRouteTable", "DisassociateRouteTable", "DeleteRouteTable", "DeleteSubnet", "DeleteVpc", "DeleteSecurityGroup", "RevokeSecurityGroupIngress", "RevokeSecurityGroupEgress", "DeleteVpcEndpoints", "ModifyVpcEndpoint", "CreateLaunchTemplateVersion", "DeleteLaunchTemplateVersions", "ModifyLaunchTemplate", "DeleteLaunchTemplate"]
  provider_reads = distinct(concat(local.child_reads, [
    "ec2:DescribeVpcAttribute", "ec2:DescribeNatGateways", "ec2:DescribeNetworkAcls", "ec2:DescribeVpcEndpoints",
    "ec2:DescribeSecurityGroupRules", "ec2:DescribeLaunchTemplates", "ec2:DescribeLaunchTemplateVersions",
    "ec2:DescribeImages", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeCapacityReservations",
    "eks:DescribeAddonVersions", "eks:DescribeAddonConfiguration", "eks:ListAccessPolicies",
  ]))
  state_key = "${var.environment}/modules/superplane-workspaces/v2/${var.org_id}/${var.workspace_id}/terraform.tfstate"
  execution_policy = {
    Version = "2012-10-17"
    Statement = jsondecode(var.execution == null ? "[]" : jsonencode(concat([
      { Effect = "Allow", Action = local.provider_reads, Resource = "*", Condition = { StringEquals = local.regional } },
      { Effect = "Allow", Action = ["servicequotas:GetServiceQuota"], Resource = "arn:aws:servicequotas:${var.aws_region}:${var.account_id}:ec2/*" },
      # New resources require owner tags atomically. Dependencies of multi-resource
      # creates separately require pre-existing owner tags in the following grant.
      { Effect = "Allow", Action = [for action in concat(local.ec2_creates, local.ec2_mutators) : "ec2:${action}"], Resource = local.ec2_resources, Condition = { StringEquals = merge(local.owned_resource, local.operation) } },
      { Effect = "Allow", Action = ["ec2:CreateTags"], Resource = local.ec2_resources, Condition = { StringEquals = merge(local.owned_request, local.operation, { "ec2:CreateAction" = local.ec2_creates }) } },
      { Effect = "Allow", Action = ["ec2:CreateTags"], Resource = local.ec2_resources, Condition = { StringEquals = merge(local.owned_resource, local.operation), StringEqualsIfExists = local.owned_request } },
      { Effect = "Allow", Action = ["ec2:DeleteTags"], Resource = local.ec2_resources, Condition = { StringEquals = merge(local.owned_resource, local.operation) } },
      # AWS does not support resource/name/VPC/subnet conditions on CreateCluster.
      # Request tags and secure flags restrict this grant. The sealed request,
      # maintained module and saved-plan review enforce the actual network/name.
      {
        Effect = "Allow", Action = ["eks:CreateCluster"], Resource = "*"
        Condition = {
          StringEquals             = merge(local.owned_request, local.operation, local.regional, { "eks:authenticationMode" = "API" })
          "ForAllValues:ArnEquals" = { "eks:encryptionConfigProviderKeyArns" = var.execution.kms_key_arn }
          Null                     = { "eks:encryptionConfigProviderKeyArns" = "false" }
          Bool                     = { "eks:bootstrapClusterCreatorAdminPermissions" = "false", "eks:endpointPrivateAccess" = "true" }
        }
      },
      {
        Effect   = "Allow", Action = ["eks:DescribeCluster", "eks:DeleteCluster", "eks:UpdateClusterConfig", "eks:UpdateClusterVersion", "eks:CreateNodegroup", "eks:CreateAddon", "eks:ListNodegroups", "eks:ListAddons", "eks:ListAccessEntries", "eks:DescribeUpdate", "eks:ListUpdates", "eks:TagResource", "eks:UntagResource"]
        Resource = local.cluster_arn, Condition = { StringEquals = local.operation }
      },
      {
        Effect   = "Allow", Action = ["eks:DescribeNodegroup", "eks:UpdateNodegroupConfig", "eks:UpdateNodegroupVersion", "eks:DeleteNodegroup", "eks:TagResource", "eks:UntagResource"]
        Resource = "${local.eks_prefix}:nodegroup/${local.workspace_name}/${local.workspace_name}-default/*", Condition = { StringEquals = local.operation }
      },
      {
        Effect   = "Allow", Action = ["eks:DescribeAddon", "eks:UpdateAddon", "eks:DeleteAddon", "eks:TagResource", "eks:UntagResource"]
        Resource = "${local.eks_prefix}:addon/${local.workspace_name}/vpc-cni/*", Condition = { StringEquals = local.operation }
      },
      {
        Effect    = "Allow", Action = ["eks:CreateAccessEntry"], Resource = local.cluster_arn
        Condition = { StringEquals = merge(local.owned_request, local.operation, { "eks:principalArn" = sort(tolist(var.execution.actor_role_arns)), "eks:accessEntryType" = "STANDARD" }) }
      },
      {
        Effect    = "Allow", Action = ["eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies", "eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"]
        Resource  = [for arn in var.execution.actor_role_arns : "${local.eks_prefix}:access-entry/${local.workspace_name}/role/${var.account_id}/${basename(arn)}/*"]
        Condition = { StringEquals = local.operation }
      },
      {
        Effect    = "Allow", Action = ["eks:AssociateAccessPolicy"]
        Resource  = [for arn in var.execution.actor_role_arns : "${local.eks_prefix}:access-entry/${local.workspace_name}/role/${var.account_id}/${basename(arn)}/*"]
        Condition = { StringEquals = merge(local.operation, { "eks:policyArn" = ["arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy", "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy", "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"] }) }
      },
      {
        Effect    = "Allow", Action = ["iam:CreateRole"], Resource = local.child_roles
        Condition = { StringEquals = merge(local.owned_request, local.operation, { "iam:PermissionsBoundary" = aws_iam_policy.child_boundary.arn }) }
      },
      {
        Effect    = "Allow", Action = ["iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy"], Resource = local.child_roles
        Condition = { StringEquals = merge(local.operation, { "iam:PermissionsBoundary" = aws_iam_policy.child_boundary.arn }) }
      },
      {
        Effect    = "Allow", Action = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"], Resource = local.child_roles
        Condition = { StringEquals = merge(local.operation, { "iam:PolicyARN" = ["arn:aws:iam::aws:policy/AmazonEKSClusterPolicy", "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy", "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"], "iam:PermissionsBoundary" = aws_iam_policy.child_boundary.arn }) }
      },
      { Effect = "Allow", Action = ["iam:DeleteRole", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"], Resource = local.child_roles, Condition = { StringEquals = local.operation } },
      { Effect = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole"], Resource = concat(local.child_roles, sort(tolist(var.execution.actor_role_arns)), [aws_iam_role.provider.arn, "arn:aws:iam::${var.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"]) },
      { Effect = "Allow", Action = ["iam:SimulatePrincipalPolicy"], Resource = aws_iam_role.provider.arn },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = slice(local.child_roles, 0, 3), Condition = { StringEquals = merge(local.operation, { "iam:PassedToService" = "eks.amazonaws.com" }) } },
      { Effect = "Allow", Action = ["sts:AssumeRole"], Resource = sort(tolist(var.execution.actor_role_arns)), Condition = { StringEquals = local.operation } },
      {
        Effect    = "Allow", Action = ["iam:CreateOpenIDConnectProvider"]
        Resource  = "arn:aws:iam::${var.account_id}:oidc-provider/oidc.eks.${var.aws_region}.amazonaws.com/id/*"
        Condition = { StringEquals = merge(local.owned_request, local.operation) }
      },
      {
        Effect    = "Allow", Action = ["iam:GetOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider", "iam:UpdateOpenIDConnectProviderThumbprint", "iam:AddClientIDToOpenIDConnectProvider", "iam:RemoveClientIDFromOpenIDConnectProvider", "iam:TagOpenIDConnectProvider", "iam:UntagOpenIDConnectProvider"]
        Resource  = "arn:aws:iam::${var.account_id}:oidc-provider/oidc.eks.${var.aws_region}.amazonaws.com/id/*"
        Condition = { StringEquals = merge(local.owned_resource, local.operation), StringEqualsIfExists = local.owned_request }
      },
      { Effect = "Allow", Action = ["kms:DescribeKey", "kms:GetKeyPolicy", "kms:CreateGrant"], Resource = var.execution.kms_key_arn },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:AssociateKmsKey", "logs:ListTagsForResource", "logs:ListTagsLogGroup", "logs:TagResource", "logs:UntagResource", "logs:TagLogGroup", "logs:UntagLogGroup"], Resource = ["arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/eks/${local.workspace_name}/cluster", "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/eks/${local.workspace_name}/cluster:*"], Condition = { StringEquals = local.operation } },
      { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "*", Condition = { StringEquals = local.regional } },
      { Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:GetBucketLocation"], Resource = "arn:aws:s3:::${var.execution.state_bucket}" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::${var.execution.state_bucket}", Condition = { StringLike = { "s3:prefix" = [local.state_key, "${dirname(local.state_key)}/workspaces/*"] } } },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "arn:aws:s3:::${var.execution.state_bucket}/${local.state_key}", Condition = { StringEquals = local.operation } },
      { Effect = "Allow", Action = ["dynamodb:DescribeTable"], Resource = "arn:aws:dynamodb:${var.aws_region}:${var.account_id}:table/${var.execution.lock_table}" },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = "arn:aws:dynamodb:${var.aws_region}:${var.account_id}:table/${var.execution.lock_table}", Condition = { StringEquals = local.operation, "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["${var.execution.state_bucket}/${local.state_key}", "${var.execution.state_bucket}/${local.state_key}-md5"] } } },
      # Prevent boundary replacement/removal even if another policy is attached
      # later. No managed-policy writer is granted to the provider.
      { Effect = "Deny", Action = ["iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicyVersion"], Resource = "*" },
      { Effect = "Deny", Action = ["ec2:DeleteTags", "iam:UntagOpenIDConnectProvider", "eks:UntagResource"], Resource = "*", Condition = { "ForAnyValue:StringEquals" = { "aws:TagKeys" = ["OrgId", "WorkspaceId"] } } },
    ], local.validation_statements, local.ec2_create_statements)))
  }
  # Never combine all create actions with all resource kinds: request tags on
  # the new subnet must not also authorize its existing parent VPC. Each new
  # resource grant names ONLY the primary kind; dependencies use ResourceTag.
  ec2_create_kinds = {
    CreateVpc                     = "vpc"
    CreateSubnet                  = "subnet"
    CreateInternetGateway         = "internet-gateway"
    AllocateAddress               = "elastic-ip"
    CreateNatGateway              = "natgateway"
    CreateRouteTable              = "route-table"
    CreateSecurityGroup           = "security-group"
    CreateVpcEndpoint             = "vpc-endpoint"
    CreateLaunchTemplate          = "launch-template"
    AuthorizeSecurityGroupIngress = "security-group-rule"
    AuthorizeSecurityGroupEgress  = "security-group-rule"
  }
  ec2_create_statements = [for action, kind in local.ec2_create_kinds : {
    Effect    = "Allow", Action = ["ec2:${action}"], Resource = "${local.ec2_prefix}:${kind}/*"
    Condition = { StringEquals = merge(local.owned_request, local.operation, action == "CreateVpcEndpoint" ? { "ec2:VpceServiceName" = "com.amazonaws.${var.aws_region}.sts" } : {}) }
  }]
  # RunInstances is permission-checked even with DryRun. Only the Gateway's
  # non-delivered validator session receives it, for one reviewed profile, with
  # no IAM instance profile. Paid-operation sessions do not receive this grant.
  validation_statements = jsondecode(var.execution == null ? "[]" : jsonencode([
    {
      Effect    = "Allow", Action = ["ec2:RunInstances"]
      Resource  = ["arn:aws:ec2:${var.aws_region}::image/${var.execution.validation_image_id}", "${local.ec2_prefix}:subnet/${var.execution.validation_subnet_id}"]
      Condition = { StringEquals = { "aws:PrincipalTag/adp:agent_id" = "superplane-provider-validation" } }
    },
    {
      Effect    = "Allow", Action = ["ec2:RunInstances"]
      Resource  = [for id in var.execution.validation_security_group_ids : "${local.ec2_prefix}:security-group/${id}"]
      Condition = { StringEquals = { "aws:PrincipalTag/adp:agent_id" = "superplane-provider-validation" } }
    },
    {
      Effect    = "Allow", Action = ["ec2:RunInstances"]
      Resource  = "${local.ec2_prefix}:instance/*"
      Condition = { StringEquals = { "aws:PrincipalTag/adp:agent_id" = "superplane-provider-validation", "ec2:InstanceType" = var.execution.validation_instance_type } }
    },
    {
      Effect    = "Allow", Action = ["ec2:RunInstances"]
      Resource  = ["${local.ec2_prefix}:network-interface/*", "${local.ec2_prefix}:volume/*"]
      Condition = { StringEquals = { "aws:PrincipalTag/adp:agent_id" = "superplane-provider-validation" } }
    },
  ]))
}
