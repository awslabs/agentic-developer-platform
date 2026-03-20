package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestEksModuleFilesExist verifies all required files exist in the EKS module
func TestEksModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	AssertModuleFilesExist(t, modulePath)

	// EKS module should also have user_data.tpl
	userDataPath := filepath.Join(modulePath, "user_data.tpl")
	assert.True(t, CheckFileExists(t, userDataPath),
		"user_data.tpl should exist in EKS module")
}

// TestEksModuleValidate validates the EKS module using terraform validate
func TestEksModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestEksModuleRequiredVariables verifies required variables are declared
func TestEksModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")

	requiredVars := []string{
		"environment",
		"name_prefix",
		"common_tags",
		"vpc_id",
		"private_subnet_ids",
		"eks_security_group_id",
		"cluster_version",
		"node_group_instance_types",
		"node_group_desired_size",
		"node_group_max_size",
		"node_group_min_size",
		"gateway_service_role_arn",
		"node_group_role_arn",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in EKS module", varName)
	}
}

// TestEksModuleOutputs verifies required outputs are declared
func TestEksModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")

	requiredOutputs := []string{
		"cluster_name",
		"cluster_arn",
		"cluster_endpoint",
		"cluster_version",
		"cluster_security_group_id",
		"cluster_ca_certificate",
		"cluster_oidc_issuer_url",
		"node_group_arn",
		"node_group_status",
		"launch_template_id",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in EKS module", outputName)
	}
}

// TestEksModuleResources verifies essential resources are declared
func TestEksModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")

	// Verify EKS cluster resource exists
	assert.True(t, ResourceExists(t, modulePath, "aws_eks_cluster", "main"),
		"EKS cluster resource should be declared")

	// Verify EKS node group exists
	assert.True(t, ResourceExists(t, modulePath, "aws_eks_node_group", "main"),
		"EKS node group resource should be declared")

	// Verify launch template exists
	assert.True(t, ResourceExists(t, modulePath, "aws_launch_template", "eks_nodes"),
		"EKS launch template should be declared")
}

// TestEksModuleTagging verifies tagging compliance
func TestEksModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for merge pattern
	assert.Contains(t, content, "merge(var.common_tags",
		"Module should merge common_tags with resource-specific tags")

	// Check for EKS-specific kubernetes tags
	assert.Contains(t, content, "kubernetes.io/cluster/",
		"EKS cluster should have Kubernetes cluster ownership tag")
}

// TestEksModuleNamingConvention verifies resource naming follows convention
func TestEksModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify EKS cluster naming pattern
	assert.Contains(t, content, "${var.name_prefix}-eks-cluster",
		"EKS cluster should follow naming convention bedrockgw-{env}-eks-cluster")

	// Verify node group naming pattern
	assert.Contains(t, content, "${var.name_prefix}-node-group",
		"EKS node group should follow naming convention")
}

// TestEksModuleClusterConfiguration verifies EKS cluster configuration
func TestEksModuleClusterConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify cluster logging is enabled
	assert.Contains(t, content, "enabled_cluster_log_types",
		"EKS cluster should have logging enabled")

	// Check for common log types
	logTypes := []string{"api", "audit", "authenticator", "controllerManager", "scheduler"}
	for _, logType := range logTypes {
		assert.Contains(t, content, logType,
			"EKS cluster should enable %s logging", logType)
	}

	// Verify private endpoint access
	assert.Contains(t, content, "endpoint_private_access = true",
		"EKS cluster should have private endpoint access enabled")
}

// TestEksModuleNodeGroupConfiguration verifies node group configuration
func TestEksModuleNodeGroupConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify scaling configuration
	assert.Contains(t, content, "scaling_config",
		"EKS node group should have scaling configuration")
	assert.Contains(t, content, "desired_size",
		"EKS node group should specify desired_size")
	assert.Contains(t, content, "max_size",
		"EKS node group should specify max_size")
	assert.Contains(t, content, "min_size",
		"EKS node group should specify min_size")

	// Verify update configuration
	assert.Contains(t, content, "update_config",
		"EKS node group should have update configuration")
}

// TestEksModuleAddons verifies EKS addons are configured
func TestEksModuleAddons(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify essential addons
	addons := []string{"vpc-cni", "kube-proxy", "coredns", "aws-ebs-csi-driver"}
	for _, addon := range addons {
		assert.Contains(t, content, addon,
			"EKS module should configure %s addon", addon)
	}
}

// TestEksModuleAutoMode verifies EKS Auto Mode configuration
func TestEksModuleAutoMode(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for compute_config (EKS Auto Mode)
	assert.Contains(t, content, "compute_config",
		"EKS cluster should have compute_config for Auto Mode")

	// Check for storage_config
	assert.Contains(t, content, "storage_config",
		"EKS cluster should have storage_config")

	// Check for kubernetes_network_config
	assert.Contains(t, content, "kubernetes_network_config",
		"EKS cluster should have kubernetes_network_config")
}

// TestEksModuleServiceAccount verifies Kubernetes service account configuration
func TestEksModuleServiceAccount(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify service account for gateway service
	assert.Contains(t, content, "kubernetes_service_account",
		"EKS module should configure Kubernetes service account")

	// Verify IRSA annotation
	assert.Contains(t, content, "eks.amazonaws.com/role-arn",
		"Service account should have IRSA role annotation")
}

// TestEksModuleVariableDefaults verifies sensible defaults for variables
func TestEksModuleVariableDefaults(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for default cluster version
	assert.Contains(t, content, "default     = \"1.31\"",
		"cluster_version should have a default value")

	// Check for default instance types
	assert.Contains(t, content, "t3.medium",
		"node_group_instance_types should have default instance type")

	// Check for default node group sizes
	assert.Contains(t, content, "default     = 2",
		"node_group_desired_size should have default value of 2")
}
