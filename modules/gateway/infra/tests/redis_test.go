package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestRedisModuleFilesExist verifies all required files exist in the Redis module
func TestRedisModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	AssertModuleFilesExist(t, modulePath)
}

// TestRedisModuleValidate validates the Redis module using terraform validate
func TestRedisModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestRedisModuleRequiredVariables verifies required variables are declared
func TestRedisModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")

	requiredVars := []string{
		"environment",
		"name_prefix",
		"common_tags",
		"vpc_id",
		"private_subnet_ids",
		"redis_security_group_id",
		"node_type",
		"num_cache_nodes",
		"parameter_group_name",
		"port",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in Redis module", varName)
	}
}

// TestRedisModuleOutputs verifies required outputs are declared
func TestRedisModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")

	requiredOutputs := []string{
		"cache_nodes",
		"port",
		"endpoint",
		"subnet_group_name",
		"parameter_group_name",
		"auth_token_secret_arn",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in Redis module", outputName)
	}
}

// TestRedisModuleResources verifies essential resources are declared
func TestRedisModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")

	// Verify ElastiCache subnet group exists
	assert.True(t, ResourceExists(t, modulePath, "aws_elasticache_subnet_group", "main"),
		"ElastiCache subnet group should be declared")

	// Verify ElastiCache parameter group exists
	assert.True(t, ResourceExists(t, modulePath, "aws_elasticache_parameter_group", "main"),
		"ElastiCache parameter group should be declared")

	// Verify Secrets Manager secret exists for auth token
	assert.True(t, ResourceExists(t, modulePath, "aws_secretsmanager_secret", "redis_auth"),
		"Secrets Manager secret should be declared for Redis auth token")
}

// TestRedisModuleTagging verifies tagging compliance
func TestRedisModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for Redis-specific tags as per tagging strategy
	assert.Contains(t, content, "Service  = \"cache\"",
		"Redis resources should have Service = cache tag")
	assert.Contains(t, content, "DataType = \"ephemeral\"",
		"Redis resources should have DataType = ephemeral tag")
}

// TestRedisModuleNamingConvention verifies resource naming follows convention
func TestRedisModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify subnet group naming
	assert.Contains(t, content, "${var.name_prefix}-cache-subnet",
		"ElastiCache subnet group should follow naming convention")

	// Verify parameter group naming
	assert.Contains(t, content, "${var.name_prefix}-cache-params",
		"ElastiCache parameter group should follow naming convention")
}

// TestRedisModuleSecurityConfiguration verifies security configuration
func TestRedisModuleSecurityConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify encryption at rest is enabled
	assert.Contains(t, content, "at_rest_encryption_enabled = true",
		"Redis replication group should have at-rest encryption enabled")

	// Verify encryption in transit is enabled
	assert.Contains(t, content, "transit_encryption_enabled = true",
		"Redis replication group should have transit encryption enabled")

	// Verify auth token is used
	assert.Contains(t, content, "auth_token",
		"Redis should use auth token for authentication")
}

// TestRedisModuleClusterConfiguration verifies cluster configuration
func TestRedisModuleClusterConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify single node cluster is supported
	assert.Contains(t, content, "aws_elasticache_cluster",
		"Module should support single node ElastiCache cluster")

	// Verify replication group is supported
	assert.Contains(t, content, "aws_elasticache_replication_group",
		"Module should support ElastiCache replication group")

	// Verify automatic failover is configurable
	assert.Contains(t, content, "automatic_failover_enabled",
		"Redis should have automatic failover configuration")

	// Verify multi-AZ is configurable
	assert.Contains(t, content, "multi_az_enabled",
		"Redis should have multi-AZ configuration")
}

// TestRedisModuleBackupConfiguration verifies backup configuration
func TestRedisModuleBackupConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify snapshot retention is configured
	assert.Contains(t, content, "snapshot_retention_limit",
		"Redis should have snapshot retention configured")

	// Verify snapshot window is set
	assert.Contains(t, content, "snapshot_window",
		"Redis should have snapshot window configured")

	// Verify maintenance window is set
	assert.Contains(t, content, "maintenance_window",
		"Redis should have maintenance window configured")
}

// TestRedisModuleVariableDefaults verifies sensible defaults for variables
func TestRedisModuleVariableDefaults(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for default node type
	assert.Contains(t, content, "cache.t3.micro",
		"node_type should have default value cache.t3.micro")

	// Check for default port
	assert.Contains(t, content, "default     = 6379",
		"port should have default value 6379")

	// Check for default num_cache_nodes
	assert.Contains(t, content, "default     = 1",
		"num_cache_nodes should have default value 1")

	// Check for default parameter group name
	assert.Contains(t, content, "default.redis7",
		"parameter_group_name should have default value")
}

// TestRedisModuleNodeCountValidation verifies num_cache_nodes has validation
func TestRedisModuleNodeCountValidation(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for num_cache_nodes validation
	assert.Contains(t, content, "var.num_cache_nodes >= 1",
		"num_cache_nodes should have minimum validation")
	assert.Contains(t, content, "var.num_cache_nodes <= 6",
		"num_cache_nodes should have maximum validation")
}

// TestRedisModuleParameterGroupConfiguration verifies parameter group configuration
func TestRedisModuleParameterGroupConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify parameter group family
	assert.Contains(t, content, "family = \"redis7.x\"",
		"Redis parameter group should use redis7.x family")

	// Verify maxmemory-policy is configured
	assert.Contains(t, content, "maxmemory-policy",
		"Redis parameter group should configure maxmemory-policy")
	assert.Contains(t, content, "allkeys-lru",
		"Redis should use allkeys-lru eviction policy")
}

// TestRedisModuleMonitoring verifies monitoring configuration
func TestRedisModuleMonitoring(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify CloudWatch alarms are configured
	assert.Contains(t, content, "aws_cloudwatch_metric_alarm",
		"Redis module should configure CloudWatch alarms")

	// Verify CPU alarm exists
	assert.Contains(t, content, "redis_cpu",
		"Redis module should have CPU utilization alarm")

	// Verify memory alarm exists
	assert.Contains(t, content, "redis_memory",
		"Redis module should have memory utilization alarm")
}

// TestRedisModuleConditionalResources verifies conditional resource creation
func TestRedisModuleConditionalResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify single node vs cluster is conditional
	assert.Contains(t, content, "var.num_cache_nodes == 1",
		"Single node cluster should be conditional on num_cache_nodes == 1")
	assert.Contains(t, content, "var.num_cache_nodes > 1",
		"Replication group should be conditional on num_cache_nodes > 1")
}

// TestRedisModuleOutputsConditional verifies conditional outputs
func TestRedisModuleOutputsConditional(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	outputsPath := filepath.Join(modulePath, "outputs.tf")
	content := ReadTerraformFile(t, outputsPath)

	// Verify outputs handle both single node and replication group
	assert.Contains(t, content, "var.num_cache_nodes == 1",
		"Outputs should handle single node cluster")
	assert.Contains(t, content, "var.num_cache_nodes > 1",
		"Outputs should handle replication group")
}
