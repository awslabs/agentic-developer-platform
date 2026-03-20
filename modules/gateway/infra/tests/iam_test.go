package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestIamModuleFilesExist verifies all required files exist in the IAM module
func TestIamModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	AssertModuleFilesExist(t, modulePath)
}

// TestIamModuleValidate validates the IAM module using terraform validate
func TestIamModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestIamModuleRequiredVariables verifies required variables are declared
func TestIamModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")

	requiredVars := []string{
		"environment",
		"name_prefix",
		"common_tags",
		"cluster_name",
		"pool_account_arns",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in IAM module", varName)
	}
}

// TestIamModuleOutputs verifies required outputs are declared
func TestIamModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")

	requiredOutputs := []string{
		"eks_cluster_role_arn",
		"eks_cluster_role_name",
		"eks_node_group_role_arn",
		"eks_node_group_role_name",
		"eks_node_group_instance_profile_name",
		"gateway_service_role_arn",
		"gateway_service_role_name",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in IAM module", outputName)
	}
}

// TestIamModuleResources verifies essential resources are declared
func TestIamModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")

	// Verify EKS cluster role exists
	assert.True(t, ResourceExists(t, modulePath, "aws_iam_role", "eks_cluster"),
		"EKS cluster IAM role should be declared")

	// Verify EKS node group role exists
	assert.True(t, ResourceExists(t, modulePath, "aws_iam_role", "eks_node_group"),
		"EKS node group IAM role should be declared")

	// Verify instance profile exists
	assert.True(t, ResourceExists(t, modulePath, "aws_iam_instance_profile", "eks_node_group"),
		"EKS node group instance profile should be declared")
}

// TestIamModuleTagging verifies tagging compliance
func TestIamModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for IAM-specific tags as per tagging strategy
	assert.Contains(t, content, "Service = \"iam\"",
		"IAM resources should have Service = iam tag")
	assert.Contains(t, content, "Purpose",
		"IAM resources should have Purpose tag")
}

// TestIamModuleNamingConvention verifies resource naming follows convention
func TestIamModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify EKS cluster role naming
	assert.Contains(t, content, "${var.name_prefix}-role-eks-cluster",
		"EKS cluster role should follow naming convention bedrockgw-{env}-role-eks-cluster")

	// Verify EKS node group role naming
	assert.Contains(t, content, "${var.name_prefix}-role-eks-node-group",
		"EKS node group role should follow naming convention")

	// Verify instance profile naming
	assert.Contains(t, content, "${var.name_prefix}-instance-profile-eks-nodes",
		"Instance profile should follow naming convention")
}

// TestIamModuleEKSClusterRoleConfiguration verifies EKS cluster role configuration
func TestIamModuleEKSClusterRoleConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify assume role policy for EKS service
	assert.Contains(t, content, "eks.amazonaws.com",
		"EKS cluster role should be assumable by EKS service")

	// Verify EKS cluster policy attachment
	assert.Contains(t, content, "AmazonEKSClusterPolicy",
		"EKS cluster role should have AmazonEKSClusterPolicy attached")
}

// TestIamModuleEKSNodeGroupRoleConfiguration verifies EKS node group role configuration
func TestIamModuleEKSNodeGroupRoleConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify assume role policy for EC2 service
	assert.Contains(t, content, "ec2.amazonaws.com",
		"EKS node group role should be assumable by EC2 service")

	// Verify required policy attachments
	requiredPolicies := []string{
		"AmazonEKSWorkerNodePolicy",
		"AmazonEKS_CNI_Policy",
		"AmazonEC2ContainerRegistryReadOnly",
	}

	for _, policy := range requiredPolicies {
		assert.Contains(t, content, policy,
			"EKS node group role should have %s attached", policy)
	}
}

// TestIamModuleGatewayServiceRole verifies gateway service role configuration
func TestIamModuleGatewayServiceRole(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify gateway service role exists
	assert.Contains(t, content, "gateway_service",
		"Gateway service IAM role should be declared")

	// Verify STS permissions
	assert.Contains(t, content, "sts:GetCallerIdentity",
		"Gateway service role should have STS permissions")

	// Verify cross-account assume role permissions
	assert.Contains(t, content, "sts:AssumeRole",
		"Gateway service role should be able to assume cross-account roles")
}

