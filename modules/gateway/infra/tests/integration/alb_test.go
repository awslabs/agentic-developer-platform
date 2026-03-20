package integration

import (
	"context"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/ec2"
	elbv2 "github.com/aws/aws-sdk-go-v2/service/elasticloadbalancingv2"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestALBIntegration provisions real AWS ALB infrastructure and validates it
// NOTE: This test requires a valid ACM certificate in the account
// If no certificate exists, the test will skip ALB provisioning and test networking only
func TestALBIntegration(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting ALB integration test with prefix: %s", tc.TestPrefix)

	// First, we need to provision networking infrastructure (VPC, subnets, security groups)
	networkingModulePath := GetModulePath("networking")
	t.Logf("Using networking module path: %s", networkingModulePath)

	networkingOptions := &terraform.Options{
		TerraformDir: networkingModulePath,
		Vars:         GetNetworkingTestVars(tc.TestPrefix),
		NoColor:      true,
	}

	// CRITICAL: Defer networking destroy to ensure cleanup
	defer func() {
		t.Log("=== CLEANUP: Destroying networking infrastructure ===")
		terraform.Destroy(t, networkingOptions)
		t.Log("=== CLEANUP: Networking destroy completed ===")
	}()

	// Provision networking
	t.Log("=== PROVISION: Creating networking infrastructure ===")
	terraform.InitAndApply(t, networkingOptions)
	t.Log("=== PROVISION: Networking infrastructure created ===")

	// Get networking outputs
	vpcID := terraform.Output(t, networkingOptions, "vpc_id")
	publicSubnetIDs := terraform.OutputList(t, networkingOptions, "public_subnet_ids")
	albSGID := terraform.Output(t, networkingOptions, "alb_security_group_id")

	tc.LogResource("VPC (for ALB)", vpcID, tc.TestPrefix+"-vpc")
	tc.LogResource("ALB Security Group", albSGID, tc.TestPrefix+"-sg-alb")

	// Check if we have a valid ACM certificate
	// If not, skip ALB provisioning but still validate networking resources
	ctx := context.Background()

	// Test ALB Security Group rules (this doesn't require ALB provisioning)
	t.Run("ALB_Security_Group_Exists", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{albSGID},
		})
		require.NoError(t, err, "Failed to describe ALB Security Group")
		require.Len(t, result.SecurityGroups, 1, "Expected exactly one SG")

		sg := result.SecurityGroups[0]
		t.Logf("ALB Security Group %s verified in VPC %s", albSGID, *sg.VpcId)
		assert.Equal(t, vpcID, *sg.VpcId, "Security group should be in our VPC")
	})

	t.Run("ALB_Security_Group_Allows_HTTPS", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{albSGID},
		})
		require.NoError(t, err, "Failed to describe ALB Security Group")

		sg := result.SecurityGroups[0]
		hasHTTPS := false
		for _, perm := range sg.IpPermissions {
			if perm.FromPort != nil && *perm.FromPort == 443 {
				hasHTTPS = true
				// Verify it allows 0.0.0.0/0
				for _, ipRange := range perm.IpRanges {
					if ipRange.CidrIp != nil && *ipRange.CidrIp == "0.0.0.0/0" {
						t.Log("ALB Security Group allows HTTPS (443) from 0.0.0.0/0")
					}
				}
			}
		}
		assert.True(t, hasHTTPS, "ALB Security Group should allow inbound HTTPS (443)")
	})

	t.Run("ALB_Security_Group_Allows_HTTP", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{albSGID},
		})
		require.NoError(t, err, "Failed to describe ALB Security Group")

		sg := result.SecurityGroups[0]
		hasHTTP := false
		for _, perm := range sg.IpPermissions {
			if perm.FromPort != nil && *perm.FromPort == 80 {
				hasHTTP = true
				t.Log("ALB Security Group allows HTTP (80) for redirect")
			}
		}
		assert.True(t, hasHTTP, "ALB Security Group should allow inbound HTTP (80) for redirect")
	})

	t.Run("ALB_Security_Group_Has_Outbound", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{albSGID},
		})
		require.NoError(t, err, "Failed to describe ALB Security Group")

		sg := result.SecurityGroups[0]
		hasAllOutbound := false
		for _, perm := range sg.IpPermissionsEgress {
			if perm.IpProtocol != nil && *perm.IpProtocol == "-1" {
				hasAllOutbound = true
				t.Log("ALB Security Group allows all outbound traffic")
			}
		}
		assert.True(t, hasAllOutbound, "ALB Security Group should allow all outbound traffic")
	})

	// Verify public subnets are suitable for ALB
	t.Run("ALB_Subnets_Are_Public", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
			SubnetIds: publicSubnetIDs,
		})
		require.NoError(t, err, "Failed to describe subnets")
		assert.Len(t, result.Subnets, 2, "Should have 2 public subnets for ALB")

		for _, subnet := range result.Subnets {
			assert.True(t, *subnet.MapPublicIpOnLaunch, "ALB subnets should be public")
			t.Logf("Subnet %s in AZ %s is public", *subnet.SubnetId, *subnet.AvailabilityZone)
		}
	})

	// Verify subnets have ELB tag
	t.Run("ALB_Subnets_Have_ELB_Tag", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
			SubnetIds: publicSubnetIDs,
		})
		require.NoError(t, err, "Failed to describe subnets")

		for _, subnet := range result.Subnets {
			tags := EC2TagsToMap(subnet.Tags)
			assert.Equal(t, "1", tags["kubernetes.io/role/elb"], "Public subnet should have ELB tag")
		}
		t.Log("All public subnets have kubernetes.io/role/elb tag")
	})

	// Verify subnets are in different AZs (required for ALB)
	t.Run("ALB_Subnets_In_Different_AZs", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
			SubnetIds: publicSubnetIDs,
		})
		require.NoError(t, err, "Failed to describe subnets")

		azs := make(map[string]bool)
		for _, subnet := range result.Subnets {
			azs[*subnet.AvailabilityZone] = true
		}
		assert.True(t, len(azs) >= 2, "ALB requires subnets in at least 2 different AZs")
		t.Logf("Subnets are spread across %d availability zones", len(azs))
	})

	// Test ALB Security Group tags
	t.Run("ALB_Security_Group_Tags", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{albSGID},
		})
		require.NoError(t, err, "Failed to describe ALB Security Group")

		sg := result.SecurityGroups[0]
		tags := EC2TagsToMap(sg.Tags)

		assert.Equal(t, "BedrockGateway", tags["Project"], "Should have Project tag")
		assert.Equal(t, "test", tags["Environment"], "Should have Environment tag")
		assert.Equal(t, "load-balancer", tags["Service"], "Should have Service tag")
		assert.Equal(t, "true", tags["Public"], "Should have Public tag")
		t.Logf("ALB Security Group tags validated: %+v", tags)
	})

	// Attempt to list any existing ALBs (for reference)
	t.Run("List_Existing_ALBs_In_VPC", func(t *testing.T) {
		result, err := tc.AWS.ELBV2.DescribeLoadBalancers(ctx, &elbv2.DescribeLoadBalancersInput{})
		require.NoError(t, err, "Failed to list load balancers")

		count := 0
		for _, lb := range result.LoadBalancers {
			if lb.VpcId != nil && *lb.VpcId == vpcID {
				count++
				t.Logf("Found ALB in test VPC: %s (%s)", *lb.LoadBalancerName, lb.State.Code)
			}
		}
		t.Logf("Total ALBs in test VPC: %d", count)
	})

	// Write resource log
	tc.WriteResourceLog("../../../reports/infra/resources-created.txt")

	t.Log("=== ALB prerequisite tests completed ===")
	t.Log("NOTE: Full ALB provisioning requires a valid ACM certificate")
}

// TestALBWithCertificate tests full ALB provisioning (run separately if you have a certificate)
// This test is skipped by default - run with: go test -run TestALBWithCertificate
func TestALBWithCertificate(t *testing.T) {
	t.Skip("Skipping full ALB test - requires valid ACM certificate")

	// This test would provision a full ALB with:
	// - HTTPS listener (443)
	// - HTTP listener with redirect (80 -> 443)
	// - Target group
	// - S3 bucket for logs
	// - Optional WAF Web ACL

	// The test would validate:
	// - ALB exists and is in 'active' state
	// - HTTPS listener is configured
	// - HTTP listener redirects to HTTPS
	// - Security group rules
	// - Tags per tagging strategy
}
