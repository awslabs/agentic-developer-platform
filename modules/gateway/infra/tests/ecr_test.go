package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestEcrModuleFilesExist verifies all required files exist in the ECR module
func TestEcrModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	AssertModuleFilesExist(t, modulePath)
}

// TestEcrModuleValidate validates the ECR module using terraform validate
func TestEcrModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestEcrModuleRequiredVariables verifies required variables are declared
func TestEcrModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")

	requiredVars := []string{
		"environment",
		"name_prefix",
		"common_tags",
		"image_tag_mutability",
		"scan_on_push",
		"lifecycle_policy_rules",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in ECR module", varName)
	}
}

// TestEcrModuleOutputs verifies required outputs are declared
func TestEcrModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")

	requiredOutputs := []string{
		"repository_arn",
		"repository_name",
		"repository_url",
		"registry_id",
		"lifecycle_policy",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in ECR module", outputName)
	}
}

// TestEcrModuleResources verifies essential resources are declared
func TestEcrModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")

	// Verify ECR repository resource exists
	assert.True(t, ResourceExists(t, modulePath, "aws_ecr_repository", "main"),
		"ECR repository should be declared")

	// Verify ECR lifecycle policy exists
	assert.True(t, ResourceExists(t, modulePath, "aws_ecr_lifecycle_policy", "main"),
		"ECR lifecycle policy should be declared")

	// Verify ECR repository policy exists
	assert.True(t, ResourceExists(t, modulePath, "aws_ecr_repository_policy", "main"),
		"ECR repository policy should be declared")
}

// TestEcrModuleTagging verifies tagging compliance
func TestEcrModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for ECR-specific tags as per tagging strategy
	assert.Contains(t, content, "Service = \"container-registry\"",
		"ECR resources should have Service = container-registry tag")
}

// TestEcrModuleNamingConvention verifies resource naming follows convention
func TestEcrModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify ECR repository naming follows bedrockgw-{env}-backend convention
	assert.Contains(t, content, "${var.name_prefix}-backend",
		"ECR repository should follow naming convention bedrockgw-{env}-backend")
}

// TestEcrModuleImageScanningConfiguration verifies image scanning configuration
func TestEcrModuleImageScanningConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify image scanning is configurable
	assert.Contains(t, content, "image_scanning_configuration",
		"ECR should have image scanning configuration")

	// Verify scan_on_push is used from variable
	assert.Contains(t, content, "scan_on_push = var.scan_on_push",
		"Image scanning should use scan_on_push variable")
}

// TestEcrModuleEncryptionConfiguration verifies encryption configuration
func TestEcrModuleEncryptionConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify encryption is configured
	assert.Contains(t, content, "encryption_configuration",
		"ECR should have encryption configuration")

	// Verify AES256 encryption
	assert.Contains(t, content, "encryption_type = \"AES256\"",
		"ECR should use AES256 encryption")
}

// TestEcrModuleLifecyclePolicyConfiguration verifies lifecycle policy configuration
func TestEcrModuleLifecyclePolicyConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify lifecycle policy has rules for different tag prefixes
	assert.Contains(t, content, "tagPrefixList",
		"Lifecycle policy should filter by tag prefixes")

	// Verify production images are kept
	assert.Contains(t, content, "prod",
		"Lifecycle policy should handle production images")

	// Verify staging images are kept
	assert.Contains(t, content, "staging",
		"Lifecycle policy should handle staging images")

	// Verify dev images are kept (with lower retention)
	assert.Contains(t, content, "dev",
		"Lifecycle policy should handle dev images")

	// Verify untagged images are cleaned up
	assert.Contains(t, content, "tagStatus   = \"untagged\"",
		"Lifecycle policy should clean up untagged images")
}

// TestEcrModuleImageTagMutabilityValidation verifies image tag mutability validation
func TestEcrModuleImageTagMutabilityValidation(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Verify image_tag_mutability has validation
	assert.Contains(t, content, "contains([\"MUTABLE\", \"IMMUTABLE\"]",
		"image_tag_mutability should have validation for MUTABLE or IMMUTABLE")
}

// TestEcrModuleVariableDefaults verifies sensible defaults for variables
func TestEcrModuleVariableDefaults(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for default image tag mutability
	assert.Contains(t, content, "default     = \"MUTABLE\"",
		"image_tag_mutability should default to MUTABLE")

	// Check for default scan_on_push
	assert.Contains(t, content, "default     = true",
		"scan_on_push should default to true")

	// Check for default lifecycle policy rules count
	assert.Contains(t, content, "default     = 10",
		"lifecycle_policy_rules should have default value")
}

