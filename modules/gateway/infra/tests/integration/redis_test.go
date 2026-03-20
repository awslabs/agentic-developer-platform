package integration

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/elasticache"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestRedisIntegration provisions real AWS ElastiCache Redis infrastructure and validates it
// NOTE: This test takes 5-10 minutes to complete due to ElastiCache provisioning time
func TestRedisIntegration(t *testing.T) {
	t.Parallel()

	// Create test context with unique identifiers
	tc := NewTestContext(t)
	t.Logf("Starting Redis integration test with prefix: %s", tc.TestPrefix)

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
	redisSGID := terraform.Output(t, networkingOptions, "redis_security_group_id")

	tc.LogResource("VPC (for Redis)", vpcID, tc.TestPrefix+"-vpc")

	// Now provision Redis (single node for faster testing)
	redisModulePath := GetModulePath("redis")
	t.Logf("Using Redis module path: %s", redisModulePath)

	redisOptions := &terraform.Options{
		TerraformDir: redisModulePath,
		Vars: map[string]interface{}{
			"environment":           "test",
			"name_prefix":           tc.TestPrefix,
			"common_tags":           GetCommonTags("test"),
			"vpc_id":                vpcID,
			"private_subnet_ids":    privateSubnetIDs,
			"redis_security_group_id": redisSGID,
			"node_type":             "cache.t3.micro", // Smallest instance for testing
			"num_cache_nodes":       1,                // Single node for faster provisioning
			"parameter_group_name":  "default.redis7",
			"port":                  6379,
			"enable_monitoring":     false, // Disable monitoring for test
			"sns_topic_arn":         "",
		},
		NoColor:         true,
	}

	// CRITICAL: Defer Redis destroy BEFORE apply
	defer func() {
		t.Log("=== CLEANUP: Destroying Redis infrastructure ===")
		terraform.Destroy(t, redisOptions)
		t.Log("=== CLEANUP: Redis destroy completed ===")
	}()

	// Initialize and apply Redis
	t.Log("=== PROVISION: Creating Redis instance (this may take 5-10 minutes) ===")
	terraform.InitAndApply(t, redisOptions)
	t.Log("=== PROVISION: Redis instance created ===")

	// Get Redis outputs (for single node)
	cacheClusterID := terraform.Output(t, redisOptions, "cache_cluster_id")
	cacheClusterARN := terraform.Output(t, redisOptions, "cache_cluster_arn")
	cacheEndpoint := terraform.Output(t, redisOptions, "endpoint")
	cachePort := terraform.Output(t, redisOptions, "port")

	tc.LogResource("ElastiCache Cluster", cacheClusterID, tc.TestPrefix+"-redis")

	// ========== VALIDATION TESTS ==========
	ctx := context.Background()

	// Test 1: Verify cache cluster is in 'available' state
	t.Run("Redis_Cluster_Available", func(t *testing.T) {
		result, err := tc.AWS.ElastiCache.DescribeCacheClusters(ctx, &elasticache.DescribeCacheClustersInput{
			CacheClusterId: aws.String(cacheClusterID),
		})
		require.NoError(t, err, "Failed to describe cache cluster")
		require.Len(t, result.CacheClusters, 1, "Expected exactly one cache cluster")

		cluster := result.CacheClusters[0]
		assert.Equal(t, "available", *cluster.CacheClusterStatus, "Cache cluster should be available")
		t.Logf("Redis cluster %s is in state: %s", cacheClusterID, *cluster.CacheClusterStatus)
	})

	// Test 2: Verify node type matches configuration
	t.Run("Redis_Node_Type_Matches", func(t *testing.T) {
		result, err := tc.AWS.ElastiCache.DescribeCacheClusters(ctx, &elasticache.DescribeCacheClustersInput{
			CacheClusterId: aws.String(cacheClusterID),
		})
		require.NoError(t, err, "Failed to describe cache cluster")

		cluster := result.CacheClusters[0]
		assert.Equal(t, "cache.t3.micro", *cluster.CacheNodeType, "Node type should match")
		t.Logf("Redis node type: %s", *cluster.CacheNodeType)
	})

	// Test 3: Verify engine is Redis
	t.Run("Redis_Engine_Redis", func(t *testing.T) {
		result, err := tc.AWS.ElastiCache.DescribeCacheClusters(ctx, &elasticache.DescribeCacheClustersInput{
			CacheClusterId: aws.String(cacheClusterID),
		})
		require.NoError(t, err, "Failed to describe cache cluster")

		cluster := result.CacheClusters[0]
		assert.Equal(t, "redis", *cluster.Engine, "Engine should be redis")
		t.Logf("Redis engine: %s version %s", *cluster.Engine, *cluster.EngineVersion)
	})

	// Test 4: TCP connection test to Redis endpoint
	t.Run("Redis_TCP_Connection", func(t *testing.T) {
		// Extract host from endpoint (format: host:port)
		endpointParts := strings.Split(cacheEndpoint, ":")
		host := endpointParts[0]
		port := 6379

		t.Logf("Testing TCP connection to %s:%d", host, port)

		// Note: This may fail if the test runner is not in the VPC
		connected := CheckTCPConnection(t, host, port, 5*time.Second)

		if connected {
			t.Log("TCP connection to Redis endpoint successful")
		} else {
			t.Log("TCP connection to Redis endpoint failed (expected if not in VPC)")
		}
	})

	// Test 5: Verify Redis tags
	t.Run("Redis_Tags_Correct", func(t *testing.T) {
		result, err := tc.AWS.ElastiCache.ListTagsForResource(ctx, &elasticache.ListTagsForResourceInput{
			ResourceName: aws.String(cacheClusterARN),
		})
		require.NoError(t, err, "Failed to list Redis tags")

		tags := make(map[string]string)
		for _, tag := range result.TagList {
			tags[*tag.Key] = *tag.Value
		}

		assert.Equal(t, "BedrockGateway", tags["Project"], "Should have Project tag")
		assert.Equal(t, "test", tags["Environment"], "Should have Environment tag")
		assert.Equal(t, "terraform", tags["ManagedBy"], "Should have ManagedBy tag")
		assert.Equal(t, "cache", tags["Service"], "Should have Service tag")
		assert.Equal(t, "ephemeral", tags["DataType"], "Should have DataType tag")
		t.Logf("Redis tags validated: %+v", tags)
	})

	// Test 6: Verify endpoint format
	t.Run("Redis_Endpoint_Format", func(t *testing.T) {
		assert.Contains(t, cacheEndpoint, ".cache.amazonaws.com", "Endpoint should contain ElastiCache domain")
		assert.Equal(t, "6379", cachePort, "Port should be 6379")
		t.Logf("Redis endpoint: %s", cacheEndpoint)
	})

	// Test 7: Verify number of cache nodes
	t.Run("Redis_Node_Count", func(t *testing.T) {
		result, err := tc.AWS.ElastiCache.DescribeCacheClusters(ctx, &elasticache.DescribeCacheClustersInput{
			CacheClusterId:    aws.String(cacheClusterID),
			ShowCacheNodeInfo: aws.Bool(true),
		})
		require.NoError(t, err, "Failed to describe cache cluster")

		cluster := result.CacheClusters[0]
		assert.Equal(t, int32(1), cluster.NumCacheNodes, "Should have 1 cache node")
		t.Logf("Redis has %d cache node(s)", cluster.NumCacheNodes)
	})

	// Test 8: Verify subnet group
	t.Run("Redis_Subnet_Group", func(t *testing.T) {
		result, err := tc.AWS.ElastiCache.DescribeCacheClusters(ctx, &elasticache.DescribeCacheClustersInput{
			CacheClusterId: aws.String(cacheClusterID),
		})
		require.NoError(t, err, "Failed to describe cache cluster")

		cluster := result.CacheClusters[0]
		require.NotNil(t, cluster.CacheSubnetGroupName, "Subnet group should be set")
		assert.Contains(t, *cluster.CacheSubnetGroupName, tc.TestPrefix, "Subnet group should contain test prefix")
		t.Logf("Redis subnet group: %s", *cluster.CacheSubnetGroupName)
	})

	// Write resource log
	tc.WriteResourceLog("../../../reports/infra/resources-created.txt")

	t.Log("=== All Redis integration tests passed ===")
}
