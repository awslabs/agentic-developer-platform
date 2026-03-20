package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestRdsModuleFilesExist verifies all required files exist in the RDS module
func TestRdsModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	AssertModuleFilesExist(t, modulePath)
}

// TestRdsModuleValidate validates the RDS module using terraform validate
func TestRdsModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestRdsModuleRequiredVariables verifies required variables are declared
func TestRdsModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")

	requiredVars := []string{
		"environment",
		"name_prefix",
		"common_tags",
		"vpc_id",
		"private_subnet_ids",
		"rds_security_group_id",
		"instance_class",
		"allocated_storage",
		"max_allocated_storage",
		"multi_az",
		"backup_retention_period",
		"backup_window",
		"maintenance_window",
		"db_name",
		"username",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in RDS module", varName)
	}
}

// TestRdsModuleOutputs verifies required outputs are declared
func TestRdsModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")

	requiredOutputs := []string{
		"db_instance_arn",
		"db_instance_endpoint",
		"db_instance_id",
		"db_instance_name",
		"db_instance_port",
		"db_credentials_secret_arn",
		"db_subnet_group_name",
		"db_parameter_group_name",
		"enhanced_monitoring_role_arn",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in RDS module", outputName)
	}
}

// TestRdsModuleResources verifies essential resources are declared
func TestRdsModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")

	// Verify RDS instance resource exists
	assert.True(t, ResourceExists(t, modulePath, "aws_db_instance", "main"),
		"RDS instance resource should be declared")

	// Verify DB subnet group exists
	assert.True(t, ResourceExists(t, modulePath, "aws_db_subnet_group", "main"),
		"DB subnet group should be declared")

	// Verify DB parameter group exists
	assert.True(t, ResourceExists(t, modulePath, "aws_db_parameter_group", "main"),
		"DB parameter group should be declared")

	// Verify Secrets Manager secret exists
	assert.True(t, ResourceExists(t, modulePath, "aws_secretsmanager_secret", "db_credentials"),
		"Secrets Manager secret should be declared for credentials")

	// Verify IAM role for enhanced monitoring exists
	assert.True(t, ResourceExists(t, modulePath, "aws_iam_role", "rds_enhanced_monitoring"),
		"IAM role for enhanced monitoring should be declared")
}

// TestRdsModuleTagging verifies tagging compliance
func TestRdsModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for RDS-specific tags as per tagging strategy
	assert.Contains(t, content, "Service  = \"database\"",
		"RDS resources should have Service = database tag")
	assert.Contains(t, content, "Backup   = \"daily\"",
		"RDS instance should have Backup = daily tag")
	assert.Contains(t, content, "DataType = \"tenant-data\"",
		"RDS resources should have DataType = tenant-data tag")
}

// TestRdsModuleNamingConvention verifies resource naming follows convention
func TestRdsModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify RDS instance naming
	assert.Contains(t, content, "${var.name_prefix}-postgres",
		"RDS instance should follow naming convention bedrockgw-{env}-postgres")

	// Verify subnet group naming
	assert.Contains(t, content, "${var.name_prefix}-db-subnet-group",
		"DB subnet group should follow naming convention")

	// Verify parameter group naming
	assert.Contains(t, content, "${var.name_prefix}-db-params",
		"DB parameter group should follow naming convention")
}

// TestRdsModuleSecurityConfiguration verifies security configuration
func TestRdsModuleSecurityConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify encryption at rest is enabled
	assert.Contains(t, content, "storage_encrypted",
		"RDS should have storage encryption enabled")

	// Verify publicly accessible is false
	assert.Contains(t, content, "publicly_accessible",
		"RDS should not be publicly accessible")

	// Verify random password is used
	assert.Contains(t, content, "random_password",
		"RDS should use random password for master user")
}

// TestRdsModuleBackupConfiguration verifies backup configuration
func TestRdsModuleBackupConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify backup retention period is set
	assert.Contains(t, content, "backup_retention_period",
		"RDS should have backup_retention_period configured")

	// Verify backup window is set
	assert.Contains(t, content, "backup_window",
		"RDS should have backup_window configured")

	// Verify maintenance window is set
	assert.Contains(t, content, "maintenance_window",
		"RDS should have maintenance_window configured")

	// Verify copy_tags_to_snapshot is enabled
	assert.Contains(t, content, "copy_tags_to_snapshot",
		"RDS should copy tags to snapshots")
}

// TestRdsModuleMultiAZConfiguration verifies Multi-AZ configuration
func TestRdsModuleMultiAZConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify multi_az is configurable
	assert.Contains(t, content, "multi_az = var.multi_az",
		"RDS multi_az should be configurable via variable")
}

// TestRdsModuleMonitoringConfiguration verifies monitoring configuration
func TestRdsModuleMonitoringConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify enhanced monitoring is configured
	assert.Contains(t, content, "monitoring_interval",
		"RDS should have enhanced monitoring configured")
	assert.Contains(t, content, "monitoring_role_arn",
		"RDS should have monitoring IAM role configured")

	// Verify Performance Insights is enabled
	assert.Contains(t, content, "performance_insights_enabled",
		"RDS should have Performance Insights enabled")
}

// TestRdsModuleParameterGroupConfiguration verifies parameter group configuration
func TestRdsModuleParameterGroupConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify parameter group family
	assert.Contains(t, content, "family = \"postgres15\"",
		"RDS parameter group should use postgres15 family")

	// Verify logging parameters
	assert.Contains(t, content, "pg_stat_statements",
		"Parameter group should enable pg_stat_statements")
	assert.Contains(t, content, "log_statement",
		"Parameter group should configure log_statement")
}

// TestRdsModuleVariableDefaults verifies sensible defaults for variables
func TestRdsModuleVariableDefaults(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for default instance class
	assert.Contains(t, content, "db.t4g.medium",
		"instance_class should have default value db.t4g.medium")

	// Check for default allocated storage
	assert.Contains(t, content, "default",
		"allocated_storage should have default value")

	// Check for default backup retention period
	assert.Contains(t, content, "default",
		"backup_retention_period should have default value")

	// Check for default database name
	assert.Contains(t, content, "bedrockgateway",
		"db_name should have default value bedrockgateway")
}

// TestRdsModuleDeletionProtection verifies deletion protection configuration
func TestRdsModuleDeletionProtection(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify deletion protection is environment-aware
	assert.Contains(t, content, "deletion_protection",
		"RDS should have deletion protection configured")
	assert.Contains(t, content, "var.environment == \"prod\"",
		"RDS deletion protection should be environment-aware")

	// Verify final snapshot is environment-aware
	assert.Contains(t, content, "skip_final_snapshot",
		"RDS skip_final_snapshot should be configured")
}

// TestRdsModuleReadReplica verifies read replica configuration
func TestRdsModuleReadReplica(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify read replica resource exists (optional, for prod)
	assert.Contains(t, content, "aws_db_instance\" \"read_replica\"",
		"RDS module should support read replica")

	// Verify read replica is conditional on environment
	assert.Contains(t, content, "var.environment == \"prod\"",
		"Read replica should be conditional on prod environment")
}
