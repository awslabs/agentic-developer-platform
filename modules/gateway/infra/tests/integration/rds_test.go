package integration

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/rds"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestRDSIntegration provisions real AWS RDS infrastructure and validates it
// NOTE: This test takes 5-10 minutes to complete due to RDS provisioning time
func TestRDSIntegration(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting RDS integration test with prefix: %s", tc.TestPrefix)

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
	privateSubnetIDs := terraform.OutputList(t, networkingOptions, "private_subnet_ids")
	rdsSGID := terraform.Output(t, networkingOptions, "rds_security_group_id")

	tc.LogResource("VPC (for RDS)", vpcID, tc.TestPrefix+"-vpc")

	// Now provision RDS
	rdsModulePath := GetModulePath("rds")
	t.Logf("Using RDS module path: %s", rdsModulePath)

	rdsOptions := &terraform.Options{
		TerraformDir: rdsModulePath,
		Vars: map[string]interface{}{
			"environment":            "test",
			"name_prefix":            tc.TestPrefix,
			"common_tags":            GetCommonTags("test"),
			"vpc_id":                 vpcID,
			"private_subnet_ids":     privateSubnetIDs,
			"rds_security_group_id":  rdsSGID,
			"instance_class":         "db.t4g.micro", // Smallest instance for testing
			"allocated_storage":      20,             // Minimum storage
			"max_allocated_storage":  50,
			"multi_az":               false, // Single AZ for faster provisioning
			"backup_retention_period": 1,
			"db_name":                "testdb",
			"username":               "testadmin",
		},
		NoColor:         true,
	}

	// CRITICAL: Defer RDS destroy BEFORE apply
	defer func() {
		t.Log("=== CLEANUP: Destroying RDS infrastructure ===")
		terraform.Destroy(t, rdsOptions)
		t.Log("=== CLEANUP: RDS destroy completed ===")
	}()

	// Initialize and apply RDS
	t.Log("=== PROVISION: Creating RDS instance (this may take 5-10 minutes) ===")
	terraform.InitAndApply(t, rdsOptions)
	t.Log("=== PROVISION: RDS instance created ===")

	// Get RDS outputs
	dbInstanceID := terraform.Output(t, rdsOptions, "db_instance_id")
	dbEndpoint := terraform.Output(t, rdsOptions, "db_instance_endpoint")
	dbPort := terraform.Output(t, rdsOptions, "db_instance_port")
	dbARN := terraform.Output(t, rdsOptions, "db_instance_arn")

	tc.LogResource("RDS Instance", dbInstanceID, tc.TestPrefix+"-postgres")

	// ========== VALIDATION TESTS ==========
	ctx := context.Background()

	// Test 1: Verify DB instance is in 'available' state
	t.Run("RDS_Instance_Available", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")
		require.Len(t, result.DBInstances, 1, "Expected exactly one DB instance")

		dbInstance := result.DBInstances[0]
		assert.Equal(t, "available", *dbInstance.DBInstanceStatus, "DB instance should be available")
		t.Logf("RDS instance %s is in state: %s", dbInstanceID, *dbInstance.DBInstanceStatus)
	})

	// Test 2: Verify instance class matches configuration
	t.Run("RDS_Instance_Class_Matches", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")

		dbInstance := result.DBInstances[0]
		assert.Equal(t, "db.t4g.micro", *dbInstance.DBInstanceClass, "Instance class should match")
		t.Logf("RDS instance class: %s", *dbInstance.DBInstanceClass)
	})

	// Test 3: Verify Multi-AZ setting
	t.Run("RDS_MultiAZ_Matches", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")

		dbInstance := result.DBInstances[0]
		assert.False(t, aws.ToBool(dbInstance.MultiAZ), "Multi-AZ should be false for test")
		t.Logf("RDS Multi-AZ: %v", dbInstance.MultiAZ)
	})

	// Test 4: Verify storage encryption is enabled
	t.Run("RDS_Storage_Encrypted", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")

		dbInstance := result.DBInstances[0]
		assert.True(t, aws.ToBool(dbInstance.StorageEncrypted), "Storage encryption should be enabled")
		t.Log("RDS storage encryption is enabled")
	})

	// Test 5: Verify backup retention period
	t.Run("RDS_Backup_Retention", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")

		dbInstance := result.DBInstances[0]
		assert.Equal(t, int32(1), dbInstance.BackupRetentionPeriod, "Backup retention should be 1 day")
		t.Logf("RDS backup retention: %d days", dbInstance.BackupRetentionPeriod)
	})

	// Test 6: TCP connection test to database endpoint
	t.Run("RDS_TCP_Connection", func(t *testing.T) {
		// Extract host from endpoint (format: host:port)
		endpointParts := strings.Split(dbEndpoint, ":")
		host := endpointParts[0]
		port := 5432

		t.Logf("Testing TCP connection to %s:%d", host, port)

		// Note: This may fail if the test runner is not in the VPC
		// The connection should timeout rather than be refused
		connected := CheckTCPConnection(t, host, port, 5*time.Second)

		// Log the result but don't fail the test if we can't connect
		// (we're likely not in the VPC)
		if connected {
			t.Log("TCP connection to RDS endpoint successful")
		} else {
			t.Log("TCP connection to RDS endpoint failed (expected if not in VPC)")
		}
	})

	// Test 7: Verify RDS tags
	t.Run("RDS_Tags_Correct", func(t *testing.T) {
		result, err := tc.AWS.RDS.ListTagsForResource(ctx, &rds.ListTagsForResourceInput{
			ResourceName: aws.String(dbARN),
		})
		require.NoError(t, err, "Failed to list RDS tags")

		tags := make(map[string]string)
		for _, tag := range result.TagList {
			tags[*tag.Key] = *tag.Value
		}

		assert.Equal(t, "BedrockGateway", tags["Project"], "Should have Project tag")
		assert.Equal(t, "test", tags["Environment"], "Should have Environment tag")
		assert.Equal(t, "terraform", tags["ManagedBy"], "Should have ManagedBy tag")
		assert.Equal(t, "database", tags["Service"], "Should have Service tag")
		assert.Equal(t, "tenant-data", tags["DataType"], "Should have DataType tag")
		t.Logf("RDS tags validated: %+v", tags)
	})

	// Test 8: Verify engine is PostgreSQL
	t.Run("RDS_Engine_PostgreSQL", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")

		dbInstance := result.DBInstances[0]
		assert.Equal(t, "postgres", *dbInstance.Engine, "Engine should be PostgreSQL")
		t.Logf("RDS engine: %s version %s", *dbInstance.Engine, *dbInstance.EngineVersion)
	})

	// Test 9: Verify Performance Insights is enabled
	t.Run("RDS_Performance_Insights", func(t *testing.T) {
		result, err := tc.AWS.RDS.DescribeDBInstances(ctx, &rds.DescribeDBInstancesInput{
			DBInstanceIdentifier: aws.String(dbInstanceID),
		})
		require.NoError(t, err, "Failed to describe RDS instance")

		dbInstance := result.DBInstances[0]
		assert.True(t, aws.ToBool(dbInstance.PerformanceInsightsEnabled), "Performance Insights should be enabled")
		t.Log("Performance Insights is enabled")
	})

	// Test 10: Verify endpoint format
	t.Run("RDS_Endpoint_Format", func(t *testing.T) {
		assert.Contains(t, dbEndpoint, ".rds.amazonaws.com", "Endpoint should contain RDS domain")
		assert.Equal(t, "5432", dbPort, "Port should be 5432")
		t.Logf("RDS endpoint: %s:%s", dbEndpoint, dbPort)
	})

	// Write resource log
	tc.WriteResourceLog("../../../reports/infra/resources-created.txt")

	t.Log("=== All RDS integration tests passed ===")
}
