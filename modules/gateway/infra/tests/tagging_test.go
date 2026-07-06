package test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
)

// TestTaggingStrategyDefaultTags verifies default_tags are configured in main.tf
func TestTaggingStrategyDefaultTags(t *testing.T) {
	t.Parallel()

	// Read the main infrastructure main.tf file
	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}

	mainTfPath := filepath.Join(testDir, "..", "main.tf")
	content := ReadTerraformFile(t, mainTfPath)

	// Verify default_tags block exists
	assert.Contains(t, content, "default_tags",
		"Main terraform config should have default_tags in AWS provider")

	// Verify all required tags are present in default_tags (unified ADP schema,
	// docs/tagging-and-observability.md §2.1/§2.4)
	requiredTags := []string{
		"Project",
		"Environment",
		"Module",
		"ManagedBy",
		"Owner",
		"CostCenter",
	}

	for _, tag := range requiredTags {
		assert.Contains(t, content, tag,
			"default_tags should include %s tag as per tagging strategy", tag)
	}

	// Verify tag values (#888: Project unified to "adp", Module added,
	// Owner is the module-specific gateway-team)
	assert.Contains(t, content, "Project     = \"adp\"",
		"Project tag should be adp (unified across all modules)")
	assert.Contains(t, content, "Module      = \"gateway\"",
		"Module tag should be gateway")
	assert.Contains(t, content, "ManagedBy   = \"terraform\"",
		"ManagedBy tag should be terraform")
	assert.Contains(t, content, "Owner       = \"gateway-team\"",
		"Owner tag should be gateway-team")
}

// TestTaggingStrategyCommonTagsVariable verifies common_tags is passed to modules
func TestTaggingStrategyCommonTagsVariable(t *testing.T) {
	t.Parallel()

	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}

	mainTfPath := filepath.Join(testDir, "..", "main.tf")
	content := ReadTerraformFile(t, mainTfPath)

	// Verify common_tags local variable is defined
	assert.Contains(t, content, "common_tags",
		"Main terraform config should define common_tags local")

	// Verify common_tags is passed to all modules
	modules := []string{
		"networking",
		"eks",
		"rds",
		"redis",
		"alb",
		"ecr",
		"iam",
	}

	for _, mod := range modules {
		// Check that each module receives common_tags
		modulePattern := "module \"" + mod + "\""
		assert.Contains(t, content, modulePattern,
			"Module %s should be declared in main.tf", mod)
	}

	// Verify common_tags = local.common_tags pattern is used
	assert.Contains(t, content, "common_tags",
		"Modules should reference common_tags")
	assert.Contains(t, content, "local.common_tags",
		"Modules should receive common_tags from local.common_tags")
}

// TestTaggingStrategyNetworkingModule verifies networking module tagging
func TestTaggingStrategyNetworkingModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "networking")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"Networking module should merge common_tags with resource-specific tags")

	// Verify EKS-specific tags for subnets
	assert.Contains(t, content, "kubernetes.io/role/elb",
		"Public subnets should have EKS ELB tag")
	assert.Contains(t, content, "kubernetes.io/role/internal-elb",
		"Private subnets should have EKS internal ELB tag")

	// Verify EKS cluster tag on security group
	assert.Contains(t, content, "kubernetes.io/cluster/",
		"EKS security group should have cluster ownership tag")
}

// TestTaggingStrategyEKSModule verifies EKS module tagging
func TestTaggingStrategyEKSModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "eks")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"EKS module should merge common_tags with resource-specific tags")

	// Verify kubernetes cluster tag
	assert.Contains(t, content, "kubernetes.io/cluster/",
		"EKS cluster should have kubernetes cluster ownership tag")
}

// TestTaggingStrategyRDSModule verifies RDS module tagging
func TestTaggingStrategyRDSModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "rds")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"RDS module should merge common_tags with resource-specific tags")

	// Verify RDS-specific tags as per tagging strategy
	assert.Contains(t, content, "Service  = \"database\"",
		"RDS should have Service = database tag")
	assert.Contains(t, content, "Backup   = \"daily\"",
		"RDS should have Backup = daily tag")
	assert.Contains(t, content, "DataType = \"tenant-data\"",
		"RDS should have DataType = tenant-data tag")
}

// TestTaggingStrategyRedisModule verifies Redis module tagging
func TestTaggingStrategyRedisModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "redis")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"Redis module should merge common_tags with resource-specific tags")

	// Verify Redis-specific tags as per tagging strategy
	assert.Contains(t, content, "Service  = \"cache\"",
		"Redis should have Service = cache tag")
	assert.Contains(t, content, "DataType = \"ephemeral\"",
		"Redis should have DataType = ephemeral tag")
}

// TestTaggingStrategyALBModule verifies ALB module tagging
func TestTaggingStrategyALBModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"ALB module should merge common_tags with resource-specific tags")

	// Verify ALB-specific tags as per tagging strategy
	assert.Contains(t, content, "Service = \"load-balancer\"",
		"ALB should have Service = load-balancer tag")
	assert.Contains(t, content, "Public  = \"true\"",
		"ALB should have Public = true tag")
}

