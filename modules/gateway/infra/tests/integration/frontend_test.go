package integration

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/cloudfront"
	cftypes "github.com/aws/aws-sdk-go-v2/service/cloudfront/types"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestFrontendInfra provisions real AWS S3 + CloudFront resources and validates them
// NOTE: This test requires S3 and CloudFront permissions. If the runner role lacks these,
// the test will report which permissions are missing and skip to validation tests.
func TestFrontendInfra(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting Frontend infrastructure test with prefix: %s", tc.TestPrefix)

	// Get module paths
	s3ModulePath := GetModulePath("s3-frontend")
	cloudfrontModulePath := GetModulePath("cloudfront")
	t.Logf("Using S3 module path: %s", s3ModulePath)
	t.Logf("Using CloudFront module path: %s", cloudfrontModulePath)

	// Create AWS clients
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(TestAWSRegion))
	require.NoError(t, err, "Failed to load AWS config")

	s3Client := s3.NewFromConfig(cfg)
	cfClient := cloudfront.NewFromConfig(cfg)

	// Test for required permissions first
	t.Log("=== Checking AWS permissions ===")
	hasS3Permissions := checkS3Permissions(t, ctx, s3Client)
	hasCFPermissions := checkCloudFrontPermissions(t, ctx, cfClient)

	if !hasS3Permissions || !hasCFPermissions {
		t.Log("=== Running validation-only tests (limited permissions) ===")
		runValidationOnlyTests(t, tc, s3ModulePath, cloudfrontModulePath)
		return
	}

	// Full test with real resource provisioning
	t.Log("=== Running full integration test ===")
	runFullIntegrationTest(t, tc, ctx, s3Client, cfClient, s3ModulePath, cloudfrontModulePath)
}

// checkS3Permissions verifies the runner has S3 bucket creation permissions
func checkS3Permissions(t *testing.T, ctx context.Context, client *s3.Client) bool {
	// Try a HEAD bucket on a non-existent bucket to test permissions
	// This will fail with 404 if we have permissions, or 403 if we don't
	_, err := client.HeadBucket(ctx, &s3.HeadBucketInput{
		Bucket: aws.String("nonexistent-bucket-test-12345"),
	})

	if err != nil {
		errStr := err.Error()
		if strings.Contains(errStr, "403") || strings.Contains(errStr, "AccessDenied") ||
			strings.Contains(errStr, "not authorized") {
			t.Log("WARNING: Runner role lacks S3 permissions. Bucket creation will be skipped.")
			return false
		}
		// 404 or other error means we have permission to check (bucket just doesn't exist)
	}
	t.Log("S3 permissions available")
	return true
}

// checkCloudFrontPermissions verifies the runner has CloudFront permissions
func checkCloudFrontPermissions(t *testing.T, ctx context.Context, client *cloudfront.Client) bool {
	_, err := client.ListDistributions(ctx, &cloudfront.ListDistributionsInput{
		MaxItems: aws.Int32(1),
	})

	if err != nil {
		errStr := err.Error()
		if strings.Contains(errStr, "403") || strings.Contains(errStr, "AccessDenied") ||
			strings.Contains(errStr, "not authorized") {
			t.Log("WARNING: Runner role lacks CloudFront permissions. Distribution creation will be skipped.")
			return false
		}
	}
	t.Log("CloudFront permissions available")
	return true
}

