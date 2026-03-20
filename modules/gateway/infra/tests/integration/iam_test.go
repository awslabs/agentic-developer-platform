package integration

import (
	"context"
	"encoding/json"
	"net/url"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iam"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestIAMIntegration provisions real AWS IAM roles and validates them
func TestIAMIntegration(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting IAM integration test with prefix: %s", tc.TestPrefix)

	// Get module path
	modulePath := GetModulePath("iam")
	t.Logf("Using module path: %s", modulePath)

	// Setup Terraform options
	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		Vars: map[string]interface{}{
			"environment":             "test",
			"name_prefix":             tc.TestPrefix,
			"common_tags":             GetCommonTags("test"),
			"cluster_name":            tc.TestPrefix + "-eks-cluster",
			"pool_account_arns":       []string{},
			"create_instance_profile": false, // Skip instance profile due to missing iam:TagInstanceProfile permission
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
	eksClusterRoleARN := terraform.Output(t, terraformOptions, "eks_cluster_role_arn")
	eksClusterRoleName := terraform.Output(t, terraformOptions, "eks_cluster_role_name")
	eksNodeGroupRoleARN := terraform.Output(t, terraformOptions, "eks_node_group_role_arn")
	eksNodeGroupRoleName := terraform.Output(t, terraformOptions, "eks_node_group_role_name")
	gatewayServiceRoleARN := terraform.Output(t, terraformOptions, "gateway_service_role_arn")
	gatewayServiceRoleName := terraform.Output(t, terraformOptions, "gateway_service_role_name")

	// Log created resources
	tc.LogResource("IAM Role (EKS Cluster)", eksClusterRoleARN, eksClusterRoleName)
	tc.LogResource("IAM Role (EKS Node Group)", eksNodeGroupRoleARN, eksNodeGroupRoleName)
	tc.LogResource("IAM Role (Gateway Service)", gatewayServiceRoleARN, gatewayServiceRoleName)

	// ========== VALIDATION TESTS ==========
	ctx := context.Background()

	// Test 1: Verify EKS Cluster role exists
	t.Run("EKS_Cluster_Role_Exists", func(t *testing.T) {
		result, err := tc.AWS.IAM.GetRole(ctx, &iam.GetRoleInput{
			RoleName: aws.String(eksClusterRoleName),
		})
		require.NoError(t, err, "Failed to get EKS cluster role")
		assert.Equal(t, eksClusterRoleARN, *result.Role.Arn, "Role ARN should match")
		t.Logf("EKS Cluster role %s verified", eksClusterRoleName)
	})

	// Test 2: Verify EKS Cluster role has correct trust policy
	t.Run("EKS_Cluster_Role_Trust_Policy", func(t *testing.T) {
		result, err := tc.AWS.IAM.GetRole(ctx, &iam.GetRoleInput{
			RoleName: aws.String(eksClusterRoleName),
		})
		require.NoError(t, err, "Failed to get EKS cluster role")

		// Decode trust policy
		trustPolicyEncoded := *result.Role.AssumeRolePolicyDocument
		trustPolicyDecoded, err := url.QueryUnescape(trustPolicyEncoded)
		require.NoError(t, err, "Failed to decode trust policy")

		var trustPolicy map[string]interface{}
		err = json.Unmarshal([]byte(trustPolicyDecoded), &trustPolicy)
		require.NoError(t, err, "Failed to parse trust policy JSON")

		// Verify eks.amazonaws.com is in the principal
		statements := trustPolicy["Statement"].([]interface{})
		hasEKSTrust := false
		for _, stmt := range statements {
			statement := stmt.(map[string]interface{})
			principal := statement["Principal"].(map[string]interface{})
			if service, ok := principal["Service"].(string); ok {
				if service == "eks.amazonaws.com" {
					hasEKSTrust = true
					break
				}
			}
		}
		assert.True(t, hasEKSTrust, "EKS cluster role should trust eks.amazonaws.com")
		t.Log("EKS Cluster role trust policy verified for eks.amazonaws.com")
	})

	// Test 3: Verify EKS Cluster role has AmazonEKSClusterPolicy attached
	t.Run("EKS_Cluster_Role_Has_EKS_Policy", func(t *testing.T) {
		result, err := tc.AWS.IAM.ListAttachedRolePolicies(ctx, &iam.ListAttachedRolePoliciesInput{
			RoleName: aws.String(eksClusterRoleName),
		})
		require.NoError(t, err, "Failed to list attached policies")

		hasEKSClusterPolicy := false
		for _, policy := range result.AttachedPolicies {
			if strings.Contains(*policy.PolicyArn, "AmazonEKSClusterPolicy") {
				hasEKSClusterPolicy = true
				t.Logf("Found attached policy: %s", *policy.PolicyArn)
				break
			}
		}
		assert.True(t, hasEKSClusterPolicy, "EKS cluster role should have AmazonEKSClusterPolicy")
	})

	// Test 4: Verify EKS Node Group role exists
	t.Run("EKS_Node_Group_Role_Exists", func(t *testing.T) {
		result, err := tc.AWS.IAM.GetRole(ctx, &iam.GetRoleInput{
			RoleName: aws.String(eksNodeGroupRoleName),
		})
		require.NoError(t, err, "Failed to get EKS node group role")
		assert.Equal(t, eksNodeGroupRoleARN, *result.Role.Arn, "Role ARN should match")
		t.Logf("EKS Node Group role %s verified", eksNodeGroupRoleName)
	})

	// Test 5: Verify EKS Node Group role has correct managed policies
	t.Run("EKS_Node_Group_Role_Has_Required_Policies", func(t *testing.T) {
		result, err := tc.AWS.IAM.ListAttachedRolePolicies(ctx, &iam.ListAttachedRolePoliciesInput{
			RoleName: aws.String(eksNodeGroupRoleName),
		})
		require.NoError(t, err, "Failed to list attached policies")

		requiredPolicies := map[string]bool{
			"AmazonEKSWorkerNodePolicy":          false,
			"AmazonEKS_CNI_Policy":               false,
			"AmazonEC2ContainerRegistryReadOnly": false,
		}

		for _, policy := range result.AttachedPolicies {
			for policyName := range requiredPolicies {
				if strings.Contains(*policy.PolicyArn, policyName) {
					requiredPolicies[policyName] = true
					t.Logf("Found required policy: %s", *policy.PolicyArn)
				}
			}
		}

		for policyName, found := range requiredPolicies {
			assert.True(t, found, "EKS node group role should have %s", policyName)
		}
	})

	// Test 6: Verify Gateway Service role exists
	t.Run("Gateway_Service_Role_Exists", func(t *testing.T) {
		result, err := tc.AWS.IAM.GetRole(ctx, &iam.GetRoleInput{
			RoleName: aws.String(gatewayServiceRoleName),
		})
		require.NoError(t, err, "Failed to get gateway service role")
		assert.Equal(t, gatewayServiceRoleARN, *result.Role.Arn, "Role ARN should match")
		t.Logf("Gateway Service role %s verified", gatewayServiceRoleName)
	})

	// Test 7: Verify Gateway Service role has STS permissions
	t.Run("Gateway_Service_Role_Has_STS_Permissions", func(t *testing.T) {
		result, err := tc.AWS.IAM.ListRolePolicies(ctx, &iam.ListRolePoliciesInput{
			RoleName: aws.String(gatewayServiceRoleName),
		})
		require.NoError(t, err, "Failed to list inline policies")

		// Check for inline policies with STS permissions
		hasStsPolicy := false
		for _, policyName := range result.PolicyNames {
			policyResult, err := tc.AWS.IAM.GetRolePolicy(ctx, &iam.GetRolePolicyInput{
				RoleName:   aws.String(gatewayServiceRoleName),
				PolicyName: aws.String(policyName),
			})
			if err != nil {
				continue
			}

			policyDocument, err := url.QueryUnescape(*policyResult.PolicyDocument)
			if err != nil {
				continue
			}

			if strings.Contains(policyDocument, "sts:GetCallerIdentity") ||
				strings.Contains(policyDocument, "sts:AssumeRole") {
				hasStsPolicy = true
				t.Logf("Found STS permissions in policy: %s", policyName)
			}
		}
		assert.True(t, hasStsPolicy, "Gateway service role should have STS permissions")
	})

	// Test 8: Verify role names follow naming convention
	t.Run("Role_Names_Follow_Convention", func(t *testing.T) {
		assert.True(t, strings.HasPrefix(eksClusterRoleName, tc.TestPrefix),
			"EKS cluster role should start with test prefix")
		assert.True(t, strings.HasPrefix(eksNodeGroupRoleName, tc.TestPrefix),
			"EKS node group role should start with test prefix")
		assert.True(t, strings.HasPrefix(gatewayServiceRoleName, tc.TestPrefix),
			"Gateway service role should start with test prefix")
		t.Log("Role names follow naming convention")
	})

	// Test 9: Verify IAM roles have correct tags
	// Note: This test may be skipped if the runner role doesn't have iam:ListRoleTags permission
	t.Run("IAM_Roles_Have_Tags", func(t *testing.T) {
		// Check EKS cluster role tags
		result, err := tc.AWS.IAM.ListRoleTags(ctx, &iam.ListRoleTagsInput{
			RoleName: aws.String(eksClusterRoleName),
		})
		if err != nil {
			// Check if this is a permission error - skip the test if so
			if strings.Contains(err.Error(), "AccessDenied") || strings.Contains(err.Error(), "not authorized") {
				t.Skip("Skipping tag verification - runner role lacks iam:ListRoleTags permission")
			}
			require.NoError(t, err, "Failed to list role tags")
		}

		tags := make(map[string]string)
		for _, tag := range result.Tags {
			tags[*tag.Key] = *tag.Value
		}

		// Log all tags first to understand what we have
		t.Logf("IAM role tags found: %+v", tags)

		// Verify required tags from common_tags
		assert.Equal(t, "BedrockGateway", tags["Project"], "Role should have Project tag")
		assert.Equal(t, "terraform", tags["ManagedBy"], "Role should have ManagedBy tag")
		assert.Equal(t, "test", tags["Environment"], "Role should have Environment tag")

		// Verify module-specific tags
		assert.Contains(t, tags, "Service", "Role should have Service tag")
		assert.Contains(t, tags, "Purpose", "Role should have Purpose tag")
	})

	t.Log("=== All IAM integration tests passed ===")
}