// TestTaggingStrategyECRModule verifies ECR module tagging
func TestTaggingStrategyECRModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "ecr")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"ECR module should merge common_tags with resource-specific tags")

	// Verify ECR-specific tags as per tagging strategy
	assert.Contains(t, content, "Service = \"container-registry\"",
		"ECR should have Service = container-registry tag")
}

// TestTaggingStrategyIAMModule verifies IAM module tagging
func TestTaggingStrategyIAMModule(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "iam")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify merge pattern for tags
	assert.Contains(t, content, "merge(var.common_tags",
		"IAM module should merge common_tags with resource-specific tags")

	// Verify IAM-specific tags as per tagging strategy
	assert.Contains(t, content, "Service = \"iam\"",
		"IAM should have Service = iam tag")
	assert.Contains(t, content, "Purpose",
		"IAM resources should have Purpose tag")
}

// TestTaggingStrategyNamingConvention verifies naming convention compliance
func TestTaggingStrategyNamingConvention(t *testing.T) {
	t.Parallel()

	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}

	mainTfPath := filepath.Join(testDir, "..", "main.tf")
	content := ReadTerraformFile(t, mainTfPath)

	// Verify name_prefix follows bedrockgw-{env} pattern
	assert.Contains(t, content, "name_prefix = \"bedrockgw-${var.environment}\"",
		"Name prefix should follow bedrockgw-{environment} pattern")
}

// TestTaggingStrategyEnvironmentVariable verifies environment variable configuration
func TestTaggingStrategyEnvironmentVariable(t *testing.T) {
	t.Parallel()

	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}

	variablesPath := filepath.Join(testDir, "..", "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Verify environment variable has validation
	assert.Contains(t, content, "var.environment",
		"Variables should include environment variable")
	assert.Contains(t, content, "contains([\"dev\", \"test\", \"prod\"]",
		"Environment variable should validate dev, test, or prod")
}

// TestTaggingStrategyCostCenterVariable verifies cost_center variable configuration
func TestTaggingStrategyCostCenterVariable(t *testing.T) {
	t.Parallel()

	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}

	variablesPath := filepath.Join(testDir, "..", "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Verify cost_center variable exists
	assert.Contains(t, content, "variable \"cost_center\"",
		"Variables should include cost_center for billing allocation")
}

// TestTaggingStrategyNoHardcodedTags verifies no hardcoded tag values
func TestTaggingStrategyNoHardcodedTags(t *testing.T) {
	t.Parallel()

	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}

	mainTfPath := filepath.Join(testDir, "..", "main.tf")
	content := ReadTerraformFile(t, mainTfPath)

	// Verify Environment tag uses variable
	assert.Contains(t, content, "Environment = var.environment",
		"Environment tag should use variable, not hardcoded value")

	// Verify CostCenter tag uses variable
	assert.Contains(t, content, "CostCenter  = var.cost_center",
		"CostCenter tag should use variable, not hardcoded value")
}

// TestTaggingStrategyAllModulesHaveCommonTagsVariable verifies all modules accept common_tags
func TestTaggingStrategyAllModulesHaveCommonTagsVariable(t *testing.T) {
	t.Parallel()

	modules := []string{"networking", "eks", "rds", "redis", "alb", "ecr", "iam"}

	for _, mod := range modules {
		modulePath := GetModulePath(t, mod)
		variablesPath := filepath.Join(modulePath, "variables.tf")
		content := ReadTerraformFile(t, variablesPath)

		assert.Contains(t, content, "variable \"common_tags\"",
			"Module %s should have common_tags variable", mod)
	}
}

// TestTaggingStrategyAllModulesHaveEnvironmentVariable verifies all modules have environment variable
func TestTaggingStrategyAllModulesHaveEnvironmentVariable(t *testing.T) {
	t.Parallel()

	modules := []string{"networking", "eks", "rds", "redis", "alb", "ecr", "iam"}

	for _, mod := range modules {
		modulePath := GetModulePath(t, mod)
		variablesPath := filepath.Join(modulePath, "variables.tf")
		content := ReadTerraformFile(t, variablesPath)

		assert.Contains(t, content, "variable \"environment\"",
			"Module %s should have environment variable", mod)
	}
}

// TestTaggingStrategyAllModulesHaveNamePrefixVariable verifies all modules have name_prefix variable
func TestTaggingStrategyAllModulesHaveNamePrefixVariable(t *testing.T) {
	t.Parallel()

	modules := []string{"networking", "eks", "rds", "redis", "alb", "ecr", "iam"}

	for _, mod := range modules {
		modulePath := GetModulePath(t, mod)
		variablesPath := filepath.Join(modulePath, "variables.tf")
		content := ReadTerraformFile(t, variablesPath)

		assert.Contains(t, content, "variable \"name_prefix\"",
			"Module %s should have name_prefix variable", mod)
	}
}