// TestIamModuleCrossAccountTrust verifies cross-account trust configuration
func TestIamModuleCrossAccountTrust(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify pool_account_arns variable is used
	assert.Contains(t, content, "var.pool_account_arns",
		"IAM module should use pool_account_arns for cross-account access")

	// Verify Bedrock pool role pattern
	assert.Contains(t, content, "BedrockGateway-Pool",
		"Cross-account assume role should target Bedrock pool roles")
}

// TestIamModuleCloudWatchLogsPermissions verifies CloudWatch logs permissions
func TestIamModuleCloudWatchLogsPermissions(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify CloudWatch logs permissions
	logsPermissions := []string{
		"logs:CreateLogGroup",
		"logs:CreateLogStream",
		"logs:PutLogEvents",
	}

	for _, permission := range logsPermissions {
		assert.Contains(t, content, permission,
			"Gateway service role should have %s permission", permission)
	}
}

// TestIamModuleECRPermissions verifies ECR permissions for node group
func TestIamModuleECRPermissions(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify ECR permissions for node group
	ecrPermissions := []string{
		"ecr:BatchCheckLayerAvailability",
		"ecr:BatchGetImage",
		"ecr:GetDownloadUrlForLayer",
		"ecr:GetAuthorizationToken",
	}

	for _, permission := range ecrPermissions {
		assert.Contains(t, content, permission,
			"EKS node group should have %s ECR permission", permission)
	}
}

// TestIamModuleVariableDefaults verifies sensible defaults for variables
func TestIamModuleVariableDefaults(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for default pool_account_arns
	assert.Contains(t, content, "default     = []",
		"pool_account_arns should default to empty list")
}

// TestIamModuleBedrockPoolRoleTemplates verifies Bedrock pool role templates
func TestIamModuleBedrockPoolRoleTemplates(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify trust policy template exists
	assert.Contains(t, content, "bedrock_pool_trust_policy",
		"IAM module should have Bedrock pool trust policy template")

	// Verify permissions policy template exists
	assert.Contains(t, content, "bedrock_pool_permissions_policy",
		"IAM module should have Bedrock pool permissions policy template")

	// Verify Bedrock permissions in template
	bedrockPermissions := []string{
		"bedrock:InvokeModel",
		"bedrock:InvokeModelWithResponseStream",
		"bedrock:ListInferenceProfiles",
	}

	for _, permission := range bedrockPermissions {
		assert.Contains(t, content, permission,
			"Bedrock pool permissions template should include %s", permission)
	}
}

// TestIamModuleExternalIdConfiguration verifies external ID configuration
func TestIamModuleExternalIdConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify external ID condition in trust policy
	assert.Contains(t, content, "sts:ExternalId",
		"Trust policy should include external ID condition")
}

// TestIamModulePolicyTemplateOutputs verifies policy template outputs
func TestIamModulePolicyTemplateOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	outputsPath := filepath.Join(modulePath, "outputs.tf")
	content := ReadTerraformFile(t, outputsPath)

	// Verify trust policy template output
	assert.Contains(t, content, "bedrock_pool_trust_policy_template",
		"IAM module should output Bedrock pool trust policy template")

	// Verify permissions policy template output
	assert.Contains(t, content, "bedrock_pool_permissions_policy_template",
		"IAM module should output Bedrock pool permissions policy template")
}

// TestIamModuleRoleArnOutputs verifies role ARN outputs
func TestIamModuleRoleArnOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	outputsPath := filepath.Join(modulePath, "outputs.tf")
	content := ReadTerraformFile(t, outputsPath)

	// Verify all role ARN outputs exist
	roleOutputs := []string{
		"eks_cluster_role_arn",
		"eks_node_group_role_arn",
		"gateway_service_role_arn",
	}

	for _, output := range roleOutputs {
		assert.Contains(t, content, output,
			"IAM module should output %s", output)
	}
}
