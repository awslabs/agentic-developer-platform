package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestNetworkingModuleFilesExist verifies all required files exist in the networking module
func TestNetworkingModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	AssertModuleFilesExist(t, modulePath)
}

// TestNetworkingModuleValidate validates the networking module using terraform validate
func TestNetworkingModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestNetworkingModuleRequiredVariables verifies required variables are declared
func TestNetworkingModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")

	requiredVars := []string{
		"environment",
		"aws_region",
		"vpc_cidr",
		"az_count",
		"name_prefix",
		"common_tags",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in networking module", varName)
	}
}

// TestNetworkingModuleOutputs verifies required outputs are declared
func TestNetworkingModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")

	requiredOutputs := []string{
		"vpc_id",
		"vpc_cidr_block",
		"public_subnet_ids",
		"private_subnet_ids",
		"public_subnet_cidr_blocks",
		"private_subnet_cidr_blocks",
		"internet_gateway_id",
		"nat_gateway_ids",
		"alb_security_group_id",
		"eks_security_group_id",
		"rds_security_group_id",
		"redis_security_group_id",
		"public_route_table_id",
		"private_route_table_ids",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in networking module", outputName)
	}
}

// TestNetworkingModuleResources verifies essential resources are declared
func TestNetworkingModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")

	// Verify VPC resource exists
	assert.True(t, ResourceExists(t, modulePath, "aws_vpc", "main"),
		"VPC resource should be declared")

	// Verify Internet Gateway exists
	assert.True(t, ResourceExists(t, modulePath, "aws_internet_gateway", "main"),
		"Internet Gateway resource should be declared")

	// Verify NAT Gateway exists
	assert.True(t, ResourceExists(t, modulePath, "aws_nat_gateway", "main"),
		"NAT Gateway resource should be declared")

	// Verify Security Groups exist
	assert.True(t, ResourceExists(t, modulePath, "aws_security_group", "alb"),
		"ALB Security Group should be declared")
	assert.True(t, ResourceExists(t, modulePath, "aws_security_group", "eks"),
		"EKS Security Group should be declared")
	assert.True(t, ResourceExists(t, modulePath, "aws_security_group", "rds"),
		"RDS Security Group should be declared")
	assert.True(t, ResourceExists(t, modulePath, "aws_security_group", "redis"),
		"Redis Security Group should be declared")
}

// TestNetworkingModuleTagging verifies tagging compliance
func TestNetworkingModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for merge pattern for combining tags
	assert.Contains(t, content, "merge(var.common_tags",
		"Module should merge common_tags with resource-specific tags")
}

// TestNetworkingModuleSecurityGroupRules verifies security group rules are properly configured
func TestNetworkingModuleSecurityGroupRules(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// ALB Security Group should allow HTTPS (443) and HTTP (80)
	assert.Contains(t, content, "from_port   = 443",
		"ALB Security Group should allow HTTPS traffic on port 443")
	assert.Contains(t, content, "from_port   = 80",
		"ALB Security Group should allow HTTP traffic on port 80")

	// RDS Security Group should allow PostgreSQL (5432)
	assert.Contains(t, content, "from_port       = 5432",
		"RDS Security Group should allow PostgreSQL traffic on port 5432")

	// Redis Security Group should allow Redis (6379)
	assert.Contains(t, content, "from_port       = 6379",
		"Redis Security Group should allow Redis traffic on port 6379")

	// EKS Security Group should allow 8080 from ALB
	assert.Contains(t, content, "from_port       = 8080",
		"EKS Security Group should allow traffic on port 8080 from ALB")
}

// TestNetworkingModuleNamingConvention verifies resource naming follows convention
func TestNetworkingModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage in resource names
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify specific naming patterns
	namingPatterns := []string{
		"${var.name_prefix}-vpc",
		"${var.name_prefix}-igw",
		"${var.name_prefix}-sg-alb",
		"${var.name_prefix}-sg-eks",
		"${var.name_prefix}-sg-rds",
		"${var.name_prefix}-sg-redis",
	}

	for _, pattern := range namingPatterns {
		assert.Contains(t, content, pattern,
			"Resource naming should follow pattern: %s", pattern)
	}
}

// TestNetworkingModuleVPCEndpoints verifies VPC endpoints are configured
func TestNetworkingModuleVPCEndpoints(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for S3 VPC endpoint
	assert.Contains(t, content, "aws_vpc_endpoint",
		"VPC endpoints should be declared")

	// Check for ECR VPC endpoints
	assert.Contains(t, content, "ecr.dkr",
		"ECR DKR VPC endpoint should be configured")
	assert.Contains(t, content, "ecr.api",
		"ECR API VPC endpoint should be configured")
}

// TestNetworkingModuleSubnetConfiguration verifies subnet configuration
func TestNetworkingModuleSubnetConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify public subnets have auto-assign public IP enabled
	assert.Contains(t, content, "map_public_ip_on_launch = true",
		"Public subnets should have map_public_ip_on_launch enabled")

	// Verify EKS subnet tags are present
	assert.Contains(t, content, "kubernetes.io/role/elb",
		"Public subnets should have EKS ELB tag")
	assert.Contains(t, content, "kubernetes.io/role/internal-elb",
		"Private subnets should have EKS internal ELB tag")
}

// TestNetworkingModuleAZCountValidation verifies az_count variable has validation
func TestNetworkingModuleAZCountValidation(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for az_count validation
	assert.Contains(t, content, "var.az_count >= 2",
		"az_count variable should have minimum value validation")
	assert.Contains(t, content, "var.az_count <= 3",
		"az_count variable should have maximum value validation")
}
