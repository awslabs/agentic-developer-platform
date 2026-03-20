package integration

import (
	"context"
	"encoding/base64"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ecr"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestECRIntegration provisions real AWS ECR repository and validates it
func TestECRIntegration(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting ECR integration test with prefix: %s", tc.TestPrefix)

	// Get module path
	modulePath := GetModulePath("ecr")
	t.Logf("Using module path: %s", modulePath)

	// Setup Terraform options
	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		Vars: map[string]interface{}{
			"environment":               "test",
			"name_prefix":               tc.TestPrefix,
			"common_tags":               GetCommonTags("test"),
			"image_tag_mutability":      "MUTABLE",
			"scan_on_push":              true,
			"lifecycle_policy_rules":    10,
			"cross_account_arns":        []string{},
			"enable_pull_through_cache": false, // Disable to avoid permission issues
			"enable_event_notifications": false,
		},
		NoColor:         true,
	}

	// CRITICAL: Defer destroy BEFORE apply to guarantee cleanup
	defer func() {
		t.Log("=== CLEANUP: Starting terraform destroy ===")
		terraform.Destroy(t, terraformOptions)
		t.Log("=== CLEANUP: Terraform destroy completed ===")
	}()

	// Initialize and apply Terraform
	t.Log("=== PROVISION: Running terraform init and apply ===")
	terraform.InitAndApply(t, terraformOptions)
	t.Log("=== PROVISION: Terraform apply completed ===")

	// Get outputs
	repoURL := terraform.Output(t, terraformOptions, "repository_url")
	repoARN := terraform.Output(t, terraformOptions, "repository_arn")
	repoName := terraform.Output(t, terraformOptions, "repository_name")

	// Log created resources
	tc.LogResource("ECR Repository", repoARN, repoName)

	// ========== VALIDATION TESTS ==========
	ctx := context.Background()

	// Test 1: Verify repository exists
	t.Run("ECR_Repository_Exists", func(t *testing.T) {
		result, err := tc.AWS.ECR.DescribeRepositories(ctx, &ecr.DescribeRepositoriesInput{
			RepositoryNames: []string{repoName},
		})
		require.NoError(t, err, "Failed to describe ECR repository")
		require.Len(t, result.Repositories, 1, "Expected exactly one repository")

		repo := result.Repositories[0]
		t.Logf("ECR Repository %s verified with URL: %s", *repo.RepositoryName, *repo.RepositoryUri)
		assert.Equal(t, repoURL, *repo.RepositoryUri, "Repository URL should match URI")
	})

	// Test 2: Verify image scanning is enabled
	t.Run("ECR_Image_Scanning_Enabled", func(t *testing.T) {
		result, err := tc.AWS.ECR.DescribeRepositories(ctx, &ecr.DescribeRepositoriesInput{
			RepositoryNames: []string{repoName},
		})
		require.NoError(t, err, "Failed to describe ECR repository")

		repo := result.Repositories[0]
		require.NotNil(t, repo.ImageScanningConfiguration, "Image scanning configuration should exist")
		assert.True(t, repo.ImageScanningConfiguration.ScanOnPush, "Scan on push should be enabled")
		t.Log("Image scanning on push is enabled")
	})

	// Test 3: Verify lifecycle policy is configured
	t.Run("ECR_Lifecycle_Policy_Configured", func(t *testing.T) {
		result, err := tc.AWS.ECR.GetLifecyclePolicy(ctx, &ecr.GetLifecyclePolicyInput{
			RepositoryName: aws.String(repoName),
		})
		require.NoError(t, err, "Failed to get ECR lifecycle policy")
		require.NotEmpty(t, *result.LifecyclePolicyText, "Lifecycle policy should not be empty")
		t.Logf("Lifecycle policy configured: %s...", (*result.LifecyclePolicyText)[:100])
	})

	// Test 4: Verify ECR authentication (docker login test)
	t.Run("ECR_Authentication_Works", func(t *testing.T) {
		result, err := tc.AWS.ECR.GetAuthorizationToken(ctx, &ecr.GetAuthorizationTokenInput{})
		require.NoError(t, err, "Failed to get ECR authorization token")
		require.NotEmpty(t, result.AuthorizationData, "Authorization data should not be empty")

		// Decode and verify token format
		authData := result.AuthorizationData[0]
		tokenDecoded, err := base64.StdEncoding.DecodeString(*authData.AuthorizationToken)
		require.NoError(t, err, "Failed to decode authorization token")

		// Token format is "AWS:password"
		tokenStr := string(tokenDecoded)
		parts := strings.SplitN(tokenStr, ":", 2)
		assert.Equal(t, "AWS", parts[0], "Token username should be 'AWS'")
		assert.NotEmpty(t, parts[1], "Token password should not be empty")
		t.Log("ECR authentication token obtained successfully")
	})

	// Test 5: Verify repository tags
	t.Run("ECR_Repository_Tags", func(t *testing.T) {
		result, err := tc.AWS.ECR.ListTagsForResource(ctx, &ecr.ListTagsForResourceInput{
			ResourceArn: aws.String(repoARN),
		})
		require.NoError(t, err, "Failed to list ECR repository tags")

		tags := make(map[string]string)
		for _, tag := range result.Tags {
			tags[*tag.Key] = *tag.Value
		}

		assert.Equal(t, "BedrockGateway", tags["Project"], "Repository should have Project tag")
		assert.Equal(t, "test", tags["Environment"], "Repository should have Environment tag")
		assert.Equal(t, "terraform", tags["ManagedBy"], "Repository should have ManagedBy tag")
		assert.Equal(t, "container-registry", tags["Service"], "Repository should have Service tag")
		t.Logf("ECR tags validated: %+v", tags)
	})

	// Test 6: Verify repository encryption
	t.Run("ECR_Repository_Encryption", func(t *testing.T) {
		result, err := tc.AWS.ECR.DescribeRepositories(ctx, &ecr.DescribeRepositoriesInput{
			RepositoryNames: []string{repoName},
		})
		require.NoError(t, err, "Failed to describe ECR repository")

		repo := result.Repositories[0]
		require.NotNil(t, repo.EncryptionConfiguration, "Encryption configuration should exist")
		assert.Equal(t, "AES256", string(repo.EncryptionConfiguration.EncryptionType), "Encryption should be AES256")
		t.Log("ECR repository encryption verified: AES256")
	})

	t.Log("=== All ECR integration tests passed ===")
}