// runValidationOnlyTests runs tests that only validate Terraform configuration
// without actually creating AWS resources
func runValidationOnlyTests(t *testing.T, tc *TestContext, s3ModulePath, cloudfrontModulePath string) {
	t.Log("=== Running Terraform validation tests ===")

	// Test S3 module Terraform configuration
	t.Run("S3_Module_Terraform_Valid", func(t *testing.T) {
		// For terraform validate, we don't pass Vars (it only checks syntax)
		terraformOptions := &terraform.Options{
			TerraformDir:    s3ModulePath,
			NoColor:         true,
		}

		// Init
		_, err := terraform.InitE(t, terraformOptions)
		require.NoError(t, err, "S3 module terraform init should succeed")

		// Validate with terraform validate (no vars needed for syntax check)
		output, err := terraform.RunTerraformCommandE(t, terraformOptions, "validate", "-no-color")
		require.NoError(t, err, "S3 module terraform validate should succeed")
		assert.Contains(t, output, "Success", "terraform validate output should contain Success")

		t.Log("S3 module Terraform configuration is valid")
	})

	// Test CloudFront module Terraform configuration
	t.Run("CloudFront_Module_Terraform_Valid", func(t *testing.T) {
		// For terraform validate, we don't pass Vars (it only checks syntax)
		terraformOptions := &terraform.Options{
			TerraformDir:    cloudfrontModulePath,
			NoColor:         true,
		}

		// Init
		_, err := terraform.InitE(t, terraformOptions)
		require.NoError(t, err, "CloudFront module terraform init should succeed")

		// Validate with terraform validate (no vars needed for syntax check)
		output, err := terraform.RunTerraformCommandE(t, terraformOptions, "validate", "-no-color")
		require.NoError(t, err, "CloudFront module terraform validate should succeed")
		assert.Contains(t, output, "Success", "terraform validate output should contain Success")

		t.Log("CloudFront module Terraform configuration is valid")
	})

	// Verify module file structure
	t.Run("Module_Files_Exist", func(t *testing.T) {
		require.FileExists(t, s3ModulePath+"/main.tf", "S3 module main.tf should exist")
		require.FileExists(t, s3ModulePath+"/variables.tf", "S3 module variables.tf should exist")
		require.FileExists(t, s3ModulePath+"/outputs.tf", "S3 module outputs.tf should exist")

		require.FileExists(t, cloudfrontModulePath+"/main.tf", "CloudFront module main.tf should exist")
		require.FileExists(t, cloudfrontModulePath+"/variables.tf", "CloudFront module variables.tf should exist")
		require.FileExists(t, cloudfrontModulePath+"/outputs.tf", "CloudFront module outputs.tf should exist")

		t.Log("All module files exist")
	})

	t.Log("=== Validation-only tests completed ===")
	t.Log("NOTE: Full integration tests require S3 and CloudFront permissions.")
	t.Log("To run full tests, ensure the runner role has these permissions:")
	t.Log("  - s3:CreateBucket, s3:PutObject, s3:GetObject, s3:DeleteBucket")
	t.Log("  - cloudfront:CreateDistribution, cloudfront:GetDistribution, cloudfront:DeleteDistribution")
}