// TestEcrModuleRepositoryPolicy verifies repository policy configuration
func TestEcrModuleRepositoryPolicy(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify IAM policy document for ECR access
	assert.Contains(t, content, "data \"aws_iam_policy_document\"",
		"ECR should have IAM policy document for access control")

	// Verify cross-account access support
	assert.Contains(t, content, "var.cross_account_arns",
		"ECR should support cross-account access")

	// Verify local account full access
	assert.Contains(t, content, "LocalAccountFullAccess",
		"ECR should allow local account full access")
}

// TestEcrModuleRegistryScanningConfiguration verifies registry scanning configuration
func TestEcrModuleRegistryScanningConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify registry scanning configuration
	assert.Contains(t, content, "aws_ecr_registry_scanning_configuration",
		"ECR should have registry scanning configuration")

	// Verify basic scanning (using BASIC to avoid requiring inspector2:Enable permission)
	assert.Contains(t, content, "scan_type = \"BASIC\"",
		"ECR should use BASIC scanning (avoids inspector2 permission requirement)")

	// Verify scan on push at registry level
	assert.Contains(t, content, "scan_frequency = \"SCAN_ON_PUSH\"",
		"ECR registry should scan on push")
}

// TestEcrModulePullThroughCache verifies pull through cache configuration
func TestEcrModulePullThroughCache(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify pull through cache rules are defined (but optional)
	assert.Contains(t, content, "aws_ecr_pull_through_cache_rule",
		"ECR should support pull through cache")

	// Verify Docker Hub cache
	assert.Contains(t, content, "registry-1.docker.io",
		"ECR should support Docker Hub pull through cache")

	// Verify public ECR cache
	assert.Contains(t, content, "public.ecr.aws",
		"ECR should support public ECR pull through cache")

	// Verify pull through cache is optional (disabled by default to avoid permission issues)
	assert.Contains(t, content, "var.enable_pull_through_cache",
		"Pull through cache should be optional")

	// Check variables file for default
	variablesPath := filepath.Join(modulePath, "variables.tf")
	variablesContent := ReadTerraformFile(t, variablesPath)
	assert.Contains(t, variablesContent, "default     = false",
		"Pull through cache should be disabled by default (requires ecr:CreatePullThroughCacheRule permission)")
}

// TestEcrModuleEventBridgeNotifications verifies EventBridge notifications
func TestEcrModuleEventBridgeNotifications(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify EventBridge rule for image push
	assert.Contains(t, content, "aws_cloudwatch_event_rule",
		"ECR should have EventBridge rule for notifications")

	// Verify event pattern for ECR
	assert.Contains(t, content, "ECR Image Action",
		"EventBridge should capture ECR image actions")

	// Verify notifications are optional
	assert.Contains(t, content, "var.enable_event_notifications",
		"Event notifications should be optional")
}

// TestEcrModuleCloudWatchLogs verifies CloudWatch logs configuration
func TestEcrModuleCloudWatchLogs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify CloudWatch log group
	assert.Contains(t, content, "aws_cloudwatch_log_group",
		"ECR should have CloudWatch log group")

	// Verify log retention
	assert.Contains(t, content, "retention_in_days = 30",
		"ECR logs should have 30 day retention")
}

// TestEcrModuleDockerCommandsOutput verifies Docker commands output
func TestEcrModuleDockerCommandsOutput(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	outputsPath := filepath.Join(modulePath, "outputs.tf")
	content := ReadTerraformFile(t, outputsPath)

	// Verify docker commands output
	assert.Contains(t, content, "docker_push_commands",
		"ECR should output Docker push commands")
}

// TestEcrModuleRepositoryURIOutputs verifies repository URI outputs
func TestEcrModuleRepositoryURIOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	outputsPath := filepath.Join(modulePath, "outputs.tf")
	content := ReadTerraformFile(t, outputsPath)

	// Verify repository URL output
	assert.Contains(t, content, "repository_url",
		"ECR should output repository URL")

	// Verify URI outputs for different tags
	assert.Contains(t, content, "repository_uri_latest",
		"ECR should output repository URI for latest tag")
	assert.Contains(t, content, "repository_uri_dev",
		"ECR should output repository URI for dev tag")
	assert.Contains(t, content, "repository_uri_prod",
		"ECR should output repository URI for prod tag")
}
