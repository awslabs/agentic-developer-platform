package integration

import (
	"context"
	"fmt"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestNetworkingIntegration provisions real AWS networking infrastructure and validates it
func TestNetworkingIntegration(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting networking integration test with prefix: %s", tc.TestPrefix)

	// Get module path
	modulePath := GetModulePath("networking")
	t.Logf("Using module path: %s", modulePath)

	// Setup Terraform options
	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		Vars:         GetNetworkingTestVars(tc.TestPrefix),
		NoColor:      true,
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
	vpcID := terraform.Output(t, terraformOptions, "vpc_id")
	vpcCIDR := terraform.Output(t, terraformOptions, "vpc_cidr_block")
	publicSubnetIDs := terraform.OutputList(t, terraformOptions, "public_subnet_ids")
	privateSubnetIDs := terraform.OutputList(t, terraformOptions, "private_subnet_ids")
	igwID := terraform.Output(t, terraformOptions, "internet_gateway_id")
	natGatewayIDs := terraform.OutputList(t, terraformOptions, "nat_gateway_ids")
	albSGID := terraform.Output(t, terraformOptions, "alb_security_group_id")
	eksSGID := terraform.Output(t, terraformOptions, "eks_security_group_id")
	rdsSGID := terraform.Output(t, terraformOptions, "rds_security_group_id")
	redisSGID := terraform.Output(t, terraformOptions, "redis_security_group_id")

	// Log created resources
	tc.LogResource("VPC", vpcID, tc.TestPrefix+"-vpc")
	for i, subnetID := range publicSubnetIDs {
		tc.LogResource("Public Subnet", subnetID, fmt.Sprintf("%s-public-%d", tc.TestPrefix, i))
	}
	for i, subnetID := range privateSubnetIDs {
		tc.LogResource("Private Subnet", subnetID, fmt.Sprintf("%s-private-%d", tc.TestPrefix, i))
	}
	tc.LogResource("Internet Gateway", igwID, tc.TestPrefix+"-igw")
	for i, natID := range natGatewayIDs {
		tc.LogResource("NAT Gateway", natID, fmt.Sprintf("%s-nat-%d", tc.TestPrefix, i))
	}
	tc.LogResource("Security Group (ALB)", albSGID, tc.TestPrefix+"-sg-alb")
	tc.LogResource("Security Group (EKS)", eksSGID, tc.TestPrefix+"-sg-eks")
	tc.LogResource("Security Group (RDS)", rdsSGID, tc.TestPrefix+"-sg-rds")
	tc.LogResource("Security Group (Redis)", redisSGID, tc.TestPrefix+"-sg-redis")

	// Write resource log
	tc.WriteResourceLog("../../../reports/infra/resources-created.txt")

	// ========== VALIDATION TESTS ==========
	ctx := context.Background()

	// Test 1: Verify VPC exists and has correct CIDR
	t.Run("VPC_Exists_With_Correct_CIDR", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeVpcs(ctx, &ec2.DescribeVpcsInput{
			VpcIds: []string{vpcID},
		})
		require.NoError(t, err, "Failed to describe VPC")
		require.Len(t, result.Vpcs, 1, "Expected exactly one VPC")

		vpc := result.Vpcs[0]
		assert.Equal(t, "10.99.0.0/16", *vpc.CidrBlock, "VPC CIDR should match")
		assert.Equal(t, vpcCIDR, *vpc.CidrBlock, "VPC CIDR output should match actual")
		t.Logf("VPC %s verified with CIDR %s", vpcID, *vpc.CidrBlock)
	})

	// Test 2: Verify public subnets exist in correct AZs
	t.Run("Public_Subnets_Exist", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
			SubnetIds: publicSubnetIDs,
		})
		require.NoError(t, err, "Failed to describe public subnets")
		assert.Len(t, result.Subnets, 2, "Expected 2 public subnets")

		for _, subnet := range result.Subnets {
			t.Logf("Public subnet %s in AZ %s", *subnet.SubnetId, *subnet.AvailabilityZone)
			assert.True(t, *subnet.MapPublicIpOnLaunch, "Public subnet should have map_public_ip_on_launch=true")

			// Verify EKS tags
			tags := EC2TagsToMap(subnet.Tags)
			assert.Equal(t, "1", tags["kubernetes.io/role/elb"], "Public subnet should have ELB tag")
		}
	})

	// Test 3: Verify private subnets exist in correct AZs
	t.Run("Private_Subnets_Exist", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
			SubnetIds: privateSubnetIDs,
		})
		require.NoError(t, err, "Failed to describe private subnets")
		assert.Len(t, result.Subnets, 2, "Expected 2 private subnets")

		for _, subnet := range result.Subnets {
			t.Logf("Private subnet %s in AZ %s", *subnet.SubnetId, *subnet.AvailabilityZone)
			assert.False(t, *subnet.MapPublicIpOnLaunch, "Private subnet should have map_public_ip_on_launch=false")

			// Verify EKS tags
			tags := EC2TagsToMap(subnet.Tags)
			assert.Equal(t, "1", tags["kubernetes.io/role/internal-elb"], "Private subnet should have internal-ELB tag")
		}
	})

	// Test 4: Verify Internet Gateway is attached
	t.Run("Internet_Gateway_Attached", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeInternetGateways(ctx, &ec2.DescribeInternetGatewaysInput{
			InternetGatewayIds: []string{igwID},
		})
		require.NoError(t, err, "Failed to describe Internet Gateway")
		require.Len(t, result.InternetGateways, 1, "Expected exactly one IGW")

		igw := result.InternetGateways[0]
		require.Len(t, igw.Attachments, 1, "IGW should have one attachment")
		assert.Equal(t, vpcID, *igw.Attachments[0].VpcId, "IGW should be attached to our VPC")
		t.Logf("Internet Gateway %s is attached to VPC %s", igwID, vpcID)
	})

	// Test 5: Verify NAT Gateways are running
	t.Run("NAT_Gateways_Available", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeNatGateways(ctx, &ec2.DescribeNatGatewaysInput{
			NatGatewayIds: natGatewayIDs,
		})
		require.NoError(t, err, "Failed to describe NAT Gateways")
		assert.Len(t, result.NatGateways, 2, "Expected 2 NAT Gateways")

		for _, nat := range result.NatGateways {
			t.Logf("NAT Gateway %s state: %s", *nat.NatGatewayId, nat.State)
			assert.Equal(t, ec2types.NatGatewayStateAvailable, nat.State, "NAT Gateway should be available")
		}
	})

	// Test 6: Verify ALB Security Group has correct rules
	t.Run("ALB_Security_Group_Rules", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{albSGID},
		})
		require.NoError(t, err, "Failed to describe ALB Security Group")
		require.Len(t, result.SecurityGroups, 1, "Expected exactly one SG")

		sg := result.SecurityGroups[0]
		t.Logf("ALB Security Group %s has %d ingress rules", albSGID, len(sg.IpPermissions))

		// Check for HTTPS (443) and HTTP (80) ingress rules
		hasHTTPS := false
		hasHTTP := false
		for _, perm := range sg.IpPermissions {
			if perm.FromPort != nil {
				if *perm.FromPort == 443 {
					hasHTTPS = true
					t.Log("Found HTTPS (443) ingress rule")
				}
				if *perm.FromPort == 80 {
					hasHTTP = true
					t.Log("Found HTTP (80) ingress rule")
				}
			}
		}
		assert.True(t, hasHTTPS, "ALB SG should allow HTTPS (443)")
		assert.True(t, hasHTTP, "ALB SG should allow HTTP (80)")
	})

	// Test 7: Verify EKS Security Group rules
	t.Run("EKS_Security_Group_Rules", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{eksSGID},
		})
		require.NoError(t, err, "Failed to describe EKS Security Group")
		require.Len(t, result.SecurityGroups, 1, "Expected exactly one SG")

		sg := result.SecurityGroups[0]
		t.Logf("EKS Security Group %s has %d ingress rules", eksSGID, len(sg.IpPermissions))

		// Check for port 8080 from ALB
		has8080FromALB := false
		for _, perm := range sg.IpPermissions {
			if perm.FromPort != nil && *perm.FromPort == 8080 {
				for _, group := range perm.UserIdGroupPairs {
					if group.GroupId != nil && *group.GroupId == albSGID {
						has8080FromALB = true
						t.Log("Found port 8080 ingress from ALB SG")
						break
					}
				}
			}
		}
		assert.True(t, has8080FromALB, "EKS SG should allow 8080 from ALB SG")
	})

	// Test 8: Verify RDS Security Group rules
	t.Run("RDS_Security_Group_Rules", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{rdsSGID},
		})
		require.NoError(t, err, "Failed to describe RDS Security Group")
		require.Len(t, result.SecurityGroups, 1, "Expected exactly one SG")

		sg := result.SecurityGroups[0]
		t.Logf("RDS Security Group %s has %d ingress rules", rdsSGID, len(sg.IpPermissions))

		// Check for PostgreSQL (5432) from EKS
		has5432FromEKS := false
		for _, perm := range sg.IpPermissions {
			if perm.FromPort != nil && *perm.FromPort == 5432 {
				for _, group := range perm.UserIdGroupPairs {
					if group.GroupId != nil && *group.GroupId == eksSGID {
						has5432FromEKS = true
						t.Log("Found PostgreSQL (5432) ingress from EKS SG")
						break
					}
				}
			}
		}
		assert.True(t, has5432FromEKS, "RDS SG should allow 5432 from EKS SG")
	})

	// Test 9: Verify Redis Security Group rules
	t.Run("Redis_Security_Group_Rules", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{
			GroupIds: []string{redisSGID},
		})
		require.NoError(t, err, "Failed to describe Redis Security Group")
		require.Len(t, result.SecurityGroups, 1, "Expected exactly one SG")

		sg := result.SecurityGroups[0]
		t.Logf("Redis Security Group %s has %d ingress rules", redisSGID, len(sg.IpPermissions))

		// Check for Redis (6379) from EKS
		has6379FromEKS := false
		for _, perm := range sg.IpPermissions {
			if perm.FromPort != nil && *perm.FromPort == 6379 {
				for _, group := range perm.UserIdGroupPairs {
					if group.GroupId != nil && *group.GroupId == eksSGID {
						has6379FromEKS = true
						t.Log("Found Redis (6379) ingress from EKS SG")
						break
					}
				}
			}
		}
		assert.True(t, has6379FromEKS, "Redis SG should allow 6379 from EKS SG")
	})

	// Test 10: Verify VPC tags
	t.Run("VPC_Tags", func(t *testing.T) {
		result, err := tc.AWS.EC2.DescribeVpcs(ctx, &ec2.DescribeVpcsInput{
			VpcIds: []string{vpcID},
		})
		require.NoError(t, err, "Failed to describe VPC")

		tags := EC2TagsToMap(result.Vpcs[0].Tags)
		assert.Equal(t, "BedrockGateway", tags["Project"], "VPC should have Project tag")
		assert.Equal(t, "test", tags["Environment"], "VPC should have Environment tag")
		assert.Equal(t, "terraform", tags["ManagedBy"], "VPC should have ManagedBy tag")
		assert.Equal(t, "platform-team", tags["Owner"], "VPC should have Owner tag")
		t.Logf("VPC tags validated: %+v", tags)
	})

	t.Log("=== All networking integration tests passed ===")
}