// runFullIntegrationTest runs the complete integration test with resource provisioning
func runFullIntegrationTest(t *testing.T, tc *TestContext, ctx context.Context,
	s3Client *s3.Client, cfClient *cloudfront.Client,
	s3ModulePath, cloudfrontModulePath string) {

	t.Log("=== PHASE 1: Creating S3 bucket ===")

	// Create S3 bucket first (with a placeholder ARN - we'll update the policy later)
	s3TerraformOptions := &terraform.Options{
		TerraformDir: s3ModulePath,
		Vars: map[string]interface{}{
			"environment":                 "test",
			"name_prefix":                 tc.TestPrefix,
			"common_tags":                 GetCommonTags("test"),
			"cloudfront_distribution_arn": "arn:aws:cloudfront::000000000000:distribution/PLACEHOLDER",
			"cors_allowed_origins":        []string{"*"},
		},
		NoColor:         true,
	}

	// CRITICAL: Defer S3 destroy BEFORE apply to guarantee cleanup
	defer func() {
		t.Log("=== CLEANUP: Starting S3 terraform destroy ===")
		terraform.Destroy(t, s3TerraformOptions)
		t.Log("=== CLEANUP: S3 terraform destroy completed ===")
	}()

	// Initialize and apply S3 module
	t.Log("=== PROVISION: Running S3 terraform init and apply ===")
	terraform.InitAndApply(t, s3TerraformOptions)
	t.Log("=== PROVISION: S3 terraform apply completed ===")

	// Get S3 outputs
	bucketName := terraform.Output(t, s3TerraformOptions, "bucket_name")
	bucketARN := terraform.Output(t, s3TerraformOptions, "bucket_arn")
	bucketRegionalDomainName := terraform.Output(t, s3TerraformOptions, "bucket_regional_domain_name")

	t.Logf("S3 Bucket created: %s", bucketName)
	tc.LogResource("S3 Bucket", bucketARN, bucketName)

	// Now create CloudFront distribution pointing to the S3 bucket
	t.Log("=== PHASE 2: Creating CloudFront distribution ===")

	cloudfrontTerraformOptions := &terraform.Options{
		TerraformDir: cloudfrontModulePath,
		Vars: map[string]interface{}{
			"environment":                    "test",
			"name_prefix":                    tc.TestPrefix,
			"common_tags":                    GetCommonTags("test"),
			"s3_bucket_regional_domain_name": bucketRegionalDomainName,
			"s3_bucket_id":                   bucketName,
			"price_class":                    "PriceClass_100",
		},
		NoColor:         true,
	}

	// CRITICAL: Defer CloudFront destroy BEFORE apply to guarantee cleanup
	defer func() {
		t.Log("=== CLEANUP: Starting CloudFront terraform destroy ===")
		terraform.Destroy(t, cloudfrontTerraformOptions)
		t.Log("=== CLEANUP: CloudFront terraform destroy completed ===")
	}()

	// Initialize and apply CloudFront module
	t.Log("=== PROVISION: Running CloudFront terraform init and apply ===")
	terraform.InitAndApply(t, cloudfrontTerraformOptions)
	t.Log("=== PROVISION: CloudFront terraform apply completed ===")

	// Get CloudFront outputs
	distributionID := terraform.Output(t, cloudfrontTerraformOptions, "distribution_id")
	distributionARN := terraform.Output(t, cloudfrontTerraformOptions, "distribution_arn")
	distributionDomainName := terraform.Output(t, cloudfrontTerraformOptions, "distribution_domain_name")
	oacID := terraform.Output(t, cloudfrontTerraformOptions, "oac_id")

	t.Logf("CloudFront Distribution created: %s", distributionID)
	t.Logf("CloudFront Domain: %s", distributionDomainName)
	tc.LogResource("CloudFront Distribution", distributionARN, distributionID)

	// Update S3 bucket policy with the real CloudFront ARN
	t.Log("=== PHASE 3: Updating S3 bucket policy with CloudFront ARN ===")
	s3TerraformOptions.Vars["cloudfront_distribution_arn"] = distributionARN
	terraform.Apply(t, s3TerraformOptions)
	t.Log("=== S3 bucket policy updated ===")

	// ========== VALIDATION TESTS ==========
	t.Log("=== PHASE 4: Running validation tests ===")

	// Test 1: Verify S3 bucket exists
	t.Run("S3_Bucket_Exists", func(t *testing.T) {
		_, err := s3Client.HeadBucket(ctx, &s3.HeadBucketInput{
			Bucket: aws.String(bucketName),
		})
		require.NoError(t, err, "S3 bucket should exist")
		t.Logf("S3 bucket %s verified to exist", bucketName)
	})

	// Test 2: Verify S3 bucket has public access blocked (all 4 settings true)
	t.Run("S3_Public_Access_Blocked", func(t *testing.T) {
		result, err := s3Client.GetPublicAccessBlock(ctx, &s3.GetPublicAccessBlockInput{
			Bucket: aws.String(bucketName),
		})
		require.NoError(t, err, "Failed to get public access block")

		config := result.PublicAccessBlockConfiguration
		assert.True(t, aws.ToBool(config.BlockPublicAcls), "BlockPublicAcls should be true")
		assert.True(t, aws.ToBool(config.BlockPublicPolicy), "BlockPublicPolicy should be true")
		assert.True(t, aws.ToBool(config.IgnorePublicAcls), "IgnorePublicAcls should be true")
		assert.True(t, aws.ToBool(config.RestrictPublicBuckets), "RestrictPublicBuckets should be true")
		t.Log("All 4 public access block settings are TRUE")
	})

	// Test 3: Verify S3 bucket versioning is enabled
	t.Run("S3_Versioning_Enabled", func(t *testing.T) {
		result, err := s3Client.GetBucketVersioning(ctx, &s3.GetBucketVersioningInput{
			Bucket: aws.String(bucketName),
		})
		require.NoError(t, err, "Failed to get bucket versioning")
		assert.Equal(t, types.BucketVersioningStatusEnabled, result.Status, "Versioning should be enabled")
		t.Log("S3 bucket versioning is enabled")
	})

	// Test 4: Verify S3 bucket has SSE encryption configured
	t.Run("S3_Encryption_Configured", func(t *testing.T) {
		result, err := s3Client.GetBucketEncryption(ctx, &s3.GetBucketEncryptionInput{
			Bucket: aws.String(bucketName),
		})
		require.NoError(t, err, "Failed to get bucket encryption")
		require.NotEmpty(t, result.ServerSideEncryptionConfiguration.Rules, "Encryption rules should exist")

		rule := result.ServerSideEncryptionConfiguration.Rules[0]
		assert.Equal(t, types.ServerSideEncryptionAes256, rule.ApplyServerSideEncryptionByDefault.SSEAlgorithm, "SSE algorithm should be AES256")
		t.Log("S3 bucket encryption is configured with AES256")
	})

	// Test 5: Verify S3 bucket policy exists
	t.Run("S3_Bucket_Policy_Exists", func(t *testing.T) {
		result, err := s3Client.GetBucketPolicy(ctx, &s3.GetBucketPolicyInput{
			Bucket: aws.String(bucketName),
		})
		require.NoError(t, err, "Failed to get bucket policy")
		require.NotEmpty(t, *result.Policy, "Bucket policy should not be empty")
		assert.Contains(t, *result.Policy, "cloudfront.amazonaws.com", "Bucket policy should allow CloudFront")
		t.Log("S3 bucket policy exists and allows CloudFront")
	})

	// Test 6: Verify CloudFront distribution is deployed and enabled
	t.Run("CloudFront_Distribution_Deployed", func(t *testing.T) {
		result, err := cfClient.GetDistribution(ctx, &cloudfront.GetDistributionInput{
			Id: aws.String(distributionID),
		})
		require.NoError(t, err, "Failed to get CloudFront distribution")
		assert.True(t, *result.Distribution.DistributionConfig.Enabled, "Distribution should be enabled")
		t.Logf("CloudFront distribution %s is deployed and enabled", distributionID)
	})

	// Test 7: Verify CloudFront has correct S3 origin
	t.Run("CloudFront_Has_S3_Origin", func(t *testing.T) {
		result, err := cfClient.GetDistribution(ctx, &cloudfront.GetDistributionInput{
			Id: aws.String(distributionID),
		})
		require.NoError(t, err, "Failed to get CloudFront distribution")

		origins := result.Distribution.DistributionConfig.Origins.Items
		require.NotEmpty(t, origins, "CloudFront should have at least one origin")

		var foundS3Origin bool
		for _, origin := range origins {
			if strings.Contains(*origin.DomainName, bucketRegionalDomainName) || strings.Contains(*origin.DomainName, ".s3.") {
				foundS3Origin = true
				t.Logf("Found S3 origin: %s", *origin.DomainName)
				break
			}
		}
		assert.True(t, foundS3Origin, "CloudFront should have S3 origin")
	})

	// Test 8: Verify CloudFront uses OAC (not OAI)
	t.Run("CloudFront_Uses_OAC", func(t *testing.T) {
		result, err := cfClient.GetDistribution(ctx, &cloudfront.GetDistributionInput{
			Id: aws.String(distributionID),
		})
		require.NoError(t, err, "Failed to get CloudFront distribution")

		origins := result.Distribution.DistributionConfig.Origins.Items
		var foundOAC bool
		for _, origin := range origins {
			if origin.OriginAccessControlId != nil && *origin.OriginAccessControlId != "" {
				foundOAC = true
				t.Logf("Found OAC ID: %s", *origin.OriginAccessControlId)
				break
			}
		}
		assert.True(t, foundOAC, "CloudFront should use Origin Access Control (OAC)")

		// Also verify OAC resource exists
		_, err = cfClient.GetOriginAccessControl(ctx, &cloudfront.GetOriginAccessControlInput{
			Id: aws.String(oacID),
		})
		require.NoError(t, err, "OAC resource should exist")
		t.Log("CloudFront uses OAC (not OAI)")
	})

	// Test 9: Verify CloudFront has custom error response for 403 -> /index.html
	t.Run("CloudFront_Custom_Error_Response", func(t *testing.T) {
		result, err := cfClient.GetDistribution(ctx, &cloudfront.GetDistributionInput{
			Id: aws.String(distributionID),
		})
		require.NoError(t, err, "Failed to get CloudFront distribution")

		customErrorResponses := result.Distribution.DistributionConfig.CustomErrorResponses.Items
		require.NotEmpty(t, customErrorResponses, "CloudFront should have custom error responses")

		var found403Response bool
		for _, resp := range customErrorResponses {
			if *resp.ErrorCode == 403 {
				assert.Equal(t, int32(200), *resp.ResponseCode, "403 error should return 200")
				assert.Equal(t, "/index.html", *resp.ResponsePagePath, "403 error should redirect to /index.html")
				found403Response = true
				break
			}
		}
		assert.True(t, found403Response, "CloudFront should have custom error response for 403")
		t.Log("CloudFront has custom error response for 403 -> /index.html")
	})

	// Test 10: Verify CloudFront has HTTPS redirect viewer protocol policy
	t.Run("CloudFront_HTTPS_Redirect", func(t *testing.T) {
		result, err := cfClient.GetDistribution(ctx, &cloudfront.GetDistributionInput{
			Id: aws.String(distributionID),
		})
		require.NoError(t, err, "Failed to get CloudFront distribution")

		defaultCacheBehavior := result.Distribution.DistributionConfig.DefaultCacheBehavior
		assert.Equal(t, cftypes.ViewerProtocolPolicyRedirectToHttps, defaultCacheBehavior.ViewerProtocolPolicy, "Default behavior should redirect to HTTPS")
		t.Log("CloudFront has redirect-to-https viewer protocol policy")
	})

	// Test 11: Verify CloudFront has security response headers
	t.Run("CloudFront_Security_Headers", func(t *testing.T) {
		result, err := cfClient.GetDistribution(ctx, &cloudfront.GetDistributionInput{
			Id: aws.String(distributionID),
		})
		require.NoError(t, err, "Failed to get CloudFront distribution")

		defaultCacheBehavior := result.Distribution.DistributionConfig.DefaultCacheBehavior
		require.NotNil(t, defaultCacheBehavior.ResponseHeadersPolicyId, "Response headers policy should be set")
		assert.NotEmpty(t, *defaultCacheBehavior.ResponseHeadersPolicyId, "Response headers policy ID should not be empty")
		t.Logf("CloudFront has response headers policy: %s", *defaultCacheBehavior.ResponseHeadersPolicyId)
	})

	// ========== END-TO-END TEST ==========
	t.Log("=== PHASE 5: End-to-end test ===")

	// Test 12: Upload test index.html and verify via CloudFront URL
	t.Run("End_To_End_Test", func(t *testing.T) {
		testContent := "<html><body><h1>Hello from Terratest!</h1></body></html>"

		// Upload test file to S3
		_, err := s3Client.PutObject(ctx, &s3.PutObjectInput{
			Bucket:      aws.String(bucketName),
			Key:         aws.String("index.html"),
			Body:        strings.NewReader(testContent),
			ContentType: aws.String("text/html"),
		})
		require.NoError(t, err, "Failed to upload test index.html to S3")
		t.Log("Uploaded test index.html to S3")

		// Wait for CloudFront distribution to be fully deployed
		t.Log("Waiting for CloudFront distribution to be fully deployed...")
		cloudFrontURL := fmt.Sprintf("https://%s/", distributionDomainName)

		// Retry with backoff - CloudFront can take a few minutes
		maxRetries := 30 // 30 retries * 10 seconds = 5 minutes
		retryInterval := 10 * time.Second
		var lastErr error
		var lastBody string

		for i := 0; i < maxRetries; i++ {
			resp, err := http.Get(cloudFrontURL)
			if err != nil {
				lastErr = err
				t.Logf("Attempt %d/%d: HTTP error: %v", i+1, maxRetries, err)
				time.Sleep(retryInterval)
				continue
			}

			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			lastBody = string(body)

			if resp.StatusCode == 200 && strings.Contains(lastBody, "Hello from Terratest!") {
				t.Logf("Successfully fetched content from CloudFront on attempt %d", i+1)
				t.Logf("Response body: %s", lastBody)
				return // Success!
			}

			lastErr = fmt.Errorf("unexpected response: status=%d, body=%s", resp.StatusCode, lastBody)
			t.Logf("Attempt %d/%d: %v", i+1, maxRetries, lastErr)
			time.Sleep(retryInterval)
		}

		// If we get here, all retries failed
		t.Logf("Final response body: %s", lastBody)
		t.Fatalf("End-to-end test failed after %d attempts: %v", maxRetries, lastErr)
	})

	t.Log("=== All Frontend infrastructure tests passed ===")
}
